"""Move a plain checkout onto the version-tree layout.

docs/UPDATE_ARCHITECTURE.md §5. Every install in the field is an ordinary git
checkout at ``AQG_ROOT``. The managed-update engine needs ``AQG_ROOT`` to be a
**symlink** into a version directory, because that is what makes an update an
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
    from scripts.aqg_update import lock, stage
except ImportError:  # invoked with scripts/ itself on sys.path
    from aqg_update import lock, stage  # type: ignore[no-redef]

VERSIONS_DIRNAME = "versions"

#: Left beside the root while it does not exist. Named to be found by someone
#: staring at a directory listing wondering where their install went.
SIGNPOST_FILENAME = "AQG-MIGRATION-INTERRUPTED.txt"

#: Legacy directories and non-version labels retain full commit names.
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
            f"directory must hold the exact committed contents")
    if untracked:
        raise MigrateError(
            f"{root} has untracked files ({len(untracked)}, e.g. "
            f"{untracked[0][3:]!r}); they would be carried into a tree named "
            f"for a commit that never contained them")

    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise MigrateError(f"{root} has no HEAD commit to name a version after")
    commit = head.stdout.decode("ascii", "replace").strip()
    if len(commit) != _SHA_LENGTH or not all(c in "0123456789abcdef" for c in commit):
        raise MigrateError(f"{root}: git returned an unusable HEAD {commit!r}")
    return commit


def _owned_release_tree(entry: Path, versions_dir: Path) -> bool:
    """A short label alone cannot give AQG ownership of another tool's tree."""
    if not stage.is_release_name(entry.name) or not stage._is_version_tree(entry):
        return False
    if not (entry / "scripts/_aqg_context.sh").is_file():
        return False
    try:
        stage.version_commit(entry)
        common = Path(stage._git("rev-parse", "--git-common-dir", cwd=entry))
        return (entry / common).resolve().is_relative_to(versions_dir.resolve())
    except (stage.StageError, OSError, ValueError):
        return False


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
        if not (
            (len(entry.name) == _SHA_LENGTH
             and all(c in "0123456789abcdef" for c in entry.name))
            or _owned_release_tree(entry, versions_dir)
        )
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


#: Where an install AQG manages lives unless told otherwise. Converting anything
#: else is a decision about someone's own directory, so it is opt-in.
MANAGED_ROOT_SUFFIX = (".deeppattern", "agent-quality-gates")


@dataclass(frozen=True)
class LayoutResult:
    """What `ensure_managed_layout` did, and why, without ever raising.

    `managed` is the only field a caller should report from: it says whether
    the install can now update itself. `ok` means the call did not fail, which
    is a different question — declining to convert is a successful call that
    leaves updates OFF, and conflating the two announced "enabled" on exactly
    the install that could not update (audit aud_Y3qcP8c7jPy4xs_H, four voices).
    """

    ok: bool
    changed: bool
    managed: bool
    reason: str
    target: Optional[Path] = None


def logical_root(path: Path) -> Path:
    """The spelling of *path* that owns a route, or *path* canonicalised.

    Two different needs were being served by one string:

    * **executing** out of the checkout wants the PHYSICAL path, so one skill
      invocation cannot tear across a swap — `scripts/_aqg_context.sh` resolves
      for exactly that reason and is right to;
    * **owning a route** wants a spelling that does not change when the version
      does, because ownership is an exact link-text comparison and
      `versions/<sha>` stops matching at the next release.

    **Nothing is computed from the caller's spelling.** Five rounds of review
    found five ways to make two derivations of "the same" string disagree — a
    symlinked `$HOME`, `/var` -> `/private/var`, a `versions/` directory moved
    to another volume, a staged tree that is itself a link — and each fix
    produced the next case. They share one cause: the installer is handed the
    root as a human spelled it, the update engine resolves its own, and anything
    computed FROM those inputs can differ. So the managed root is named, not
    derived: `~/.deeppattern/agent-quality-gates` is where AQG installs.

    The only judgement is whether *path* is that install, and it is made on
    physical identity — the one comparison that cannot be spelled two ways.

    **Why this is anchored on `Path.home()` and not searched for.** Round 7 tried
    searching the tree's own ancestors so that an installer and an update engine
    under different `HOME` would still agree. Measured, it did not even fix that
    case — with `versions/` on another volume the managed link is not on the
    tree's ancestor chain at all — and it made any symlink named
    `agent-quality-gates` in any ancestor directory into an ownership root,
    which is what decides what the prune loop may delete. Strictly worse, so it
    is gone.

    The `HOME` dependency it was trying to remove is pre-existing and systemic
    rather than introduced here: the state root, every host adapter's default
    locations, and the installer this repo publishes all anchor on it. An
    install made under one `HOME` and updated under another is already looking
    at a different machine's worth of directories before ownership is reached.

    That is not the same as "it cannot happen", and an earlier draft of this
    docstring said so — wrongly, and a reviewer caught it. If the second `HOME`
    happens to contain a host skills directory of its own, the roster is read
    from THAT directory, none of it is ours, and the host is planned a full
    route: the update stalls at `pending` rather than doing damage. The failure
    is a stall, in the direction everything else here fails in.

    Accepting the constraint is still the right call — the mechanism built to
    route around it adopted any similarly-named symlink in any ancestor as an
    ownership root, and ownership decides what the prune loop deletes. A stall
    under a mismatched `HOME` is worse than nothing and much better than
    that.

    An unmanaged checkout has no swap to survive and gets its own fully resolved
    spelling: also identical from either side, for the same reason.
    """
    candidate = _canonical(Path(path).expanduser())
    try:
        managed = _canonical(Path.home() / MANAGED_ROOT_SUFFIX[0]) / MANAGED_ROOT_SUFFIX[-1]
        if managed.is_symlink() and _canonical(managed) == candidate:
            return managed
    except (OSError, RuntimeError):
        # `Path.home()` raises RuntimeError — not OSError — when HOME is unset
        # and the uid has no passwd entry. Both sides call this unconditionally,
        # so it must not be the thing that makes an update crash.
        pass
    return candidate


def _canonical(path: Path) -> Path:
    """One spelling for one directory, whoever names it.

    `resolve()` when it exists — the only way two callers who spelled a path
    differently arrive at the same string. Lexical normalisation when it does
    not, so a path not yet created still has an answer rather than an exception.
    """
    try:
        return Path(path).resolve()
    except OSError:
        return Path(os.path.normpath(str(Path(path).absolute())))


def _is_managed_location(root: Path) -> bool:
    """Whether *root* is the location AQG installs to and therefore owns.

    The previous discriminator was that a fresh clone has no untracked files
    and a developer's copy usually does. That is a claim about habits: a clean
    checkout on a feature branch has none either, and it would have been moved
    silently and then updated away from that branch.
    """
    try:
        resolved = Path(root).expanduser().absolute()
    except (OSError, RuntimeError):
        return False
    return resolved.parts[-len(MANAGED_ROOT_SUFFIX):] == MANAGED_ROOT_SUFFIX


def ensure_managed_layout(root: Path, *, managed: Optional[bool] = None) -> LayoutResult:
    """Put an install on the managed-update layout at install time, or say why not.

    `_apply` refuses any root that is not a symlink into a versions directory,
    and no install path produced one: both `scripts/install.sh` and
    decision-engine's `de-aqg-install` clone into a plain directory. So every
    fresh install could fetch a release, verify its signature, and then refuse
    to apply it.

    **Never raises.** An install that cannot be converted is still a working
    install — it simply will not update itself. What it must not do is stay
    quiet, so every refusal carries its reason and `managed` says plainly
    whether updates are on.

    **Only converts what AQG owns.** This is a one-way change to a directory,
    so it happens at the location AQG installs to, or when the caller states
    otherwise: `managed=True`, or `AQG_MIGRATE=1` for someone installing
    deliberately elsewhere. `AQG_NO_MIGRATE` declines everywhere.

    The refusals beyond that are `migrate`'s own, unchanged and not widened.
    """
    root = Path(root)
    declined = os.environ.get("AQG_NO_MIGRATE")
    if declined:
        return LayoutResult(
            ok=True, changed=False, managed=False,
            reason=f"AQG_NO_MIGRATE={declined!r} is set; the layout was left alone",
        )
    if managed is None:
        managed = _is_managed_location(root) or bool(os.environ.get("AQG_MIGRATE"))
    if not managed:
        return LayoutResult(
            ok=True, changed=False, managed=False,
            reason=(
                f"{root} is not the managed install location "
                f"(~/{'/'.join(MANAGED_ROOT_SUFFIX)}); it was left alone. "
                f"Set AQG_MIGRATE=1 to convert an install kept elsewhere."
            ),
        )
    try:
        result = migrate(root)
    except MigrateError as exc:
        return LayoutResult(ok=False, changed=False, managed=False, reason=str(exc))
    except OSError as exc:  # aqg: top-level boundary
        return LayoutResult(
            ok=False, changed=False, managed=False,
            reason=f"cannot migrate {root}: {exc}",
        )
    if result.already:
        return LayoutResult(
            ok=True, changed=False, managed=True,
            reason="already on the managed-update layout", target=result.target,
        )
    return LayoutResult(
        ok=True, changed=True, managed=True,
        reason=f"moved onto the managed-update layout at {result.target}",
        target=result.target,
    )


def migrate(root: Path, *, dry_run: bool = False) -> Migration:
    """Turn *root* into a version-or-commit directory link, or explain why not.

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
    recorded = _git(root, "show", f"{commit}:VERSION")
    version = recorded.stdout.decode("utf-8", "replace").strip() if recorded.returncode == 0 else ""
    target = versions_dir / stage.version_name(version, commit, versions_dir)
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
        os.symlink(str(target), str(staged_link), target_is_directory=True)
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
                hint = ""
                if os.name == "nt" and getattr(exc, "winerror", None) in (5, 32):
                    hint = " Close applications reading AQG files, check directory permissions, and retry; the checkout was preserved."
                raise MigrateError(f"cannot move {root} to {target}: {exc}.{hint}") from exc
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
