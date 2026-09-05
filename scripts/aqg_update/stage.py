"""Materialize a version beside the live one, and switch to it atomically.

docs/UPDATE_ARCHITECTURE.md §5.1. The update is **additive**. A new version is
checked out into its own directory and a symlink is re-pointed; nothing is ever
overwritten in place. That is the whole reason an update is safe while sessions
are running: a hook that already resolved the old tree keeps reading it, because
the old tree is still there. DE resets its checkout in place and relies on a
recovery tree, which is right for DE — only its own launcher reads that checkout,
and it holds the lock. AQG's checkout is dereferenced by every hook of every
concurrent session on every tool call, so in-place is not available here.

Versions are `git worktree` checkouts sharing one object store, so a retained
version costs its working tree (~17 MB) rather than a full clone (~43 MB).

**The refusal that matters most is the migration case.** Every machine installed
today has `~/.deeppattern/agent-quality-gates` as a real git checkout, not a
symlink. `swap_root` refuses to *replace* anything that is not a symlink this
layer created — it creates a link only where nothing exists: that directory is
the user's entire install, and replacing it is unrecoverable. Migrating an
existing install into this layout is a separate, deliberate operation.

Assumptions, each enforced below rather than trusted:

* a version *name* is a single bare path component (`_require_safe_name`);
* `versions_dir` is a real directory, not a symlink — `iterdir()` on a link
  lists the *target's* children, which would let pruning delete elsewhere;
* a swap *target* is absolute and carries the version sentinel — a relative one
  is written into the link verbatim and then resolved against the root's
  directory, producing a dangling root right after a "successful" swap;
* a tree is only removed if it carries that same sentinel. The sentinel used to
  be checked before the *reversible* step and not the *irreversible* one.

Two residuals this layer cannot close alone, both owned by the transaction in
PR4: the check-to-rename window in `swap_root` (the update lock serializes it),
and pruning a version some long-running session is still executing from
(`prune_versions` protects the live tree, but tracking every session's resolved
version needs state that lives above here).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Tuple

#: A materialized version is recognizable by the sentinel every AQG checkout has.
#: Used to refuse pointing the root at a directory that is not one.
VERSION_SENTINEL = "VERSION"


class StageError(RuntimeError):
    """A refusal from the staging layer. Always fail-closed."""


def _require_safe_name(name: str) -> str:
    """Refuse anything but a single bare path component.

    `versions_dir / name` is replaced outright by an absolute name and escapes
    with `..`, and the failure path then removes a tree at wherever that landed.
    """
    if not name or name in {".", ".."} or os.sep in name or "/" in name:
        raise StageError(f"invalid version name {name!r}: expected one path component")
    if os.altsep and os.altsep in name:
        raise StageError(f"invalid version name {name!r}: expected one path component")
    if Path(name).is_absolute() or Path(name).name != name:
        raise StageError(f"invalid version name {name!r}: expected one path component")
    return name


def _holds_the_object_store(path: Path) -> bool:
    """Whether deleting *path* would destroy the repository the others read.

    Asked of git rather than inferred from the shape of ``.git``. An earlier
    version tested ``.git``-is-a-directory, which is a proxy and wrong in both
    directions: a ``--separate-git-dir`` clone has ``.git`` as a FILE and is
    perfectly safe to remove, while what actually matters is whether the
    **common** git directory lives inside this tree.

    A staged version tree is a `git worktree`, so its common dir is the migrated
    tree's — outside itself, and prunable. The migrated tree's own common dir is
    inside it, and removing it takes every other tree's repository with it.
    """
    try:
        found = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Cannot tell, so assume the dangerous answer: refusing to prune costs
        # disk, and pruning wrongly costs the repository.
        return True
    if found.returncode != 0:
        return False  # not a git tree at all; nothing depends on it
    common = found.stdout.strip()
    if not common:
        return True
    try:
        resolved = (Path(path) / common).resolve() if not Path(common).is_absolute() \
            else Path(common).resolve()
        return resolved == Path(path).resolve() or Path(path).resolve() in resolved.parents
    except (OSError, ValueError):
        return True


def _is_version_tree(path: Path) -> bool:
    """Whether *path* is a version tree the root may point at.

    Deliberately does NOT exclude the tree holding the object store: right after
    a migration the root points exactly there, and a rollback has to be able to
    point back at it. Prunability is a different question, asked separately
    below — collapsing the two made `swap_root` refuse the migrated tree, which
    would have made the first rollback impossible.
    """
    return (
        path.is_dir()
        and not path.is_symlink()
        and (path / VERSION_SENTINEL).is_file()
    )


def _is_prunable(path: Path) -> bool:
    """Whether *path* may be removed.

    A version tree, and not the one carrying the repository. The sentinel alone
    is not enough: it is ``VERSION``, which every AQG checkout has, so the
    migrated tree looked exactly like a version and ``keep=0`` removed it —
    which is not hypothetical, because retention is "current plus previous" and
    on any install this would have happened on the second update.
    """
    return _is_version_tree(path) and not _holds_the_object_store(path)


#: git options applied to every invocation. `acquire` verifies a COMMIT and this
#: module checks it out, so everything downstream is only as trustworthy as
#: "the staged tree IS that commit" — and checkout conversion breaks that
#: quietly. `core.autocrlf=true` is the Git for Windows installer default and
#: rewrites LF blobs to CRLF on the way to disk; `core.eol` does the same for
#: files git already calls text. Pinning both off here makes the staged bytes
#: the blob's bytes on every platform and for every file.
#:
#: Config, not an attribute, is deliberate. A tracked `.gitattributes` is what
#: A used to rely on, and it (a) covered only `*.sh`, leaving `.py` — most of
#: what this engine installs — converted anyway, and (b) is refused outright by
#: the WS-8 paid-transport guard, since `git archive` honours it. Attributes win
#: over config, so this pin cannot restore exactness if an attribute ever comes
#: back; test_a_staged_version_is_blob_exact_under_a_converting_config is what
#: notices.
_GIT_CONVERSION_OFF = (
    "-c", "core.autocrlf=false",
    "-c", "core.eol=lf",
)


def _git(*args: str, cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *_GIT_CONVERSION_OFF, *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StageError(f"git {args[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        raise StageError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def stage_version(
    *, repo: Path, commit: str, versions_dir: Path, name: str
) -> Path:
    """Check *commit* out into ``versions_dir/name`` and return that path.

    Refuses an existing name rather than reusing it: a directory left by an
    earlier, failed attempt would be handed back as if it were the version that
    was asked for, and nothing downstream re-verifies its contents.
    """
    versions_dir = Path(versions_dir)
    _require_safe_name(name)
    target = versions_dir / name
    if target.exists() or target.is_symlink():
        raise StageError(f"a staged version already exists at {target}")
    try:
        versions_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StageError(f"cannot create {versions_dir}: {exc}") from exc
    try:
        _git("worktree", "add", "--detach", str(target), commit, cwd=Path(repo))
    except StageError as exc:
        # A failed `worktree add` can leave a partial directory and a registry
        # entry. Cleanup is best-effort, so the post-condition is CHECKED: if a
        # corpse survives, say so here. Otherwise the next attempt meets a bare
        # "already exists" for a version that was never materialized, and the
        # name is wedged with no indication why.
        _remove_worktree(Path(repo), target)
        if target.exists() or target.is_symlink():
            raise StageError(
                f"{exc}. Cleanup also failed: {target} survives and needs manual "
                f"removal before this version name can be staged again"
            ) from exc
        raise
    return target


def discard_version(*, repo: Path, target: Path) -> None:
    """Remove a tree that was staged and then not used.

    Public because a caller can decide not to apply a version *after* staging
    it — the plan is only knowable once the tree exists — and a staged tree left
    behind blocks every later attempt at that same commit, since the directory
    is named after it.
    """
    target = Path(target)
    if not target.exists() and not target.is_symlink():
        return
    _remove_worktree(Path(repo), target)


def _remove_worktree(repo: Path, target: Path) -> None:
    """Best-effort removal of a worktree and its registry entry."""
    try:
        _git("worktree", "remove", "--force", str(target), cwd=repo)
        return
    except StageError:
        pass
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target, ignore_errors=True)
    try:
        _git("worktree", "prune", cwd=repo)
    except StageError:
        pass


def current_target(root: Path) -> Optional[Path]:
    """Where the root symlink points, or ``None`` if it is not a symlink."""
    root = Path(root)
    if not root.is_symlink():
        return None
    try:
        raw = Path(os.readlink(root))
    except OSError as exc:
        raise StageError(f"cannot read the root link at {root}: {exc}") from exc
    # Absolutized against the LINK's directory, not the caller's cwd: a relative
    # link reported verbatim would be resolved against whatever directory the
    # caller happens to be in.
    return raw if raw.is_absolute() else (root.parent / raw).resolve()


def swap_root(*, root: Path, target: Path) -> Optional[Path]:
    """Point *root* at *target* atomically. Returns the version it replaced.

    The previous target is returned rather than left to be looked up afterwards,
    because by then the link has already moved and the answer is gone — and a
    rollback needs it.

    Refuses when *root* exists as anything other than a symlink. See the module
    docstring: that is every install in the field today.
    """
    root = Path(root)
    target = Path(target)

    if not target.is_absolute():
        raise StageError(
            f"the swap target must be an absolute path, got {target}; a relative "
            f"one is written into the link verbatim and then resolved against "
            f"{root.parent}, which would leave the root dangling"
        )
    if not _is_version_tree(target):
        raise StageError(
            f"{target} is not an AQG version tree (a real directory carrying a "
            f"{VERSION_SENTINEL} sentinel); refusing to point the root at it"
        )

    previous: Optional[Path] = None
    if root.is_symlink():
        previous = current_target(root)
        # "Is a symlink" was standing in for "was created by this layer". A user
        # symlink to a relocated real checkout carries a VERSION file too, so the
        # sentinel alone does not distinguish them — the test is whether it lives
        # in the same versions directory the new target does.
        versions_dir = target.parent.resolve()
        if (
            previous is None
            or not _is_version_tree(previous)
            or previous.resolve().parent != versions_dir
        ):
            raise StageError(
                f"{root} points at {previous}, which is not a version tree under "
                f"{versions_dir}; refusing to re-point an install this layer does "
                f"not own"
            )
    elif root.exists():
        raise StageError(
            f"{root} exists and is not a symlink — refusing to replace it. An "
            f"install created before the version-tree layout is a real checkout "
            f"there, and replacing it would destroy it; migrate deliberately"
        )

    # A symlink cannot be re-pointed in place, so create it under a temporary
    # name in the same directory and rename over the old one. `os.replace` is
    # atomic, so a concurrent reader sees either the old target or the new one,
    # never a missing root.
    fd, tmp_name = tempfile.mkstemp(prefix=".aqg-root-", dir=str(root.parent))
    os.close(fd)
    os.unlink(tmp_name)
    try:
        os.symlink(target, tmp_name)
        # Re-validated immediately before the rename. `os.replace` onto a
        # directory fails, but onto a REGULAR FILE it succeeds and destroys it,
        # so the kernel does not enforce the refusal above on its own. This
        # narrows the window; the update lock closes it in PR4.
        if root.exists() and not root.is_symlink():
            raise StageError(
                f"{root} became a non-symlink while the swap was in flight; "
                f"refusing to replace it"
            )
        os.replace(tmp_name, root)
    except StageError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise StageError(f"cannot point {root} at {target}: {exc}") from exc
    return previous


def prune_versions(
    *,
    versions_dir: Path,
    keep: int,
    protected: Iterable[Path],
    repo: Path,
    root: Optional[Path] = None,
) -> Tuple[Path, ...]:
    """Remove all but the *keep* newest version trees. Returns what was removed.

    The live version — whatever *root* points at — is protected structurally
    rather than by asking the caller to remember it. ``protected`` adds anything
    else that must survive, such as a tree the journal still references.

    Only real directories directly inside ``versions_dir`` that carry the version
    sentinel are ever removed, and each removal takes its git worktree
    registration with it: an ``rmtree``'d worktree leaves a stale entry behind
    and the next ``worktree add`` for that name then refuses.
    """
    versions_dir = Path(versions_dir)
    if versions_dir.is_symlink():
        # `iterdir()` on a link lists the TARGET's children, so pruning would
        # delete outside the directory this function claims to confine itself to.
        raise StageError(
            f"refusing to prune through a symlinked versions directory: "
            f"{versions_dir}"
        )
    if not versions_dir.is_dir():
        return ()

    keep_paths = {Path(p).resolve() for p in protected}
    if root is not None:
        live = current_target(Path(root))
        if live is not None:
            keep_paths.add(live.resolve())

    # Directory mtime is the ordering signal, and it is approximate: writing
    # inside a tree updates it. A wrong order is bounded — the live tree, the
    # protected set, and anything without the sentinel are all excluded anyway.
    candidates = [child for child in versions_dir.iterdir() if _is_prunable(child)]
    candidates.sort(key=lambda p: p.stat().st_mtime)
    doomed = candidates[: max(len(candidates) - max(keep, 0), 0)]

    removed = []
    for tree in doomed:
        if tree.resolve() in keep_paths:
            continue
        _remove_worktree(Path(repo), tree)
        if not tree.exists():
            removed.append(tree)
    return tuple(removed)
