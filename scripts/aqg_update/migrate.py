"""Move a plain checkout onto the version-tree layout.

docs/UPDATE_ARCHITECTURE.md §5. Every install in the field is an ordinary git
checkout at ``AQG_ROOT``. The managed-update engine needs ``AQG_ROOT`` to be a
**symlink** into ``versions/<sha>/``, because that is what makes an update an
atomic ``os.replace`` of one link rather than an edit of a tree that every
running session is reading. Nothing bridged the two, which meant the automatic
channel could not apply anything to any install that exists.

**This is one-way, one-time, and it moves the user's tree.** So it refuses
anything it is not certain about — a dirty checkout, an untracked file, a
worktree, a `versions/` directory that belongs to somebody else, a parent it
cannot write — and it runs under the install lock, so a background update check
cannot inspect the layout while it is changing.

**The window, and what is and is not true about it.** No filesystem turns a
directory into a symlink atomically. The tree is renamed aside and the prepared
link is renamed into place, and between those two syscalls ``AQG_ROOT`` does not
exist. A hook firing in that gap runs `bash "$AQG_ROOT/..."` against a missing
path, fails, and is swallowed by its own ``|| true`` — so a single tool call
could go unguarded.

Three things narrow it, and one thing does not close it:

* the symlink is **created before** the window, under a temporary name, so the
  only syscalls inside it are two renames in one directory;
* a **failure** inside the window is undone — the tree is renamed back;
* ``SIGINT`` and ``SIGTERM`` are handled inside the window and undo before
  exiting, so Ctrl-C does not strand an install.

What none of that covers is ``SIGKILL`` or losing power. An earlier version of
this paragraph said the tree is put back "if it cannot finish", which
termination makes false. So a **signpost** is written beside the root before the
window and removed after it: if a user ever finds ``AQG_ROOT`` missing, the file
next to it carries the literal ``mv`` that restores them. It has to live outside
the root, because the tool that would fix this lives inside the directory that
vanished.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # invoked as a package (tests, `python3 -m`)
    from scripts.aqg_update import lock
except ImportError:  # invoked with scripts/ itself on sys.path
    from aqg_update import lock  # type: ignore[no-redef]

VERSIONS_DIRNAME = "versions"

#: Left beside the root while it does not exist. Named to be found by someone
#: staring at a directory listing wondering where their install went.
SIGNPOST_FILENAME = "AQG-MIGRATION-INTERRUPTED.txt"

#: A directory name under `versions/` is a full commit sha and nothing else, so
#: `stage.prune_versions` and this module agree about what belongs there.
_SHA_LENGTH = 40


class MigrateError(RuntimeError):
    """A refusal to migrate. Always fail-closed, and always non-destructive."""


@dataclass(frozen=True)
class Migration:
    root: Path
    versions_dir: Path
    target: Path
    already: bool = False


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise MigrateError(f"git {args[0]} timed out in {root}") from exc
    except OSError as exc:
        raise MigrateError(f"cannot run git: {exc}") from exc


def _require_clean_checkout(root: Path) -> str:
    """Return the commit this checkout is at, or refuse to touch it."""
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        raise MigrateError(f"{root} is not a git checkout; refusing to move it")

    # A worktree's `.git` is a FILE pointing at another repository's gitdir.
    # Moving it leaves a link that resolves until something writes through it.
    if (root / ".git").is_file():
        raise MigrateError(
            f"{root} is a git worktree, not a full checkout; migrating it would "
            f"leave its gitdir link pointing at a path that no longer holds it")

    status = _git(root, "status", "--porcelain")
    if status.returncode != 0:
        raise MigrateError(f"cannot read the state of {root}")
    lines = [line for line in status.stdout.decode("utf-8", "replace").splitlines() if line]
    untracked = [line for line in lines if line.startswith("??")]
    modified = [line for line in lines if not line.startswith("??")]
    if modified:
        raise MigrateError(
            f"{root} has uncommitted changes ({len(modified)}); the version "
            f"directory is named after a commit, and naming it after one whose "
            f"contents it does not hold would make every later check disagree")
    if untracked:
        raise MigrateError(
            f"{root} has untracked files ({len(untracked)}, e.g. "
            f"{untracked[0][3:]!r}); they would be carried into a tree named "
            f"after a commit that never contained them")

    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise MigrateError(f"{root} has no HEAD commit to name a version after")
    commit = head.stdout.decode("ascii", "replace").strip()
    if len(commit) != _SHA_LENGTH or not all(c in "0123456789abcdef" for c in commit):
        raise MigrateError(f"{root}: git returned an unusable HEAD {commit!r}")
    return commit


def _require_usable_versions_dir(versions_dir: Path) -> None:
    """`versions/` beside the install may be somebody else's directory."""
    if versions_dir.is_symlink():
        raise MigrateError(f"{versions_dir} is a symlink; refusing to write through it")
    if not versions_dir.exists():
        return
    if not versions_dir.is_dir():
        raise MigrateError(f"{versions_dir} exists and is not a directory")
    strangers = [
        entry.name for entry in versions_dir.iterdir()
        if len(entry.name) != _SHA_LENGTH
        or not all(c in "0123456789abcdef" for c in entry.name)
    ]
    if strangers:
        raise MigrateError(
            f"{versions_dir} holds entries that are not version trees "
            f"({strangers[:3]}); it belongs to something else and AQG will not "
            f"write into it")


def _require_writable_parent(root: Path) -> None:
    """The rename needs write on the PARENT, not on the root.

    Checked here rather than discovered by a failing syscall, because by then
    the user has already typed a confirmation for something one-way.
    """
    parent = root.parent
    if not os.access(str(parent), os.W_OK | os.X_OK):
        raise MigrateError(
            f"{parent} is not writable; moving {root.name} needs write "
            f"permission on the directory that contains it, not on it")


@contextmanager
def _undo_on_signal(undo):
    """Put the tree back on Ctrl-C. Does nothing for SIGKILL — see the signpost.

    Only installs handlers when running on the main thread; `signal.signal`
    raises anywhere else, and a migration that refused to run under a test
    runner's worker thread would be a worse outcome than one without handlers.
    """
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            def handler(signum, frame, _sig=sig):
                undo()
                signal.signal(_sig, previous.get(_sig, signal.SIG_DFL))
                os.kill(os.getpid(), _sig)
            previous[sig] = signal.signal(sig, handler)
    except (ValueError, OSError):  # not the main thread, or no such signal
        previous = {}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def migrate(root: Path, *, dry_run: bool = False) -> Migration:
    """Turn *root* into a symlink into ``versions/<sha>/``, or explain why not.

    Re-running is safe: an install already on the layout reports ``already`` and
    changes nothing, because a user who is unsure whether they migrated should
    be able to simply run it again.
    """
    root = Path(root)
    versions_dir = root.parent / VERSIONS_DIRNAME

    if root.is_symlink():
        # A symlink is not automatically "already migrated". A developer whose
        # AQG_ROOT points at a working copy has one too, and reporting that as
        # done would leave the engine staging versions into that copy's parent —
        # somebody else's directory.
        target = Path(os.path.realpath(root))
        ours = versions_dir.is_dir() and target.parent == versions_dir.resolve()
        if not ours:
            raise MigrateError(
                f"{root} is a symlink to {target}, which is not into a versions "
                f"directory at {versions_dir}. AQG will not adopt a layout it did "
                f"not create; point AQG_ROOT at a real checkout, or migrate that "
                f"checkout instead")
        return Migration(root=root, versions_dir=versions_dir, target=target, already=True)
    if not root.exists():
        raise MigrateError(f"there is nothing at {root} to migrate")
    if not root.is_dir():
        raise MigrateError(f"{root} is not a directory")

    commit = _require_clean_checkout(root)
    _require_usable_versions_dir(versions_dir)
    _require_writable_parent(root)
    target = versions_dir / commit
    if target.exists() or target.is_symlink():
        raise MigrateError(
            f"{target} already exists; a previous migration may have stopped "
            f"part-way. Check it before retrying")

    if dry_run:
        return Migration(root=root, versions_dir=versions_dir, target=target)

    # Under the lock, because a background update check starts at every session
    # start and inspects the very layout this is changing.
    try:
        with lock.install_lock():
            # Checked AGAIN now that the lock is held: `git status` and `rename`
            # were a check and a use with an unheld gap between them.
            recheck = _require_clean_checkout(root)
            if recheck != commit:
                raise MigrateError(
                    f"{root} moved from {commit} to {recheck} while this was "
                    f"starting; nothing was changed")
            _perform(root, versions_dir, target)
    except lock.LockBusy as exc:
        raise MigrateError(
            f"another AQG update is in progress; not moving the install now"
        ) from exc

    return Migration(root=root, versions_dir=versions_dir, target=target)


def _perform(root: Path, versions_dir: Path, target: Path) -> None:
    """Everything that touches the filesystem, in the order that narrows the gap."""
    try:
        versions_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MigrateError(f"cannot create {versions_dir}: {exc}") from exc

    # The link is made FIRST, under a temporary name. Allocating an inode is a
    # way to fail, and failing inside the window is what must not happen.
    staged_link = root.parent / f".aqg-root-{target.name[:12]}"
    for leftover in (staged_link,):
        if leftover.is_symlink() or leftover.exists():
            try:
                os.unlink(str(leftover))
            except OSError as exc:
                raise MigrateError(f"cannot clear {leftover}: {exc}") from exc
    try:
        os.symlink(str(target), str(staged_link))
    except OSError as exc:
        raise MigrateError(f"cannot create a symlink beside {root}: {exc}") from exc

    signpost = root.parent / SIGNPOST_FILENAME
    _write_signpost(signpost, root, target)

    def undo() -> None:
        if not root.exists() and target.exists():
            try:
                os.rename(str(target), str(root))
            except OSError:
                return
        for leftover in (staged_link, signpost):
            try:
                os.unlink(str(leftover))
            except OSError:
                pass

    moved = False
    try:
        with _undo_on_signal(undo):
            try:
                os.rename(str(root), str(target))
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise MigrateError(
                        f"{versions_dir} is on a different filesystem from {root}, "
                        f"so the install cannot be moved there without copying it. "
                        f"Move the checkout onto one filesystem first") from exc
                raise MigrateError(f"cannot move {root} to {target}: {exc}") from exc
            moved = True
            try:
                os.rename(str(staged_link), str(root))
            except OSError as exc:
                raise MigrateError(
                    f"cannot put the link in place at {root}: {exc}") from exc
    except BaseException:  # aqg: top-level boundary
        # Cleanup and re-raise. An install with no root at all is the worst
        # outcome available, so this runs for every exception, not only OSError.
        undo()
        raise

    if not moved:  # pragma: no cover - defensive
        undo()
        raise MigrateError("the install was not moved")
    try:
        os.unlink(str(signpost))
    except OSError:
        pass


def _write_signpost(path: Path, root: Path, target: Path) -> None:
    """A note beside the root, for the case nothing in this process gets to run.

    SIGKILL and power loss reach no handler. What survives them is a file, and
    the only useful thing to put in it is the exact command.
    """
    try:
        path.write_text(
            "AQG was moving this install onto the managed-update layout and did\n"
            "not finish. If there is nothing at:\n\n"
            f"    {root}\n\n"
            "then the install is intact at:\n\n"
            f"    {target}\n\n"
            "Put it back with:\n\n"
            f"    mv {target} {root}\n\n"
            "or complete the move by hand with:\n\n"
            f"    ln -s {target} {root}\n\n"
            "This file is removed automatically when the move succeeds.\n",
            encoding="utf-8",
        )
    except OSError:
        # Best effort. Failing to write the note must not stop the migration —
        # but it is the reason the note is written BEFORE anything moves.
        pass
