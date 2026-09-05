"""Cross-process admission for the managed update — one apply at a time.

**File existence is never authority.** That rule is borrowed verbatim from DE's
`update_coordination`, and it is the whole design. A lock implemented as "is the
file there?" fails in the two ways that matter: a crashed updater leaves a file
that wedges every future run, and the obvious remedy — delete the stale file —
silently permits two concurrent applies, which is the one thing this exists to
prevent. So the lock is an OS advisory lock held on an open handle: the kernel
releases it when the holder dies, and a leftover file does not block a new
acquire.

Three limits on that, each learned the hard way and each stated because a
guarantee is only worth what its exceptions say:

* **A leftover file is inert in state, not in ownership.** One run under `sudo`
  leaves a root-owned lock, after which every unprivileged acquire fails with
  `EACCES` — permanently. The error names the owner so that is diagnosable.
* **The crash release does not survive a fork.** A child inherits the open
  descriptor and keeps the lock alive after its parent dies (`set_inheritable`
  governs exec, not fork). Nothing here may fork while holding it.
* **`flock` semantics are unreliable on NFS.** The state root is a local profile
  path by default; an override onto a network filesystem is out of contract.

**A deleted lock cannot be made safe from the acquirer's side.** If the file is
unlinked while held, the next acquirer creates a fresh inode at the same path
and locks that — and from its point of view everything is consistent, so no
check it performs can notice. What the *holder* can do is notice that its own
file is gone (`st_nlink == 0`) and stop. ``install_lock`` therefore yields a
handle, and an apply must call ``still_held()`` before each irreversible step;
the identity check on acquire closes the narrower window where the file is
*replaced* between opening and locking.

`is_locked` is the other half, and it is for hooks. A SessionStart hook that
finds an apply in progress must no-op **immediately** — blocking there would
stall the user's session behind an update they did not ask for. Both entry
points are therefore non-blocking; nothing here ever waits.

The `_try_lock` / `_unlock` core is ported from DE, including its Windows branch
and errno handling, and so is its inode-identity re-check. That re-check was
omitted in the first draft on the grounds that it addressed a hostile local
process. **That justification was wrong**: its real trigger is an operator
deleting a lock file this docstring had called inert. A documented reason for
dropping a guard is itself a claim, and this one did not hold. Still not ported:
DE's Windows ACL validation, which does depend on a hostile-process model this
layer does not claim to cover.
"""

from __future__ import annotations

import errno
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from . import state

LOCK_FILENAME = "update.lock"


class LockError(RuntimeError):
    """The lock itself is unusable — a refusal, always fail-closed."""


class LockBusy(RuntimeError):
    """Someone else holds the lock.

    Deliberately not a subclass of ``LockError``: this is the ordinary,
    expected outcome of a second trigger firing, not a failure. A caller that
    sees it should exit quietly, not report a problem.
    """


def lock_path() -> Path:
    """Where the admission lock lives — beside install state.

    Same root, for the same reason (docs/UPDATE_ARCHITECTURE.md §4): the version
    swap replaces ``AQG_ROOT`` wholesale, so a lock kept inside it would vanish
    mid-apply, exactly when it is holding something back.
    """
    return state.state_root(create=True) / LOCK_FILENAME


class LockLost(LockError):
    """The lock was undermined while held — the file was removed or replaced.

    Raised by ``LockHandle.still_held``. A second apply may already be running,
    so the correct response is to stop before the next irreversible step, not to
    re-acquire.
    """


class LockHandle:
    """A held lock, and the only way to ask whether it is still meaningful."""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def still_held(self) -> None:
        """Raise ``LockLost`` if this lock no longer excludes anyone.

        An apply calls this before each irreversible step. It cannot prevent a
        second apply that started after the file was deleted — nothing on this
        side can — but it lets this one stop rather than proceed alongside it.
        """
        try:
            info = os.fstat(self._fd)
        except OSError as exc:
            raise LockLost(f"cannot re-check the update lock: {exc}") from exc
        # `st_nlink == 0` covers both deletion and replacement on POSIX: a
        # rename over the path unlinks our inode too. The identity comparison is
        # the belt to that brace — and the one that carries the acquire-time
        # race in `_lock_fd`, where the file may be swapped between open and
        # lock while both inodes still have links.
        if info.st_nlink == 0 or not _same_file(self._fd, self._path):
            raise LockLost(
                f"the update lock {self._path} was deleted or replaced while "
                f"held; another apply may have started, so this one must not "
                f"continue"
            )


class _StructuralLockError(LockError):
    """The path cannot hold a lock at all — a symlink, a directory, a fifo.

    Separated from an environmental failure because the two have OPPOSITE safe
    answers for a probe: nobody can be holding a path like this, whereas "I ran
    out of file descriptors" says nothing about whether an apply is running.
    """


def _open_lock_file(path: Path) -> int:
    """Open the lock file privately, refusing anything that cannot hold a lock."""
    # Best-effort and for a clearer message only: this is a check-then-open
    # race. `O_NOFOLLOW` is the guard for the final component, and the
    # post-lock identity check in `_lock_fd` is what closes the rest.
    if path.is_symlink():
        raise _StructuralLockError(f"refusing to lock through a symlink: {path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR}:
            raise _StructuralLockError(
                f"the update lock path {path} cannot hold a lock: {exc}"
            ) from exc
        if exc.errno == errno.EACCES:
            raise LockError(
                f"cannot open the update lock at {path}: {exc}. If a previous run "
                f"was elevated the file may belong to another user; check its "
                f"owner and remove it only when no update can be running"
            ) from exc
        raise LockError(f"cannot open the update lock at {path}: {exc}") from exc
    try:
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _StructuralLockError(
                f"the update lock at {path} is not a regular file"
            )
        # On the descriptor, not the path, and unconditionally: `os.open`'s mode
        # applies only when it CREATES the file, and this design deliberately
        # reuses a pre-existing one.
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):  # Windows has no meaningful fchmod
            pass
    except LockError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise LockError(f"cannot inspect the update lock at {path}: {exc}") from exc
    return fd


def _same_file(fd: int, path: Path) -> bool:
    """Whether the locked descriptor is still the file at *path*."""
    try:
        held = os.fstat(fd)
        current = os.stat(path)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _lock_fd(target: Path, *, attempts: int = 3) -> int:
    """Acquire the lock and return the held descriptor, or raise ``LockBusy``.

    Two reasons to retry rather than answer on the first result:

    * The lock file can be replaced between opening and locking, in which case
      the descriptor we hold is no longer the file at the path — the unlink
      bypass. Re-open and try again.
    * ``is_locked`` genuinely takes the lock while it is free, so an updater
      starting in that instant would see a spurious busy and skip silently.
      A short retry removes a window that would be near-impossible to diagnose.
    """
    delay = 0.02
    for attempt in range(attempts):
        fd = _open_lock_file(target)
        if _try_lock(fd):
            if _same_file(fd, target):
                return fd
            # Someone replaced or removed the file under us; the lock we hold is
            # on an inode nobody else will find.
            _unlock(fd)
            os.close(fd)
        else:
            os.close(fd)
        if attempt < attempts - 1:
            time.sleep(delay)
            delay *= 2
    raise LockBusy(f"another AQG update holds {target}")


def _try_lock(fd: int) -> bool:
    """Take the lock without waiting. ``False`` means someone else holds it."""
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                return False
            raise LockError(f"cannot acquire the Windows update lock: {exc}") from exc
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise LockError(f"cannot acquire the POSIX update lock: {exc}") from exc
    return True


def _unlock(fd: int) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        # Releasing is best-effort: closing the fd below drops the lock anyway,
        # and the kernel would drop it at process exit regardless.
        pass


@contextmanager
def install_lock(*, path: Optional[Path] = None) -> Iterator[LockHandle]:
    """Hold the update admission lock for the duration of the block.

    Raises ``LockBusy`` immediately if another process holds it — never waits.
    Released on the way out however the block ends, including an exception, so
    a failed apply cannot leave the machine unable to update.

    Yields a :class:`LockHandle`; a long apply must call ``still_held()`` before
    each irreversible step, because a deleted lock file admits a second apply
    that no check on the acquiring side can detect.
    """
    try:
        target = Path(path) if path is not None else lock_path()
    except state.StateError as exc:
        # Resolving the default location is part of acquiring: a failure here is
        # a refusal, in this entry point's documented direction.
        raise LockError(f"cannot resolve the update lock location: {exc}") from exc
    fd = _lock_fd(target)
    try:
        yield LockHandle(fd, target)
    finally:
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass


def is_locked(*, path: Optional[Path] = None) -> bool:
    """Whether an apply is in progress. Never waits, never keeps the lock.

    A hook calls this to decide whether to stand down. It probes by trying to
    take the lock and releasing it at once: the only honest way to answer, since
    the answer is a kernel property rather than anything on disk.

    "Unusable" is two conditions with opposite safe answers, and they are
    reported differently:

    * **Structural** — a symlink, a directory, an unresolvable state root. No
      apply can be holding a path like this, so the honest answer is *not
      locked*; reporting otherwise would make a hook stand down forever and
      silently disable updates.
    * **Environmental** — out of descriptors, out of space, a permission error.
      This says nothing about whether an apply is running, so the safe answer is
      *locked*: the hook stands down for this run only, and these conditions are
      transient by nature.

    The apply path (``install_lock``) fails closed on both.
    """
    try:
        target = Path(path) if path is not None else lock_path()
    except state.StateError:
        return False  # structural: no lock can exist at a root we cannot resolve
    try:
        fd = _open_lock_file(target)
    except _StructuralLockError:
        return False
    except LockError:
        return True
    try:
        if not _try_lock(fd):
            return True
        _unlock(fd)
        return False
    except LockError:
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
