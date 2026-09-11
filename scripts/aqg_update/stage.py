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
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

#: A materialized version is recognizable by the sentinel every AQG checkout has.
#: Used to refuse pointing the root at a directory that is not one.
VERSION_SENTINEL = "VERSION"


def is_release_name(name: str) -> bool:
    """A bounded, portable version label, never an arbitrary manifest path."""
    # Keep the existing 40-character path budget on Windows, including suffixes.
    return isinstance(name, str) and len(name) <= 40 and re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", name,
    ) is not None


def version_name(version: str, commit: str, versions_dir: Path) -> str:
    """Prefer a release label; distinguish reissued versions without overwriting.

    Never reuse a leftover checkout, even with the same HEAD: it may be partial
    or locally modified. Give retries a fresh bounded name, leaving occupied
    paths (including dangling links) intact. stage_version still refuses races.
    """
    name = version if is_release_name(version) else commit
    occupied = Path(versions_dir) / name
    if _is_version_tree(occupied):
        try:
            if version_commit(occupied) != commit:
                suffixed = f"{version}-{commit[:12]}"
                name = suffixed if is_release_name(suffixed) else commit
        except StageError:
            pass  # Choose fresh content instead of trusting an unreadable tree.
    occupied = Path(versions_dir) / name
    if occupied.exists() or occupied.is_symlink():
        # Never trade a permanent name collision for unbounded disk growth
        # when Windows/permissions prevent cleanup. No persistent disable flag:
        # freeing a retained attempt makes the next invocation eligible again.
        prefix = f'{name[:23]}-'
        retained = sum(bool(re.fullmatch(re.escape(prefix) + r'[0-9a-f]{16}', child.name))
                       for child in Path(versions_dir).iterdir())
        if retained >= 3:
            raise StageError(f'3 retained retry trees at {versions_dir}; release file locks and remove unused retry trees before retrying')
        name = f'{name[:23]}-{uuid.uuid4().hex[:16]}'
    return name


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


def _holds_the_object_store(path: Path, *, timeout: float = 30) -> bool:
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
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
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


def _is_prunable(path: Path, *, timeout: float = 30) -> bool:
    """Whether *path* may be removed.

    A version tree, and not the one carrying the repository. The sentinel alone
    is not enough: it is ``VERSION``, which every AQG checkout has, so the
    migrated tree looked exactly like a version and ``keep=0`` removed it —
    which is not hypothetical, because retention is "current plus previous" and
    on any install this would have happened on the second update.
    """
    return _is_version_tree(path) and not _holds_the_object_store(path, timeout=timeout)


def _is_indirection(path: Path) -> bool:
    """Inspect this node, allowing symlinked ancestors but never junctions."""
    return path.is_symlink() or (path.exists() and bool(
        getattr(path.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


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


def _git(*args: str, cwd: Path, timeout: float = 300) -> str:
    # A commit-named generation is deeper than the initial clone. Native Git
    # otherwise rejects valid release paths once this crosses MAX_PATH.
    platform_config = ("-c", "core.longpaths=true") if os.name == "nt" else ()
    try:
        proc = subprocess.run(
            ["git", *_GIT_CONVERSION_OFF, *platform_config, *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StageError(f"git {args[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        raise StageError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout if "-z" in args else proc.stdout.strip()


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


def version_commit(path: Path) -> str:
    """Read this generation's HEAD, refusing Git discovery in its ancestors."""
    path = Path(path)
    if Path(_git("rev-parse", "--show-toplevel", cwd=path)).resolve() != path.resolve():
        raise StageError(f"{path} is not the root of a Git checkout")
    return _git("rev-parse", "HEAD", cwd=path)


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
    target = raw if raw.is_absolute() else (root.parent / raw).resolve()
    # Windows readlink returns a \\?\ prefix even for an ordinary drive path.
    # Resolve from the original spelling so ownership comparisons agree, but
    # preserve a directly linked symlink for the version-tree refusal below.
    if os.name == "nt" and not target.is_symlink():
        return Path(os.path.realpath(root))
    return target


def _replace_root_link(source: str, root: Path) -> None:
    """Replace the link without unlinking the live root, including on Windows.

    MoveFileEx (used by os.replace) cannot replace a Windows directory link.
    FileRenameInfoEx supplies POSIX replacement semantics for the reparse point
    itself. Unsupported filesystems/Windows versions fail with the old root
    intact; there is deliberately no unlink-then-rename fallback.
    https://learn.microsoft.com/windows/win32/api/winbase/ns-winbase-file_rename_info
    """
    if os.name != "nt":
        os.replace(source, root)
        return

    import ctypes
    from ctypes import wintypes

    class RenameInfo(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * 1)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    rename = kernel.SetFileInformationByHandle
    rename.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    rename.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    # DELETE access; share read/write/delete; OPEN_EXISTING; BACKUP_SEMANTICS
    # permits a directory handle, OPEN_REPARSE_POINT prevents following it.
    handle = create(source, 0x00010000, 0x7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        name = str(root.absolute()).encode("utf-16-le")
        buffer = ctypes.create_string_buffer(ctypes.sizeof(RenameInfo) + len(name))
        info = RenameInfo.from_buffer(buffer)
        info.Flags = 0x3  # REPLACE_IF_EXISTS | POSIX_SEMANTICS
        info.FileNameLength = len(name)
        ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, name, len(name))
        if not rename(handle, 22, buffer, len(buffer)):  # FileRenameInfoEx
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)


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
        os.symlink(target, tmp_name, target_is_directory=True)
        # Re-validated immediately before the rename. `os.replace` onto a
        # directory fails, but onto a REGULAR FILE it succeeds and destroys it,
        # so the kernel does not enforce the refusal above on its own. This
        # narrows the window; the update lock closes it in PR4.
        if root.exists() and not root.is_symlink():
            raise StageError(
                f"{root} became a non-symlink while the swap was in flight; "
                f"refusing to replace it"
            )
        _replace_root_link(tmp_name, root)
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


def _canonical_or_none(path: Path) -> Optional[Path]:
    """`resolve()` where it works, ``None`` where it cannot. Never raises."""
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _host_pinned_paths() -> Tuple[Path, ...]:
    """Every path an installed host hook command executes, across all hosts.

    Imported lazily: `hosts` imports this module for staging, so binding it at
    module scope would be a cycle. Any failure refuses pruning: an unknown pin
    cannot be treated as evidence that an old tree is unused.
    """
    try:
        from scripts.aqg_update import hosts as hosts_mod  # noqa: PLC0415
    except ImportError:  # pragma: no cover - packaging variant
        try:
            from aqg_update import hosts as hosts_mod  # type: ignore[no-redef]
        except ImportError as exc:
            raise StageError(
                f"cannot enumerate host hook pins, so no version tree can be "
                f"shown to be unused: {exc}"
            ) from exc
    found: list[Path] = []
    for client_id in hosts_mod.available_clients():
        try:
            found.extend(hosts_mod.adapter_for(client_id).pinned_command_paths())
        except Exception as exc:  # aqg: top-level boundary
            # RAISE, do not skip. The two enforcement points must fail in the
            # SAME direction or the argument they share is void: the apply gate
            # treats an adapter that cannot answer as "blocks", but a pruner
            # that treated it as "pins nothing" would DELETE the tree a host
            # was already deferred onto — the gate's caution undone later by
            # the pruner's carelessness, on a machine that had been working.
            raise StageError(
                f"{client_id} could not report the paths its hooks execute, so "
                f"no version tree can be shown to be unused: {exc}"
            ) from exc
    return tuple(found)


def prune_versions(
    *,
    versions_dir: Path,
    keep: int,
    protected: Iterable[Path],
    repo: Path,
    root: Optional[Path] = None,
    budget_seconds: float = 5,
    before_remove: Optional[Callable[[], None]] = None,
    progress: Optional[dict] = None,
) -> Tuple[Path, ...]:
    """Remove all but the *keep* newest version trees. Returns what was removed.

    The live version — whatever *root* points at — is protected structurally
    rather than by asking the caller to remember it. ``protected`` adds anything
    else that must survive, such as a tree the journal still references.

    Only real directories directly inside ``versions_dir`` that carry the version
    sentinel are ever removed, and each removal takes its git worktree
    registration with it. Git must accept a non-forced removal; dirty, locked,
    or unregistered trees are retained. There is no recursive-delete fallback.
    The budget limits starting more work; an admitted removal has a separate
    30-second timeout, never the shrinking remainder of the inspection budget.
    """
    versions_dir = Path(versions_dir)
    if _is_indirection(versions_dir):
        # `iterdir()` on a link lists the TARGET's children, so pruning would
        # delete outside the directory this function claims to confine itself to.
        raise StageError(
            f"refusing to prune through a symlinked versions directory: "
            f"{versions_dir}"
        )
    if not versions_dir.is_dir():
        return ()
    versions_dir = versions_dir.resolve()
    progress = progress if progress is not None else {}
    progress.setdefault("removed", [])
    progress.setdefault("skipped", [])

    deadline = time.monotonic() + budget_seconds
    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise StageError("history cleanup budget exhausted; retained remaining versions")
        return seconds

    remaining()

    keep_paths = {Path(p).resolve() for p in protected}
    def refresh_pins():
        remaining()
        pins = _host_pinned_paths()
        remaining()
        for pin in pins:
            spelling = _canonical_or_none(pin)
            if spelling is None:
                raise StageError("cannot resolve a host hook pin; refusing history cleanup")
            try:
                inside = spelling.relative_to(versions_dir)
            except ValueError:
                continue
            if inside.parts:
                keep_paths.add(versions_dir / inside.parts[0])
    refresh_pins()
    if root is not None:
        live = current_target(Path(root))
        if live is not None:
            keep_paths.add(live.resolve())

    # Directory mtime is the ordering signal, and it is approximate: writing
    # inside a tree updates it. A wrong order is bounded — the live tree, the
    # protected set, and anything without the sentinel are all excluded anyway.
    candidates = []
    for child in versions_dir.iterdir():
        remaining()
        if (not _is_indirection(child) and child.resolve().parent == versions_dir
                and _is_prunable(child, timeout=remaining())):
            candidates.append(child)
    candidates.sort(key=lambda p: p.stat().st_mtime)
    doomed = candidates[: max(len(candidates) - max(keep, 0), 0)]

    removed = []
    for tree in doomed:
        if before_remove is not None:
            before_remove()
        refresh_pins()
        if tree.resolve() in keep_paths:
            continue
        # Recheck boundaries at the destructive operation, under the caller's
        # update locks. Never follow a newly introduced directory junction.
        if (_is_indirection(tree) or tree.resolve().parent != versions_dir or not _is_version_tree(tree)
                or (root is not None and current_target(Path(root)) == tree)):
            raise StageError(f"version changed during history cleanup: {tree}")
        try:
            ignored = _git("ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                           cwd=tree, timeout=remaining()).split("\0")
            # Runtime bytecode is disposable; ignored user data is not. Require
            # a tracked source file for each permitted __pycache__ artifact.
            tracked = set(_git("ls-files", "-z", cwd=tree, timeout=remaining()).split("\0")) if any(ignored) else set()
            for item in filter(None, ignored):
                p = Path(item)
                name = re.fullmatch(r"(.+)\.cpython-\d+(?:\.opt-\d+)?\.pyc", p.name)
                source = (p.parent.parent / (name[1] + ".py")).as_posix() if name else ""
                if p.parent.name != "__pycache__" or source not in tracked:
                    raise StageError(f"ignored local data retained: {item}")
            remaining()
            if before_remove is not None:
                before_remove()
            refresh_pins()
            if tree.resolve() in keep_paths:
                continue
            if _is_indirection(tree) or tree.resolve().parent != versions_dir:
                raise StageError(f"version path changed before removal: {tree}")
            _git("worktree", "remove", str(tree), cwd=Path(repo), timeout=30)
        except StageError as exc:
            progress["skipped"].append({"path": str(tree), "reason": str(exc)})
            if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                raise  # Stop after a timed-out command; never force a partial tree.
            continue
        if not tree.exists():
            removed.append(tree)
            progress["removed"].append(str(tree))
    return tuple(removed)
