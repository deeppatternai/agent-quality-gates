#!/usr/bin/env python3
"""Install managed Agent Quality Gates support for Cursor.

The installer writes only exact AQG-owned skill links or directories, the
project rule ``.cursor/rules/aqg.mdc``, and hook entries whose command
references the AQG Cursor adapter. Existing Cursor configuration is merged and
backed up first.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from aqg_skill_install import skill_link_source, same_skill_source
except ModuleNotFoundError:
    from scripts.aqg_skill_install import skill_link_source, same_skill_source

try:
    from _aqg_backup import BackupSession, migrate_legacy
except ModuleNotFoundError:
    from scripts._aqg_backup import BackupSession, migrate_legacy


# Hook configuration must retain the swappable entrance used to run installer.
REPO_ROOT = Path(__file__).absolute().parent.parent
ADAPTER = REPO_ROOT / "scripts" / "cursor_aqg_hook.py"
MANAGED_MARKER = ".aqg-cursor-managed.json"
LINK_MARKER_DIR = "managed-links"
LEGACY_LINK_MARKER_DIR = ".aqg-cursor-managed-links"
MANAGER = "aqg-cursor-support"
RULE_MARKER = "<!-- AQG CURSOR MANAGED RULE: do not edit generated content -->"
RULE_NAME = "aqg.mdc"
HOOK_SENTINEL = "cursor_aqg_hook.py"
LEGACY_AQG_HOOK_FRAGMENTS = (
    "sessionstart_preflight.sh",
    "agent-packs/claude-code/hooks",
    "agent-packs\\claude-code\\hooks",
    "install_aqg_hooks.py",
)
HOOK_EVENTS = ("sessionStart", "preToolUse", "postToolUse", "preCompact", "stop")
HOOKS_BACKUP_SUFFIX = ".aqg-cursor.bak"  # legacy adjacent suffix, migrated into the central store
LEGACY_BACKUP_DIR = "aqg-backups"  # legacy owned dir, migrated into the central store
CLIENT_ID = "cursor"

# Active central-store session for the current apply/uninstall run.
_BACKUP: "BackupSession | None" = None


def _skill_sources() -> list[Path]:
    return sorted(
        path
        for path in (REPO_ROOT / "skills").glob("aqg-*")
        if path.is_dir() and (path / "SKILL.md").is_file()
    )


def _atomic_write(path: Path, text: str) -> None:
    _refuse_symlink_path(path, "write target")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.aqg-", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _tree_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"refusing to hash Cursor skill through symlink or non-directory: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"refusing to hash Cursor skill through symlink: {path}")
        if not path.is_file():
            continue
        if path.name == MANAGED_MARKER:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _marker_for(source: Path, mode: str) -> dict[str, object]:
    marker: dict[str, object] = {
        "manager": MANAGER,
        "schema_version": 2,
        "skill": source.name,
        "install_mode": mode,
    }
    if mode == "link":
        marker["source_path"] = str(skill_link_source(source))
    else:
        marker["source_digest"] = _tree_digest(source)
    return marker


def _is_windows_host() -> bool:
    return os.name == "nt"


def _is_windows_junction(path: Path) -> bool:
    if not _is_windows_host() or path.is_symlink():
        return False
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return bool(isjunction(path))

    class FileTime(ctypes.Structure):
        _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))

    class FindData(ctypes.Structure):
        _fields_ = (
            ("attributes", ctypes.c_uint32),
            ("creation_time", FileTime),
            ("access_time", FileTime),
            ("write_time", FileTime),
            ("size_high", ctypes.c_uint32),
            ("size_low", ctypes.c_uint32),
            ("reparse_tag", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32),
            ("file_name", ctypes.c_wchar * 260),
            ("alternate_file_name", ctypes.c_wchar * 14),
        )

    kernel32 = ctypes.windll.kernel32
    find_first = kernel32.FindFirstFileW
    find_first.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(FindData))
    find_first.restype = ctypes.c_void_p
    find_close = kernel32.FindClose
    find_close.argtypes = (ctypes.c_void_p,)
    find_close.restype = ctypes.c_int
    data = FindData()
    handle = find_first(str(path), ctypes.byref(data))
    if handle == ctypes.c_void_p(-1).value:
        return False
    find_close(handle)
    return bool(data.attributes & 0x0010 and data.attributes & 0x0400 and data.reparse_tag == 0xA0000003)


def _is_link_install(path: Path) -> bool:
    return path.is_symlink() or _is_windows_junction(path)


def _normalize_resolved_path(path: Path, *, strict: bool) -> str:
    if strict:
        path.resolve(strict=True)
    value = os.path.normpath(os.path.realpath(path))
    if _is_windows_host():
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        value = os.path.normcase(value)
    return value


def _same_resolved_path(left: Path, right: Path, *, strict: bool) -> bool:
    try:
        return _normalize_resolved_path(left, strict=strict) == _normalize_resolved_path(right, strict=strict)
    except OSError:
        return False


def _link_marker_path(target: Path) -> Path:
    return target.parent.parent / LINK_MARKER_DIR / f"{target.name}.json"


def _legacy_link_marker_path(target: Path) -> Path:
    return target.parent / LEGACY_LINK_MARKER_DIR / f"{target.name}.json"


def _read_marker_file(marker: Path) -> dict[str, object] | None:
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get("manager") == MANAGER else None


def _read_link_marker(target: Path) -> dict[str, object] | None:
    return _read_marker_file(_link_marker_path(target)) or _read_marker_file(
        _legacy_link_marker_path(target)
    )


def _read_marker(target: Path) -> dict[str, object] | None:
    if _is_link_install(target):
        return _read_link_marker(target)
    return _read_marker_file(target / MANAGED_MARKER)


def _remove_marker_if_managed(marker: Path) -> bool:
    if _read_marker_file(marker) is None:
        return False
    marker.unlink()
    return True


def _cleanup_legacy_link_marker_dir(cursor_root: Path) -> bool:
    legacy_dir = cursor_root / "skills" / LEGACY_LINK_MARKER_DIR
    if not legacy_dir.exists() and not legacy_dir.is_symlink():
        return False
    if legacy_dir.is_symlink() or not legacy_dir.is_dir():
        print(f"WARNING: legacy Cursor marker path is not a directory; kept: {legacy_dir}", file=sys.stderr)
        return False
    changed = False
    for child in sorted(legacy_dir.iterdir()):
        if _remove_marker_if_managed(child):
            changed = True
    remaining = list(legacy_dir.iterdir())
    if not remaining:
        legacy_dir.rmdir()
        return True
    print(
        f"WARNING: legacy Cursor marker directory contains non-AQG file(s); kept: {legacy_dir}",
        file=sys.stderr,
    )
    return changed


def _cleanup_managed_link_marker_dir(cursor_root: Path) -> bool:
    marker_dir = cursor_root / LINK_MARKER_DIR
    if not marker_dir.exists() and not marker_dir.is_symlink():
        return False
    if marker_dir.is_symlink() or not marker_dir.is_dir():
        print(f"WARNING: Cursor managed marker path is not a directory; kept: {marker_dir}", file=sys.stderr)
        return False
    changed = False
    for child in sorted(marker_dir.iterdir()):
        if _remove_marker_if_managed(child):
            changed = True
    remaining = list(marker_dir.iterdir())
    if not remaining:
        marker_dir.rmdir()
        return True
    print(
        f"WARNING: Cursor managed marker directory contains non-AQG file(s); kept: {marker_dir}",
        file=sys.stderr,
    )
    return changed


def _remove_link_markers(target: Path) -> bool:
    changed = False
    for marker in (_link_marker_path(target), _legacy_link_marker_path(target)):
        changed = _remove_marker_if_managed(marker) or changed
    return changed


def _marker_matches_expected(marker: dict[str, object] | None, expected: dict[str, object]) -> bool:
    if marker is None:
        return False
    if expected.get("install_mode") != "link":
        return marker == expected
    for key in ("manager", "schema_version", "skill", "install_mode"):
        if marker.get(key) != expected.get(key):
            return False
    source_path = marker.get("source_path")
    expected_source = expected.get("source_path")
    if not isinstance(source_path, str) or not isinstance(expected_source, str):
        return False
    return same_skill_source(Path(source_path), Path(expected_source))


def _sync_link_marker(target: Path, expected: dict[str, object]) -> bool:
    marker_text = json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
    marker_path = _link_marker_path(target)
    changed = True
    if marker_path.is_file() and not marker_path.is_symlink():
        changed = marker_path.read_text(encoding="utf-8") != marker_text
    if changed:
        _atomic_write(marker_path, marker_text)
    return _remove_marker_if_managed(_legacy_link_marker_path(target)) or changed


def _refuse_symlink_path(path: Path, label: str) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink():
                raise RuntimeError(f"refusing to use {label} through symlink: {candidate}")


def _migrate_legacy_backups(cursor_root: Path, *, scope: str, project_root: Path | None) -> None:
    """Fold any legacy in-place backups (owned dir + adjacent .bak) into the central store."""
    items: list[tuple[Path, str]] = []
    legacy_owned = cursor_root / LEGACY_BACKUP_DIR
    if legacy_owned.is_dir() and not legacy_owned.is_symlink():
        items.append((legacy_owned, "_legacy/aqg-backups"))
    for path in sorted(cursor_root.rglob("*" + HOOKS_BACKUP_SUFFIX + "*")):
        if legacy_owned in path.parents:
            continue  # already covered by the owned-dir migration above
        if path.is_file() and not path.is_symlink():
            items.append((path, "_legacy/adjacent/" + path.relative_to(cursor_root).as_posix()))
    if items:
        migrate_legacy(
            CLIENT_ID,
            cursor_root,
            items,
            scope=scope,
            project_root=project_root,
            installer="install_cursor_support.py",
            aqg_root=REPO_ROOT,
        )


def _open_backups(cursor_root: Path, args: argparse.Namespace) -> None:
    """Start a central-store session for this run, migrating legacy backups first."""
    global _BACKUP
    scope = args.scope
    project_root = (
        Path(args.project_root).expanduser().resolve() if scope == "project" else None
    )
    _migrate_legacy_backups(cursor_root, scope=scope, project_root=project_root)
    _BACKUP = BackupSession(
        CLIENT_ID,
        cursor_root,
        scope=scope,
        project_root=project_root,
        installer="install_cursor_support.py",
        aqg_root=REPO_ROOT,
    )


def _close_backups(ok: bool) -> None:
    """Finalize the run's session: keep+prune on success, discard on failure."""
    global _BACKUP
    session = _BACKUP
    _BACKUP = None
    if session is None:
        return
    if ok:
        session.close()
        session.gc()
    else:
        session.discard()


def _backup_adjacent(path: Path) -> Path | None:
    if _BACKUP is None:  # defensive: apply/uninstall always open a session first
        raise RuntimeError("internal error: backup session not open")
    _refuse_symlink_path(path, "backup source")
    return _BACKUP.backup(path)


def _backup_owned(path: Path, cursor_root: Path) -> Path | None:
    if _BACKUP is None:  # defensive: apply/uninstall always open a session first
        raise RuntimeError("internal error: backup session not open")
    if path.is_symlink():
        raise RuntimeError(f"refusing to back up Cursor asset through symlink: {path}")
    return _BACKUP.backup(path)


def _is_current_managed_link(target: Path, source: Path, expected: dict[str, object]) -> bool:
    marker = _read_marker(target)
    if not _marker_matches_expected(marker, expected):
        return False
    return _same_resolved_path(target, source, strict=True)


def _create_windows_junction(source: Path, target: Path) -> bool:
    if not _is_windows_host():
        return False
    cmd = shutil.which("cmd.exe") or shutil.which("cmd")
    if cmd is None:
        return False
    completed = subprocess.run(
        [cmd, "/c", "mklink", "/J", str(target), str(source)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0 and _is_windows_junction(target)


def _create_link(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source, target_is_directory=True)
        return
    except OSError:
        if not _create_windows_junction(source, target):
            raise


def _remove_managed_link(target: Path) -> None:
    if target.is_symlink():
        target.unlink()
    elif _is_windows_junction(target):
        os.rmdir(target)
    else:
        raise RuntimeError(f"refusing to remove non-link Cursor skill as link: {target}")
    _remove_link_markers(target)


def _install_skill(source: Path, skills_root: Path, cursor_root: Path, mode: str) -> bool:
    target = skills_root / source.name
    expected = _marker_for(source, mode)
    backup: Path | None = None
    target_present = target.exists() or target.is_symlink()
    if target_present:
        marker = _read_marker(target)
        if marker is None:
            raise RuntimeError(f"refusing to overwrite existing Cursor skill: {target}")
        if mode == "link":
            if _is_link_install(target) and _is_current_managed_link(target, source, expected):
                return _sync_link_marker(target, expected)
        elif (
            not _is_link_install(target)
            and target.is_dir()
            and marker == expected
            and _tree_digest(target) == expected["source_digest"]
        ):
            return False
        if _is_link_install(target):
            _remove_managed_link(target)
        else:
            backup = _backup_owned(target, cursor_root)
            shutil.rmtree(target)

    skills_root.mkdir(parents=True, exist_ok=True)
    if mode == "link":
        marker_text = json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(_link_marker_path(target), marker_text)
        try:
            _create_link(skill_link_source(source), target)
        except OSError:
            _remove_link_markers(target)
            if backup is not None and not target.exists():
                shutil.copytree(backup, target)
            raise
        return True

    temp_target = Path(tempfile.mkdtemp(prefix=f".{source.name}.aqg-", dir=skills_root))
    try:
        shutil.rmtree(temp_target)
        shutil.copytree(source, temp_target)
        _atomic_write(
            temp_target / MANAGED_MARKER,
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
        )
        try:
            os.replace(temp_target, target)
        except OSError:
            if backup is not None and not target.exists():
                shutil.copytree(backup, target)
            raise
    finally:
        if temp_target.exists():
            shutil.rmtree(temp_target, ignore_errors=True)
    return True


def _render_project_rule() -> str:
    template = (REPO_ROOT / "examples" / "aqg-codex-agents.example.md").read_text(
        encoding="utf-8"
    )
    heading = "## Agent Quality Gates (AQG) engineering discipline"
    start = template.find(heading)
    if start < 0:
        raise RuntimeError(f"AQG rule heading missing from template: {heading}")
    # A literal <AQG_ROOT> in a rules file is a dangling pointer — nothing expands
    # it there, so the one line leading to the authoritative criteria would lead
    # nowhere. Resolve it against the checkout actually being installed from.
    # This rule is a PROJECT file and is not gitignored, so whatever lands here
    # is committed and read on every teammate's machine. A local checkout path
    # would be wrong for all of them; the documented default install location is
    # the same everywhere.
    body = template[start:].rstrip().replace(
        "<AQG_ROOT>", "~/.deeppattern/agent-quality-gates"
    )
    return (
        "---\n"
        'description: "Agent Quality Gates engineering discipline"\n'
        "alwaysApply: true\n"
        "---\n\n"
        f"{RULE_MARKER}\n\n"
        f"{body}\n"
    )


def _install_rule(cursor_root: Path) -> bool:
    rule_path = cursor_root / "rules" / RULE_NAME
    _refuse_symlink_path(rule_path, "Cursor rule")
    rendered = _render_project_rule()
    if rule_path.exists():
        current = rule_path.read_text(encoding="utf-8")
        if RULE_MARKER not in current:
            raise RuntimeError(f"refusing to overwrite existing Cursor rule: {rule_path}")
        if current == rendered:
            return False
        _backup_owned(rule_path, cursor_root)
    _atomic_write(rule_path, rendered)
    return True


def _quoted_command(event: str, aqg_root: Path | None = None) -> str:
    root = aqg_root if aqg_root is not None else REPO_ROOT
    args = [
        str(Path(sys.executable).resolve()),
        str((root / "scripts/cursor_aqg_hook.py").absolute()) if aqg_root is not None else str(ADAPTER.absolute()),
        event,
        "--aqg-root",
        str(root.absolute()),
    ]
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    import shlex

    return " ".join(shlex.quote(arg) for arg in args)


def _hook_specs(aqg_root: Path | None = None) -> dict[str, dict[str, object]]:
    specs: dict[str, dict[str, object]] = {}
    for event in HOOK_EVENTS:
        spec: dict[str, object] = {"command": _quoted_command(event, aqg_root), "timeout": 30}
        if event == "sessionStart":
            spec["timeout"] = 45
        if event == "preToolUse":
            spec.update({"matcher": "Shell|Write", "failClosed": True})
        elif event == "postToolUse":
            spec["matcher"] = "Write"
        elif event == "stop":
            spec["loop_limit"] = 1
        specs[event] = spec
    return specs


def _read_hooks(path: Path) -> dict[str, object]:
    _refuse_symlink_path(path, "Cursor hooks")
    if not path.exists():
        return {"version": 1, "hooks": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid Cursor hooks JSON at {path}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid Cursor hooks JSON object at {path}")
    hooks = value.get("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"invalid Cursor hooks block at {path}")
    for event, entries in hooks.items():
        if not isinstance(event, str) or not isinstance(entries, list):
            raise RuntimeError(f"invalid Cursor hooks event at {path}")
    return value


def _is_managed_hook(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    command = str(value.get("command", ""))
    normalized = command.replace("\\", "/")
    return HOOK_SENTINEL in command or any(
        fragment.replace("\\", "/") in normalized for fragment in LEGACY_AQG_HOOK_FRAGMENTS
    )


def _merge_hooks(path: Path) -> bool:
    config = _read_hooks(path)
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"invalid Cursor hooks block at {path}")
    specs = _hook_specs()
    changed = False
    for event in list(hooks):
        if event in specs:
            continue
        entries = hooks[event]
        if not isinstance(entries, list):
            raise RuntimeError(f"invalid Cursor hooks event {event} at {path}")
        filtered = [entry for entry in entries if not _is_managed_hook(entry)]
        if filtered != entries:
            hooks[event] = filtered
            changed = True
    for event, spec in specs.items():
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise RuntimeError(f"invalid Cursor hooks event {event} at {path}")
        filtered = [entry for entry in entries if not _is_managed_hook(entry)]
        replacement = filtered + [spec]
        if replacement != entries:
            hooks[event] = replacement
            changed = True
    if config.get("version") != 1:
        config["version"] = 1
        changed = True
    if not changed:
        return False
    if path.exists():
        _backup_adjacent(path)
    _atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return True


def _cursor_root(args: argparse.Namespace) -> Path:
    if args.scope == "user":
        return args.home.expanduser().resolve() / ".cursor"
    if args.project_root is None:
        raise RuntimeError("--project-root is required for --scope project")
    return args.project_root.expanduser().resolve() / ".cursor"


def _check_collisions(cursor_root: Path, *, project_scope: bool) -> None:
    for source in _skill_sources():
        target = cursor_root / "skills" / source.name
        if (target.exists() or target.is_symlink()) and _read_marker(target) is None:
            raise RuntimeError(f"refusing to overwrite existing Cursor skill: {target}")
    if project_scope:
        rule = cursor_root / "rules" / RULE_NAME
        _refuse_symlink_path(rule, "Cursor rule")
        if rule.exists() and RULE_MARKER not in rule.read_text(encoding="utf-8"):
            raise RuntimeError(f"refusing to overwrite existing Cursor rule: {rule}")


def _apply(args: argparse.Namespace) -> int:
    if not ADAPTER.is_file():
        raise RuntimeError(f"Cursor hook adapter missing: {ADAPTER}")
    cursor_root = _cursor_root(args)
    _check_collisions(cursor_root, project_scope=args.scope == "project")
    if not args.no_hooks:
        _read_hooks(cursor_root / "hooks.json")
    changed = False
    for source in _skill_sources():
        changed = _install_skill(source, cursor_root / "skills", cursor_root, args.mode) or changed
    changed = _cleanup_legacy_link_marker_dir(cursor_root) or changed
    if args.scope == "project":
        changed = _install_rule(cursor_root) or changed
    if not args.no_hooks:
        changed = _merge_hooks(cursor_root / "hooks.json") or changed
    print(f"OK: AQG Cursor {args.scope} support {'updated' if changed else 'already current'} at {cursor_root}")
    if args.scope == "user":
        print("INFO: Cursor User Rules are UI-managed; no undocumented user rule file was written")
    if args.no_hooks:
        print("INFO: Cursor lifecycle hooks skipped by --no-hooks")
    return 0


def _verify(args: argparse.Namespace) -> int:
    cursor_root = _cursor_root(args)
    problems: list[str] = []
    for source in _skill_sources():
        target = cursor_root / "skills" / source.name
        expected = _marker_for(source, args.mode)
        marker = _read_marker(target) if target.exists() or target.is_symlink() else None
        if args.mode == "link":
            current = _is_link_install(target) and _is_current_managed_link(target, source, expected)
        else:
            current = (
                not target.is_symlink()
                and target.is_dir()
                and marker == expected
                and _tree_digest(target) == expected["source_digest"]
            )
        if not current:
            problems.append(f"skill drift/missing: {source.name}")
    if args.scope == "project":
        rule = cursor_root / "rules" / RULE_NAME
        if rule.is_symlink() or not rule.is_file() or rule.read_text(encoding="utf-8") != _render_project_rule():
            problems.append("project rule drift/missing: aqg.mdc")
    if not args.no_hooks:
        try:
            config = _read_hooks(cursor_root / "hooks.json")
            hooks = config.get("hooks", {})
            if not isinstance(hooks, dict):
                raise RuntimeError(f"invalid Cursor hooks block at {cursor_root / 'hooks.json'}")
            for event, expected in _hook_specs().items():
                managed = [entry for entry in hooks.get(event, []) if _is_managed_hook(entry)]
                if managed != [expected]:
                    problems.append(f"hook drift/missing: {event}")
        except (AssertionError, RuntimeError) as exc:
            problems.append(str(exc))
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(f"OK: verified AQG Cursor {args.scope} support at {cursor_root}")
    return 0


def _has_managed_install(cursor_root: Path, *, project_scope: bool) -> bool:
    if any(_read_marker(path) is not None for path in (cursor_root / "skills").glob("aqg-*")):
        return True
    if any(
        _read_marker_file(path) is not None
        for path in (
            *(cursor_root / LINK_MARKER_DIR).glob("aqg-*.json"),
            *(cursor_root / "skills" / LEGACY_LINK_MARKER_DIR).glob("aqg-*.json"),
        )
    ):
        return True
    rule = cursor_root / "rules" / RULE_NAME
    if project_scope and not rule.is_symlink() and rule.is_file():
        try:
            if RULE_MARKER in rule.read_text(encoding="utf-8"):
                return True
        except OSError:
            pass
    try:
        config = _read_hooks(cursor_root / "hooks.json")
    except RuntimeError:
        return False
    hooks = config.get("hooks", {})
    return isinstance(hooks, dict) and any(
        _is_managed_hook(entry) for entries in hooks.values() for entry in entries
    )


def _uninstall(args: argparse.Namespace) -> int:
    cursor_root = _cursor_root(args)
    changed = False
    skills_root = cursor_root / "skills"
    for target in list(skills_root.glob("aqg-*")):
        marker = _read_marker(target) if target.exists() or target.is_symlink() else None
        if marker is not None and _is_link_install(target):
            _remove_managed_link(target)
            changed = True
        elif marker is not None and target.is_dir():
            _backup_owned(target, cursor_root)
            shutil.rmtree(target)
            changed = True
    changed = _cleanup_managed_link_marker_dir(cursor_root) or changed
    changed = _cleanup_legacy_link_marker_dir(cursor_root) or changed
    if args.scope == "project":
        rule = cursor_root / "rules" / RULE_NAME
        if rule.is_symlink():
            raise RuntimeError(f"refusing to uninstall Cursor rule through symlink: {rule}")
        if rule.is_file() and RULE_MARKER in rule.read_text(encoding="utf-8"):
            _backup_owned(rule, cursor_root)
            rule.unlink()
            changed = True
    hooks_path = cursor_root / "hooks.json"
    if hooks_path.exists() or hooks_path.is_symlink():
        config = _read_hooks(hooks_path)
        hooks = config.get("hooks", {})
        if not isinstance(hooks, dict):
            raise RuntimeError(f"invalid Cursor hooks block at {hooks_path}")
        hook_changed = False
        for event in list(hooks):
            entries = hooks[event]
            filtered = [entry for entry in entries if not _is_managed_hook(entry)]
            if filtered != entries:
                hook_changed = True
                if filtered:
                    hooks[event] = filtered
                else:
                    del hooks[event]
        if hook_changed:
            _backup_adjacent(hooks_path)
            _atomic_write(hooks_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
            changed = True
    print(f"OK: AQG Cursor {args.scope} support {'uninstalled' if changed else 'not installed'} at {cursor_root}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install AQG support for Cursor")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true", help="install managed Cursor support")
    action.add_argument("--verify", action="store_true", help="verify managed Cursor support")
    action.add_argument("--uninstall", action="store_true", help="remove managed Cursor support")
    action.add_argument("--is-installed", action="store_true", help="exit 0 iff managed support is present")
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--mode", choices=("link", "copy"), default="link")
    parser.add_argument("--no-hooks", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cursor_root = _cursor_root(args)
        if args.is_installed:
            return 0 if _has_managed_install(cursor_root, project_scope=args.scope == "project") else 1
        if args.verify:
            return _verify(args)
        # apply / uninstall mutate managed files -> run inside a central-store session
        action = _apply if args.apply else _uninstall
        _open_backups(cursor_root, args)
        ok = False
        try:
            result = action(args)
            ok = result == 0
            return result
        finally:
            _close_backups(ok)
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
