#!/usr/bin/env python3
r"""Centralized backup store shared by every AQG installer.

Before an installer overwrites the user's pre-existing config / assets it stashes
them here, so an ``--uninstall`` or a manual recovery can restore the original.
All installers write to one location instead of scattering ``.bak`` files and
``aqg-backups/`` dirs across each client root. Layout::

    <base>/<client>/<scope>/<UTC>-<pid>[-N]/
        manifest.json
        <original relative path>          # file (copy2) or dir (copytree)

* ``base``  = ``AQG_BACKUP_DIR`` (verbatim)
            | ``dirname(aqg_root or $AQG_ROOT)/aqg-backups``
            | ``$HOME/.deeppattern/aqg-backups``
* ``scope`` = ``"user"`` | ``"project-<12hex of the abs project root>"``

Because the backup no longer lives under the client root, ``manifest.json``
records the absolute ``source_root`` so :func:`restore` can map each entry back.

Shared safety invariants, implemented once here so the installers don't each
re-roll them:

* **never-clobber** — a new run (or a within-run entry) never overwrites a prior
  one; a ``.N`` suffix finds the first free run directory.
* **symlink refusal** — never back up *through* a symlink and never write
  *through* one (atomic ``os.replace`` onto a temp file in the same dir).
* **containment** — every write stays inside its run directory / the base.
* **Windows long paths** — deep trees use the ``\\?\`` extended-length prefix.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

__all__ = [
    "BackupError",
    "BackupSession",
    "backup_base",
    "scope_key",
    "migrate_legacy",
    "gc",
    "list_runs",
    "latest_run",
    "remove_run",
    "restore",
    "git_config_entries",
]

SCHEMA = "aqg-backup/1"
DEFAULT_KEEP = 10
_STAMP_FMT = "%Y%m%dT%H%M%SZ"


class BackupError(RuntimeError):
    """Raised on a refused or failed backup / restore operation."""


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------

def _win_long(path: Path) -> str:
    r"""OS path string safe for deep trees on Windows.

    Windows' default MAX_PATH is 260; mirroring a deep skill tree under the
    ``<UTC>-<pid>`` prefix can exceed it. The ``\\?\`` extended-length prefix
    lifts the limit. No-op on POSIX and for already-prefixed paths.
    """
    if os.name != "nt":
        return str(path)
    s = os.fspath(path)
    if s.startswith("\\\\?\\"):
        return s
    abs_s = os.path.abspath(s)
    if abs_s.startswith("\\\\"):  # UNC \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + abs_s[2:]
    return "\\\\?\\" + abs_s


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _contained(path: Path, root: Path) -> bool:
    """True if *path* resolved stays within *root* resolved (symlink-safe)."""
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    try:
        try:
            return resolved.is_relative_to(root_resolved)
        except AttributeError:  # Python < 3.9
            resolved.relative_to(root_resolved)
            return True
    except (ValueError, OSError):
        return False


def _refuse_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise BackupError(f"refusing to use {label} through symlink: {path}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(_win_long(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_digest(root: Path) -> str:
    """Order-independent content digest of a directory tree (files + symlinks)."""
    h = hashlib.sha256()
    base = _resolve(root)
    for p in sorted(base.rglob("*"), key=lambda x: x.as_posix()):
        rel = p.relative_to(base).as_posix()
        if p.is_symlink():
            try:
                target = os.readlink(p)
            except OSError:
                target = "?"
            h.update(("L:" + rel + "->" + target).encode("utf-8"))
        elif p.is_dir():
            h.update(("D:" + rel).encode("utf-8"))
        else:
            h.update(("F:" + rel + ":").encode("utf-8"))
            h.update(_sha256_file(p).encode("ascii"))
    return h.hexdigest()


def _size(path: Path) -> int:
    if path.is_dir() and not path.is_symlink():
        total = 0
        for p in path.rglob("*"):
            if p.is_file() and not p.is_symlink():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _atomic_write_bytes(dest: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Atomically write *data* to *dest*, refusing a symlink at *dest*.

    Mirrors ``install_aqg_construction_hook._atomic_write``: temp file in the
    same dir, fsync, ``fchmod`` the open fd (POSIX), then ``os.replace``.
    """
    if dest.is_symlink():
        raise BackupError(f"refusing to write through symlink: {dest}")
    fd, tmp = tempfile.mkstemp(prefix=".aqg-bk-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            try:
                os.fchmod(fh.fileno(), mode)
            except (AttributeError, OSError):  # Windows has no fchmod
                pass
        os.replace(tmp, dest)
    # aqg: top-level boundary
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _run_sort_key(run_dir: Path) -> Tuple[float, str]:
    try:
        mtime = run_dir.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, run_dir.name)


# ---------------------------------------------------------------------------
# base + scope resolution
# ---------------------------------------------------------------------------

def backup_base(aqg_root: Optional[Path] = None, *, create: bool = True) -> Path:
    """Resolve the centralized backup root.

    Precedence: ``AQG_BACKUP_DIR`` (verbatim) > ``dirname(aqg_root or
    $AQG_ROOT)/aqg-backups`` > ``$HOME/.deeppattern/aqg-backups``.
    """
    override = os.environ.get("AQG_BACKUP_DIR")
    if override:
        base = Path(override)
    else:
        root = aqg_root
        if root is None:
            env_root = os.environ.get("AQG_ROOT")
            root = Path(env_root) if env_root else None
        if root is not None:
            base = Path(root).parent / "aqg-backups"
        else:
            base = Path.home() / ".deeppattern" / "aqg-backups"
    if create:
        _refuse_symlink(base, "backup base")
        base.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
    return base


def scope_key(scope: str = "user", project_root: Optional[Path] = None) -> str:
    """Map a scope to its directory segment.

    ``"user"`` -> ``"user"``; ``"project"`` -> ``"project-<hash>"`` over the
    absolute project root; any other value is treated as an already-built key.
    """
    if scope == "user":
        return "user"
    if scope == "project":
        if project_root is None:
            raise BackupError("project scope requires project_root")
        return "project-" + _short_hash(str(_resolve(Path(project_root))))
    return scope


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

class BackupSession:
    """One install run's backups: a single ``<UTC>-<pid>`` snapshot directory.

    The run directory is created lazily on the first :meth:`backup` /
    :meth:`backup_value`, so a no-op install leaves nothing behind. Use as a
    context manager to auto-:meth:`close` on success and :meth:`discard` on
    error, or call them explicitly.
    """

    def __init__(
        self,
        client: str,
        source_root: Path,
        *,
        scope: str = "user",
        project_root: Optional[Path] = None,
        installer: str = "",
        aqg_root: Optional[Path] = None,
        origin: str = "install",
    ) -> None:
        self.client = client
        self.source_root = _resolve(Path(source_root))
        self.scope = scope_key(scope, project_root)
        self.installer = installer
        self.origin = origin
        self._aqg_root = aqg_root
        self._base = backup_base(aqg_root)
        self._run_prefix = ""
        self._run_dir: Optional[Path] = None
        self._created_utc: Optional[str] = None
        self._entries: List[dict] = []

    # -- run lifecycle ------------------------------------------------------

    @property
    def run_dir(self) -> Optional[Path]:
        return self._run_dir

    def _scope_dir(self) -> Path:
        return self._base / self.client / self.scope

    def _ensure_run(self) -> Path:
        if self._run_dir is not None:
            return self._run_dir
        scope_dir = self._scope_dir()
        scope_dir.mkdir(parents=True, exist_ok=True)
        if not _contained(scope_dir, self._base):
            raise BackupError(f"scope dir escapes backup base: {scope_dir}")
        stamp = datetime.now(timezone.utc).strftime(_STAMP_FMT)
        pid = os.getpid()
        cand = scope_dir / f"{self._run_prefix}{stamp}-{pid}"
        n = 1
        while cand.exists() or cand.is_symlink():
            cand = scope_dir / f"{self._run_prefix}{stamp}-{pid}-{n}"
            n += 1
        cand.mkdir()
        self._run_dir = cand
        self._created_utc = stamp
        return cand

    # -- backup primitives --------------------------------------------------

    def _stash(self, src: Path, rel: Path) -> Tuple[Path, str, Optional[str]]:
        if rel.is_absolute() or ".." in rel.parts or rel == Path("."):
            raise BackupError(f"invalid backup relpath: {rel}")
        run = self._ensure_run()
        dest = run / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not _contained(dest, run):
            raise BackupError(f"backup dest escapes run dir: {dest}")
        if dest.exists() or dest.is_symlink():
            raise BackupError(f"backup dest already exists (within-run duplicate): {dest}")
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(_win_long(src), _win_long(dest), symlinks=True)
            return dest, "dir", _tree_digest(src)
        shutil.copy2(_win_long(src), _win_long(dest))
        return dest, "file", _sha256_file(src)

    def backup(self, path: Path) -> Optional[Path]:
        """Stash *path* (must live under ``source_root``); no-op if it is absent.

        Returns the backup destination, or ``None`` if *path* does not exist.
        """
        path = Path(path)
        if not path.exists() and not path.is_symlink():
            return None
        _refuse_symlink(path, "backup source")
        if not _contained(path, self.source_root):
            raise BackupError(
                f"backup source escapes source_root {self.source_root}: {path}"
            )
        rel = _resolve(path).relative_to(self.source_root)
        dest, kind, sha = self._stash(path, rel)
        self._entries.append(
            {"relpath": rel.as_posix(), "kind": kind, "sha256": sha, "bytes": _size(path)}
        )
        return dest

    def backup_value(self, key: str, value: str, *, repo: Path) -> Path:
        """Stash a config *value* (e.g. a git ``core.hooksPath``) as a file.

        Restored by the caller via :func:`git_config_entries`, not :func:`restore`.
        """
        run = self._ensure_run()
        rel = Path("_values") / (key.replace("/", "_") + ".value")
        dest = run / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not _contained(dest, run):
            raise BackupError(f"value dest escapes run dir: {dest}")
        payload = (value or "").encode("utf-8")
        _atomic_write_bytes(dest, payload)
        self._entries.append(
            {
                "relpath": rel.as_posix(),
                "kind": "git-config",
                "key": key,
                "repo": str(_resolve(Path(repo))),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
        return dest

    def close(self) -> Optional[Path]:
        """Write ``manifest.json`` and return the run dir, or ``None`` if empty."""
        if self._run_dir is None:
            return None
        manifest = {
            "schema": SCHEMA,
            "client": self.client,
            "scope": self.scope,
            "source_root": str(self.source_root),
            "created_utc": self._created_utc,
            "pid": os.getpid(),
            "installer": self.installer,
            "origin": self.origin,
            "entries": self._entries,
        }
        blob = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        _atomic_write_bytes(self._run_dir / "manifest.json", blob.encode("utf-8"))
        return self._run_dir

    def discard(self) -> None:
        """Remove a partial run (failure path); safe to call when empty."""
        if self._run_dir is not None and self._run_dir.exists():
            shutil.rmtree(_win_long(self._run_dir), ignore_errors=True)
        self._run_dir = None
        self._entries = []

    def gc(self, keep: Optional[int] = None) -> List[Path]:
        """Prune this session's ``<client>/<scope>/`` to the newest ``keep`` runs."""
        return gc(self.client, scope=self.scope, keep=keep, aqg_root=self._aqg_root)

    def __enter__(self) -> "BackupSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.close()
        else:
            self.discard()
        return False


# ---------------------------------------------------------------------------
# migration / gc / restore
# ---------------------------------------------------------------------------

def migrate_legacy(
    client: str,
    source_root: Path,
    items,
    *,
    scope: str = "user",
    project_root: Optional[Path] = None,
    installer: str = "migrate_legacy",
    aqg_root: Optional[Path] = None,
) -> Optional[Path]:
    """Fold legacy in-place backups into the central store, then delete them.

    *items* is an iterable of ``(backup_path, relpath)`` where ``backup_path`` is
    the old in-place backup on disk and ``relpath`` is where its content
    originally lived relative to ``source_root`` — so the migrated run is
    :func:`restore`-able like a normal install run (``origin="legacy-inplace"``).

    Returns the migrated run dir, or ``None`` when no legacy item exists. Copies
    happen before deletes, so a copy failure never loses the original; a failed
    delete only emits a WARN (the item is re-migrated idempotently next run).
    """
    present: List[Tuple[Path, str]] = []
    for backup_path, rel in items:
        p = Path(backup_path)
        if p.exists() or p.is_symlink():
            present.append((p, str(rel)))
    if not present:
        return None

    sess = BackupSession(
        client,
        source_root,
        scope=scope,
        project_root=project_root,
        installer=installer,
        aqg_root=aqg_root,
        origin="legacy-inplace",
    )
    sess._run_prefix = "migrated-"
    to_delete: List[Path] = []
    for backup_path, rel in present:
        _refuse_symlink(backup_path, "legacy backup source")
        rel_path = Path(rel)
        dest, kind, sha = sess._stash(backup_path, rel_path)
        sess._entries.append(
            {
                "relpath": rel_path.as_posix(),
                "kind": kind,
                "sha256": sha,
                "bytes": _size(backup_path),
                "legacy_src": str(backup_path),
            }
        )
        to_delete.append(backup_path)

    run_dir = sess.close()

    for backup_path in to_delete:
        try:
            if backup_path.is_dir() and not backup_path.is_symlink():
                shutil.rmtree(_win_long(backup_path), ignore_errors=False)
            else:
                backup_path.unlink()
        except OSError as exc:
            print(
                f"WARN: migrated but could not remove legacy backup {backup_path}: {exc}",
                file=sys.stderr,
            )
    return run_dir


def _keep_from_env(keep: Optional[int]) -> int:
    if keep is not None:
        return keep
    env = os.environ.get("AQG_BACKUP_KEEP")
    if env and env.strip().lstrip("-").isdigit():
        return int(env)
    return DEFAULT_KEEP


def gc(
    client: str,
    source_root: Optional[Path] = None,
    *,
    scope: str = "user",
    project_root: Optional[Path] = None,
    keep: Optional[int] = None,
    aqg_root: Optional[Path] = None,
) -> List[Path]:
    """Prune old runs under one ``<client>/<scope>/``, newest ``keep`` kept.

    ``keep`` defaults to ``AQG_BACKUP_KEEP`` or :data:`DEFAULT_KEEP` (10);
    ``keep <= 0`` disables GC. The newest run is always retained.
    """
    keep = _keep_from_env(keep)
    if keep <= 0:
        return []
    base = backup_base(aqg_root, create=False)
    scope_dir = base / client / scope_key(scope, project_root)
    if not scope_dir.is_dir():
        return []
    runs = [d for d in scope_dir.iterdir() if d.is_dir() and not d.is_symlink()]
    runs.sort(key=_run_sort_key, reverse=True)
    removed: List[Path] = []
    for stale in runs[keep:]:
        shutil.rmtree(_win_long(stale), ignore_errors=True)
        removed.append(stale)
    return removed


def _read_manifest(run_dir: Path) -> dict:
    return json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))


def _run_origin(run_dir: Path) -> Optional[str]:
    """The ``origin`` recorded in a run's manifest (``install`` / ``legacy-inplace``)."""
    try:
        return _read_manifest(run_dir).get("origin")
    except (OSError, ValueError):
        return None


def list_runs(
    client: str,
    source_root: Optional[Path] = None,
    *,
    scope: str = "user",
    project_root: Optional[Path] = None,
    aqg_root: Optional[Path] = None,
    origins=None,
) -> List[Path]:
    """Runs (each with a ``manifest.json``) under ``<client>/<scope>/``, newest first.

    ``origins`` optionally restricts the result to runs whose manifest ``origin``
    is in the given collection (e.g. ``("install",)``); ``None`` keeps every run.
    Callers that must not let a migrated ``legacy-inplace`` run shadow a real
    ``install`` capture select with an explicit ``origins`` rather than relying on
    the ``(mtime, name)`` sort — a ``migrated-`` prefix sorts lexically *after* a
    bare timestamp, and an uninstall-time migration is the newest run of all.
    """
    base = backup_base(aqg_root, create=False)
    scope_dir = base / client / scope_key(scope, project_root)
    if not scope_dir.is_dir():
        return []
    runs = [
        d
        for d in scope_dir.iterdir()
        if d.is_dir() and not d.is_symlink() and (d / "manifest.json").is_file()
    ]
    if origins is not None:
        allowed = set(origins)
        runs = [d for d in runs if _run_origin(d) in allowed]
    runs.sort(key=_run_sort_key, reverse=True)
    return runs


def latest_run(
    client: str,
    source_root: Optional[Path] = None,
    *,
    scope: str = "user",
    project_root: Optional[Path] = None,
    aqg_root: Optional[Path] = None,
    origins=None,
) -> Optional[Path]:
    """Newest run (with a ``manifest.json``) under ``<client>/<scope>/``, or None.

    ``origins`` filters by manifest ``origin`` exactly as :func:`list_runs`.
    """
    runs = list_runs(
        client,
        source_root,
        scope=scope,
        project_root=project_root,
        aqg_root=aqg_root,
        origins=origins,
    )
    return runs[0] if runs else None


def remove_run(run_dir: Path) -> bool:
    """Delete one run directory (consume-on-restore / discard); idempotent.

    Returns ``True`` iff the run is *gone* after the call — i.e. the removal
    actually succeeded, or the run was already absent. Returns ``False`` when a
    directory that existed could not be removed (e.g. a Windows file lock), so a
    caller consuming the backup slot can surface the failure instead of silently
    claiming success while a stale run survives (external audit round-2 V2f4).
    """
    run_dir = Path(run_dir)
    if run_dir.exists() or run_dir.is_symlink():
        shutil.rmtree(_win_long(run_dir), ignore_errors=True)
    return not (run_dir.exists() or run_dir.is_symlink())


def git_config_entries(run_dir: Path) -> List[dict]:
    """Return ``[{key, value, repo}]`` for the git-config entries in *run_dir*."""
    run_dir = Path(run_dir)
    out: List[dict] = []
    for entry in _read_manifest(run_dir).get("entries", []):
        if entry.get("kind") != "git-config":
            continue
        value = (run_dir / entry["relpath"]).read_text(encoding="utf-8")
        out.append({"key": entry["key"], "value": value, "repo": entry["repo"]})
    return out


def restore(run_dir: Path, *, force: bool = False) -> List[str]:
    """Restore the file/dir entries of *run_dir* back to their ``source_root``.

    git-config entries are skipped (fetch them with :func:`git_config_entries`).
    Refuses to overwrite an existing target unless *force* is set.
    """
    run_dir = Path(run_dir)
    data = _read_manifest(run_dir)
    source_root = Path(data["source_root"])
    restored: List[str] = []
    for entry in data.get("entries", []):
        kind = entry.get("kind")
        if kind == "git-config":
            continue
        rel = entry["relpath"]
        src = run_dir / rel
        dest = source_root / rel
        if dest.is_symlink():
            raise BackupError(f"refusing to restore through symlink: {dest}")
        if dest.exists() and not force:
            raise BackupError(f"restore target exists (pass force=True): {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(_win_long(dest), ignore_errors=True)
            else:
                dest.unlink()
        if kind == "dir":
            shutil.copytree(_win_long(src), _win_long(dest), symlinks=True)
        else:
            shutil.copy2(_win_long(src), _win_long(dest))
        restored.append(str(dest))
    return restored
