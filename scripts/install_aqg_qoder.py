#!/usr/bin/env python3
"""Install AQG skills, rules, and hooks for Qoder-family clients."""

from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import json
import os
import shlex
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
    from aqg_update.rules import policy_root
except ModuleNotFoundError:
    from scripts.aqg_update.rules import policy_root

try:
    from aqg_skill_install import classify_install, create_windows_junction, remove_install
except ModuleNotFoundError:  # package import, e.g. tests loading from repo root
    from scripts.aqg_skill_install import classify_install, create_windows_junction, remove_install

try:
    from _aqg_backup import BackupError, BackupSession, migrate_legacy
except ModuleNotFoundError:
    from scripts._aqg_backup import BackupError, BackupSession, migrate_legacy


EXIT_OK = 0
EXIT_ERROR = 1
MANAGED_ID = "aqg-qoder-v1"
SKILL_MARKER = ".aqg-qoder-managed.json"
LINK_MARKER_DIR = "managed-links"
OWNERS_MARKER = ".aqg-qoder-owners.json"
RULE_MARKER = "<!-- AQG-MANAGED: aqg-qoder-v1 -->"
SETTINGS_BACKUP_SUFFIX = ".aqg-qoder.bak"  # legacy adjacent settings backup suffix (migrated to central)
LEGACY_BACKUP_DIR = ".aqg-backups"  # legacy in-place snapshot dir under client_root (migrated to central)
SKILL_INSTALL_MODES = ("link", "copy")

# Single central-store session open for the current apply/uninstall run. Qoder
# writes every managed asset (skills, rule, settings.json) under one client_root,
# so one session rooted there covers them all.
_BACKUP: "BackupSession | None" = None

PROFILES = {
    # Paths verified 2026-07-23 against docs.qoder.com/extensions/{hooks,skills}.md
    # and docs.qoder.com/en/cli/{hooks,Skills,memory}.md.
    "qoder": {"user_dir": ".qoder", "project_dir": ".qoder", "level": "partial", "cli": False},
    "qoder-cli": {"user_dir": ".qoder", "project_dir": ".qoder", "level": "full", "cli": True},
    # CN paths verified against docs.qoder.cn/user-guide/{hooks,skills,rules}.md
    # and docs.qoder.cn/cli/{hook,skills,memory}.md.
    # `qoder-cn` is Qoder CN Desktop (`Qoder CN.app`, com.qodercn.app), whose user
    # configuration root is `~/.qoder-cn`. `.lingma` is the legacy Tongyi Lingma
    # root: writing there installs 16 skills the product will never read.
    "qoder-cn": {"user_dir": ".qoder-cn", "project_dir": ".qoder-cn", "level": "partial", "cli": False},
    "qoder-cli-cn": {
        "user_dir": ".qoder-cn",
        "project_dir": ".qoder",
        "level": "full",
        "cli": True,
    },
}

HOOK_SCRIPTS = (
    "pretooluse_bash_skill_validator.sh",
    "pretooluse_secret_scan.sh",
    "posttooluse_bash_error_debugging_reminder.sh",
    "posttooluse_skill_edit_reminder.sh",
    "posttooluse_code_construction_reminder.sh",
    "posttooluse_test_quality_reminder.sh",
    "posttooluse_security_review_reminder.sh",
    "precompact_closeout_reminder.sh",
    "sessionstart_preflight.sh",
    "sessionstart_update_check.sh",
    "userpromptsubmit_handoff_mandate.sh",
    "wip_checkpoint_save.sh",
    "wip_checkpoint_recover.sh",
)
BLOCKING_HOOKS = frozenset(
    {
        "pretooluse_bash_skill_validator.sh",
        "pretooluse_secret_scan.sh",
    }
)


class InstallError(RuntimeError):
    pass


def _resolve_aqg_root(value: str | None) -> Path:
    if value:
        candidate = Path(value).expanduser().absolute()
    else:
        candidate = Path(__file__).absolute().parent.parent
    # Persist the swappable entrance in settings, not versions/<commit>.
    if not (candidate / "VERSION").is_file() or not (candidate / "skills").is_dir():
        raise InstallError(f"invalid AQG root: {candidate}")
    return candidate


def _client_root(args: argparse.Namespace) -> Path:
    profile = PROFILES[args.client]
    if args.scope == "user":
        return Path(args.home).resolve() / profile["user_dir"]
    if not args.project_root:
        raise InstallError("--project-root is required with --scope project")
    project = Path(args.project_root).resolve()
    if not project.is_dir():
        raise InstallError(f"project root does not exist: {project}")
    return project / profile["project_dir"]


def _validate_client_layout(client_root: Path) -> None:
    for path in (
        client_root,
        client_root / "skills",
        client_root / LINK_MARKER_DIR,
        client_root / "rules",
        client_root / ".aqg-backups",
    ):
        if path.is_symlink():
            raise InstallError(f"refusing to use managed path through symlink: {path}")


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise InstallError(f"refusing to read settings through symlink: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot parse settings {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"settings top-level must be a JSON object: {path}")
    hooks = data.get("hooks", {})
    if hooks is not None and not isinstance(hooks, dict):
        raise InstallError(f"settings hooks must be a JSON object: {path}")
    return data


def _read_text(path: Path, description: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InstallError(f"cannot read {description} {path}: {exc}") from exc


def _atomic_write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise InstallError(f"refusing to write through symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-aqg-qoder-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _backup(path: Path) -> Path | None:
    """Stash ``path`` into the central store for the current run; None if absent."""
    if _BACKUP is None:  # defensive: apply/uninstall always open a session first
        raise InstallError("internal error: backup session not open")
    return _BACKUP.backup(path)


def _migrate_legacy_backups(
    client_root: Path,
    settings_path: Path,
    *,
    client_id: str,
    scope: str,
    project_root: Path | None,
    aqg_root: Path,
) -> None:
    """Fold pre-central in-place backups under ``client_root`` into the central store.

    Two legacy forms existed: the ``.aqg-backups/<stamp>-pid/`` snapshot dir (skills
    and rule) and adjacent ``settings.json.aqg-qoder.bak[.N]`` files. Both migrate as
    one run and are then deleted (idempotent; symlinks are skipped, not followed)."""
    items: list[tuple[Path, str]] = []
    legacy_dir = client_root / LEGACY_BACKUP_DIR
    if legacy_dir.is_dir() and not legacy_dir.is_symlink():
        items.append((legacy_dir, "_legacy/aqg-backups"))
    pattern = settings_path.name + SETTINGS_BACKUP_SUFFIX + "*"
    for candidate in sorted(settings_path.parent.glob(pattern)):
        if candidate.is_file() and not candidate.is_symlink():
            items.append((candidate, "_legacy/" + candidate.name))
    if items:
        migrate_legacy(
            client_id,
            client_root,
            items,
            scope=scope,
            project_root=project_root,
            installer="install_aqg_qoder.py",
            aqg_root=aqg_root,
        )


def _open_backups(args: argparse.Namespace, client_root: Path, aqg_root: Path) -> None:
    """Start the central-store session for this run, migrating legacy backups first."""
    global _BACKUP
    scope = args.scope
    project_root = (
        Path(args.project_root).resolve() if scope == "project" and args.project_root else None
    )
    _migrate_legacy_backups(
        client_root,
        client_root / "settings.json",
        client_id=args.client,
        scope=scope,
        project_root=project_root,
        aqg_root=aqg_root,
    )
    _BACKUP = BackupSession(
        args.client,
        client_root,
        scope=scope,
        project_root=project_root,
        installer="install_aqg_qoder.py",
        aqg_root=aqg_root,
    )


def _close_backups(ok: bool) -> None:
    """Finalize the run session: keep+prune on success, discard a partial on failure."""
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


def _tree_digest(root: Path) -> str:
    if root.is_symlink():
        raise InstallError(f"refusing to hash skill through symlink: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise InstallError(f"refusing to hash skill through symlink: {path}")
        if not path.is_file() or path.name == SKILL_MARKER:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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


def _link_marker(path: Path) -> Path:
    return path.parent.parent / LINK_MARKER_DIR / f"{path.name}.json"


def _legacy_link_marker(path: Path) -> Path:
    return path.parent / f".{path.name}{SKILL_MARKER}"


def _read_managed_marker(marker: Path) -> dict | None:
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("managed_by") == MANAGED_ID else None


def _managed_copy_skill(path: Path) -> dict | None:
    if path.is_symlink():
        return None
    data = _read_managed_marker(path / SKILL_MARKER)
    if data is None:
        return None
    return data if data.get("mode", "copy") == "copy" else None


def _managed_link_skill(path: Path) -> dict | None:
    if not _is_link_install(path):
        return None
    data = _read_managed_marker(_link_marker(path)) or _read_managed_marker(_legacy_link_marker(path))
    if data is None:
        return None
    source = data.get("source")
    if data.get("mode") != "link" or not isinstance(source, str):
        return None
    marker_source = Path(source)
    if not marker_source.is_absolute():
        return None
    try:
        destination = _symlink_destination(path)
    except OSError:
        return None
    return data if same_skill_source(marker_source, destination) else None


def _normalize_windows_link_target(raw_text: str) -> str:
    if raw_text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw_text[8:]
    if raw_text.startswith("\\\\?\\"):
        return raw_text[4:]
    if raw_text.startswith("\\??\\UNC\\"):
        return "\\\\" + raw_text[8:]
    if raw_text.startswith("\\??\\"):
        return raw_text[4:]
    return raw_text


def _symlink_destination(path: Path) -> Path:
    if _is_windows_junction(path):
        return path.resolve(strict=False)
    raw_text = os.readlink(path)
    if os.name == "nt":
        raw_text = _normalize_windows_link_target(raw_text)
    raw = Path(raw_text)
    if not raw.is_absolute():
        raw = path.parent / raw
    return raw.resolve(strict=False)


def _managed_skill(path: Path) -> dict | None:
    if _is_link_install(path):
        return _managed_link_skill(path)
    return _managed_copy_skill(path)


def _link_points_to(path: Path, source: Path) -> bool:
    return _same_resolved_path(path, source, strict=True)


def _remove_marker_if_managed(marker: Path) -> bool:
    if _read_managed_marker(marker) is None:
        return False
    marker.unlink()
    return True


def _remove_link_markers(target: Path) -> bool:
    changed = False
    for marker in (_link_marker(target), _legacy_link_marker(target)):
        changed = _remove_marker_if_managed(marker) or changed
    return changed


def _sync_link_marker(target: Path, marker_text: str) -> bool:
    marker_path = _link_marker(target)
    changed = True
    if marker_path.is_file() and not marker_path.is_symlink():
        changed = _read_text(marker_path, "managed skill marker") != marker_text
    if changed:
        _atomic_write(marker_path, marker_text)
    return _remove_marker_if_managed(_legacy_link_marker(target)) or changed


def _cleanup_legacy_link_markers(client_root: Path) -> bool:
    skills_dir = client_root / "skills"
    if not skills_dir.is_dir():
        return False
    changed = False
    for marker in sorted(skills_dir.glob(f".aqg-*{SKILL_MARKER}")):
        if _remove_marker_if_managed(marker):
            changed = True
        else:
            print(f"WARNING: legacy Qoder marker is not AQG-managed; kept: {marker}", file=sys.stderr)
    return changed


def _cleanup_managed_link_markers(client_root: Path) -> bool:
    marker_dir = client_root / LINK_MARKER_DIR
    if not marker_dir.exists() and not marker_dir.is_symlink():
        return False
    if marker_dir.is_symlink() or not marker_dir.is_dir():
        print(f"WARNING: Qoder managed marker path is not a directory; kept: {marker_dir}", file=sys.stderr)
        return False
    changed = False
    for marker in sorted(marker_dir.iterdir()):
        if marker.name == OWNERS_MARKER:
            continue
        if _remove_marker_if_managed(marker):
            changed = True
    remaining = list(marker_dir.iterdir())
    if not remaining:
        marker_dir.rmdir()
        return True
    print(f"WARNING: Qoder managed marker directory contains non-AQG file(s); kept: {marker_dir}", file=sys.stderr)
    return changed


def _owners_path(client_root: Path) -> Path:
    return client_root / LINK_MARKER_DIR / OWNERS_MARKER


def _read_owners(client_root: Path) -> tuple[list[str], bool]:
    """Read the shared-root ownership ledger, distinguishing absent from invalid."""
    path = _owners_path(client_root)
    if not path.exists() and not path.is_symlink():
        return [], False
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"refusing invalid Qoder ownership ledger: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot parse Qoder ownership ledger {path}: {exc}") from exc
    owners = data.get("owners") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("managed_by") != MANAGED_ID
        or data.get("schema_version") != 1
        or not isinstance(owners, list)
        or any(not isinstance(owner, str) or owner not in PROFILES for owner in owners)
    ):
        raise InstallError(f"refusing invalid Qoder ownership ledger: {path}")
    return sorted(set(owners)), True


def _write_owners(client_root: Path, owners: list[str]) -> bool:
    path = _owners_path(client_root)
    normalized = sorted(set(owners))
    if not normalized:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise InstallError(f"refusing invalid Qoder ownership ledger: {path}")
            _backup(path)
            path.unlink()
            if path.parent.is_dir() and not any(path.parent.iterdir()):
                path.parent.rmdir()
            return True
        return False
    payload = json.dumps(
        {"managed_by": MANAGED_ID, "schema_version": 1, "owners": normalized},
        indent=2,
    ) + "\n"
    current = path.read_text(encoding="utf-8") if path.is_file() and not path.is_symlink() else None
    if current == payload:
        return False
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise InstallError(f"refusing invalid Qoder ownership ledger: {path}")
        _backup(path)
    _atomic_write(path, payload)
    return True


def _shared_profile_ids(client: str, scope: str) -> tuple[str, ...]:
    profile = PROFILES[client]
    root_key = "user_dir" if scope == "user" else "project_dir"
    return tuple(
        other
        for other, candidate in PROFILES.items()
        if candidate[root_key] == profile[root_key]
    )


def _read_root_owners(
    client_root: Path, client: str, scope: str
) -> tuple[list[str], bool]:
    owners, present = _read_owners(client_root)
    allowed = set(_shared_profile_ids(client, scope))
    if present and any(owner not in allowed for owner in owners):
        raise InstallError(
            f"Qoder ownership ledger contains an owner outside this {scope} root: "
            f"{_owners_path(client_root)}"
        )
    return owners, present


def _legacy_shared_owners(
    aqg_root: Path,
    client_root: Path,
    client: str,
    settings: dict,
    *,
    scope: str,
) -> tuple[str, ...]:
    """Infer only siblings needed to explain legacy managed hook differences."""
    _owners, present = _read_root_owners(client_root, client, scope)
    if present:
        return ()

    def spec_keys(profile: str) -> set[tuple[str, str, str | None]]:
        return {
            (event, block.get("matcher", ""), _script_in_command(hook["command"]))
            for event, blocks in _hook_specs(
                aqg_root, bool(PROFILES[profile]["cli"])
            ).items()
            for block in blocks
            for hook in block["hooks"]
        }

    actual = {
        (event, block.get("matcher", ""), _script_in_command(hook.get("command", "")))
        for event, blocks in (settings.get("hooks", {}) or {}).items()
        if isinstance(blocks, list)
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("hooks", []), list)
        for hook in block.get("hooks", [])
        if isinstance(hook, dict) and _owned_command(hook.get("command"))
    }
    unexplained = actual - spec_keys(client)
    if not unexplained:
        return ()
    return tuple(
        sibling
        for sibling in _shared_profile_ids(client, scope)
        if sibling != client and unexplained <= spec_keys(sibling)
    )


def _legacy_managed_state_present(
    aqg_root: Path, client_root: Path, settings: dict
) -> bool:
    skills_dir = client_root / "skills"
    if any(
        _managed_skill(skills_dir / source.name) is not None
        for source in _skill_sources(aqg_root)
    ):
        return True
    if any(
        isinstance(hook, dict) and _owned_command(hook.get("command"))
        for blocks in (settings.get("hooks", {}) or {}).values()
        if isinstance(blocks, list)
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("hooks", []), list)
        for hook in block.get("hooks", [])
    ):
        return True
    rule = client_root / "rules" / "aqg.md"
    if (
        not rule.is_symlink()
        and rule.is_file()
        and RULE_MARKER in _read_text(rule, "managed rule")
    ):
        return True
    marker_dir = client_root / LINK_MARKER_DIR
    return marker_dir.is_dir() and any(
        marker.name != OWNERS_MARKER and _read_managed_marker(marker) is not None
        for marker in marker_dir.iterdir()
    )


def _claim_owners(
    client_root: Path, clients: tuple[str, ...], scope: str
) -> bool:
    # Installer entry points are single-writer operations. Cross-process
    # serialization is an existing caller contract, not introduced here.
    if not clients:
        return False
    client = clients[0]
    owners, _present = _read_root_owners(client_root, client, scope)
    claimed = sorted(set(owners).union(clients))
    if claimed == owners:
        return False
    return _write_owners(client_root, claimed)


def _release_owner(
    client_root: Path, client: str, scope: str
) -> tuple[bool, list[str], bool]:
    owners, present = _read_root_owners(client_root, client, scope)
    if client not in owners:
        return False, owners, present
    remaining = [owner for owner in owners if owner != client]
    return True, remaining, present


def _effective_hook_specs(
    aqg_root: Path, clients: list[str]
) -> dict[str, list[dict]]:
    """Build one hook surface for all active Qoder identities on the root."""
    result: dict[str, list[dict]] = {}
    seen: set[tuple[str, str, str]] = set()
    for client in clients:
        for event, blocks in _hook_specs(aqg_root, bool(PROFILES[client]["cli"])).items():
            for block in blocks:
                for hook in block["hooks"]:
                    key = (event, block.get("matcher", ""), hook["command"])
                    if key in seen:
                        continue
                    seen.add(key)
                    existing = next(
                        (item for item in result.setdefault(event, [])
                         if item.get("matcher", "") == block.get("matcher", "")),
                        None,
                    )
                    if existing is None:
                        existing = {"matcher": block.get("matcher", ""), "hooks": []}
                        result[event].append(existing)
                    existing["hooks"].append(hook)
    return result


def _create_windows_junction(source: Path, target: Path) -> bool:
    return create_windows_junction(source, target) and _is_windows_junction(target)


def _create_link(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source, target_is_directory=True)
        return
    except OSError:
        if not _create_windows_junction(source, target):
            raise


def _remove_link(path: Path) -> None:
    if not _is_link_install(path):
        raise InstallError(f"refusing to remove non-link skill as link: {path}")
    remove_install(path)


def _installed_skill_ok(target: Path, source: Path, source_digest: str) -> bool:
    link_marker = _managed_link_skill(target)
    if link_marker is not None:
        return _link_points_to(target, source)

    copy_marker = _managed_copy_skill(target)
    return (
        copy_marker is not None
        and copy_marker.get("source_digest") == source_digest
        and _tree_digest(target) == source_digest
    )


def _skill_sources(aqg_root: Path) -> list[Path]:
    return sorted(path for path in (aqg_root / "skills").glob("aqg-*") if path.is_dir())


def _rule_text(aqg_root: Path) -> str:
    template = (aqg_root / "examples" / "aqg-codex-agents.example.md").read_text(encoding="utf-8")
    lines = template.splitlines()
    try:
        start = next(
            index
            for index, line in enumerate(lines)
            if "Agent Quality Gates (AQG) engineering discipline" in line
        )
    except StopIteration as exc:
        raise InstallError("AQG rules template section is missing") from exc
    body = "\n".join(lines[start:]).rstrip() + "\n"
    # A literal <AQG_ROOT> in a rules file is a dangling pointer — nothing expands
    # it there, so the one line leading to the authoritative criteria would lead
    # nowhere. Resolve it against the checkout actually being installed from.
    body = body.replace("<AQG_ROOT>", str(policy_root(aqg_root)))
    return f"---\nalwaysApply: true\n---\n\n{RULE_MARKER}\n\n{body}"


def _rule_supported(client: str, scope: str) -> bool:
    # Qoder IDE documents project rules only. Both CLIs additionally document
    # user rules under their user configuration directory.
    return scope == "project" or bool(PROFILES[client]["cli"])


def _hook_command(aqg_root: Path, script: str) -> str:
    adapter = aqg_root / "agent-packs" / "qoder" / "hooks" / "qoder_hook_adapter.py"
    values = [
        sys.executable,
        str(adapter),
        "--aqg-root",
        str(aqg_root),
        "--hook",
        script,
        "--managed-id",
        MANAGED_ID,
    ]
    command = _bash_command_with_windows_python_candidates(values) if os.name == "nt" else _bash_command(values)
    if script in BLOCKING_HOOKS:
        return f"{command} || exit 2"
    return f"{command} || exit 0"


def _bash_command(values: list[str]) -> str:
    return " ".join(shlex.quote(value) for value in values)


def _windows_python_candidates(value: str) -> list[str]:
    normalized = value.replace("\\", "/")
    if len(normalized) < 3 or normalized[1] != ":" or normalized[2] != "/":
        return [value]
    drive = normalized[0].lower()
    rest = normalized[2:]
    return [f"/mnt/{drive}{rest}", f"/{drive}{rest}", value]


def _bash_command_with_windows_python_candidates(values: list[str]) -> str:
    candidates = _windows_python_candidates(values[0])
    if len(candidates) == 1:
        return _bash_command(values)
    tail = [*values[1:]]
    first, second, original = candidates
    first_command = _bash_command([first, *tail])
    second_command = _bash_command([second, *tail])
    original_command = _bash_command([original, *tail])
    return (
        f"if [ -x {shlex.quote(first)} ]; then {first_command}; "
        f"elif [ -x {shlex.quote(second)} ]; then {second_command}; "
        f"else {original_command}; fi"
    )


def _split_powershell_args(value: str) -> list[str] | None:
    parts: list[str] = []
    index = 0
    while index < len(value):
        while index < len(value) and value[index].isspace():
            index += 1
        if index == len(value):
            break
        if value[index] != "'":
            return None
        index += 1
        current: list[str] = []
        while index < len(value):
            if value[index] != "'":
                current.append(value[index])
                index += 1
                continue
            if index + 1 < len(value) and value[index + 1] == "'":
                current.append("'")
                index += 2
                continue
            index += 1
            break
        else:
            return None
        parts.append("".join(current))
    return parts


def _command_parts(command: object) -> tuple[list[str], int] | None:
    if not isinstance(command, str):
        return None
    ps_prefix = "$ErrorActionPreference='Stop'; try { & "
    if command.startswith(ps_prefix):
        for fallback in (0, 2):
            suffix = (
                f"; if ($null -eq $LASTEXITCODE) {{ exit {fallback} }}; "
                f"exit $LASTEXITCODE }} catch {{ exit {fallback} }}"
            )
            if command.endswith(suffix):
                parts = _split_powershell_args(command[len(ps_prefix) : -len(suffix)])
                return (parts, fallback) if parts is not None else None
        return None

    blocking_suffix = " || exit 2"
    legacy_blocking_suffix = '; aqg_rc=$?; if [ "$aqg_rc" -eq 0 ]; then exit 0; else exit 2; fi'
    warning_suffix = " || exit 0"
    if command.endswith(blocking_suffix):
        base, fallback = command[: -len(blocking_suffix)], 2
    elif command.endswith(legacy_blocking_suffix):
        base, fallback = command[: -len(legacy_blocking_suffix)], 2
    elif command.endswith(warning_suffix):
        base, fallback = command[: -len(warning_suffix)], 0
    else:
        return None
    if base.startswith("if [ -x ") and "; else " in base and base.endswith("; fi"):
        invocation = base.rsplit("; else ", 1)[1][: -len("; fi")]
        try:
            return shlex.split(invocation), fallback
        except ValueError:
            return None
    try:
        return shlex.split(base), fallback
    except ValueError:
        return None


def _is_absolute_portable(value: str) -> bool:
    path = Path(value)
    if path.is_absolute():
        return True
    drive = value[:2]
    return len(value) >= 3 and drive[0].isalpha() and drive[1] == ":" and value[2] in ("\\", "/")


def _join_portable(root: str, *parts: str) -> str:
    return "/".join([root.rstrip("\\/"), *parts])


def _normalize_portable(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[0].lower() + normalized[1:]
    return normalized


def _hook_specs(aqg_root: Path, cli: bool) -> dict[str, list[dict]]:
    for script in HOOK_SCRIPTS:
        if not (aqg_root / "agent-packs" / "claude-code" / "hooks" / script).is_file():
            raise InstallError(f"AQG hook script is missing: {script}")
    if not (aqg_root / "agent-packs" / "qoder" / "hooks" / "qoder_hook_adapter.py").is_file():
        raise InstallError("Qoder hook adapter is missing")

    def entry(script: str) -> dict:
        return {"type": "command", "command": _hook_command(aqg_root, script)}

    specs: dict[str, list[dict]] = {
        "UserPromptSubmit": [
            {"matcher": "", "hooks": [entry("userpromptsubmit_handoff_mandate.sh")]}
        ],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [entry("pretooluse_bash_skill_validator.sh")]},
            {
                "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                "hooks": [entry("pretooluse_secret_scan.sh")],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "Write|Edit|MultiEdit",
                "hooks": [
                    entry("posttooluse_skill_edit_reminder.sh"),
                    entry("posttooluse_code_construction_reminder.sh"),
                    entry("posttooluse_test_quality_reminder.sh"),
                    entry("posttooluse_security_review_reminder.sh"),
                ],
            }
        ],
        "PostToolUseFailure": [
            {"matcher": "Bash", "hooks": [entry("posttooluse_bash_error_debugging_reminder.sh")]}
        ],
        "Stop": [
            {
                "matcher": "",
                "hooks": [entry("precompact_closeout_reminder.sh")],
            }
        ],
    }
    # `cli` gates every session-lifecycle event, and with it the managed update
    # check below. Qoder Desktop and Qoder CN Desktop (`cli=False`, support level
    # `partial`) have no verified SessionStart surface, so AQG mounts none for them
    # and they get no hook-borne update trigger at all -- not an oversight, and not
    # silently absent: tests/behavior/test_update_check_host_coverage.py names both
    # with this reason and fails if either quietly gains or loses coverage. They
    # are reached instead by _aqgctx_nudge_update in scripts/_aqg_context.sh,
    # which every skill sources and which shares this check's throttle file --
    # but only when a session invokes a skill, never merely by starting.
    if cli:
        specs["Stop"][0]["hooks"].append(entry("wip_checkpoint_save.sh"))
        specs["SessionStart"] = [
            {
                "matcher": "",
                "hooks": [
                    entry("sessionstart_preflight.sh"),
                    entry("wip_checkpoint_recover.sh"),
                    # A trigger only: it starts a detached process and returns, and
                    # it stays out of BLOCKING_HOOKS so a slow remote can never stop
                    # a session from starting.
                    entry("sessionstart_update_check.sh"),
                ],
            }
        ]
        specs["PreCompact"] = [
            {
                "matcher": "",
                "hooks": [
                    entry("precompact_closeout_reminder.sh"),
                    entry("wip_checkpoint_save.sh"),
                ],
            }
        ]
    return specs


def _owned_command(command: object) -> bool:
    parsed = _command_parts(command)
    if parsed is None:
        return False
    parts, fallback = parsed
    if len(parts) != 8:
        return False
    interpreter, adapter, root, script, managed = parts[0], parts[1], parts[3], parts[5], parts[7]
    if parts[2::2] != ["--aqg-root", "--hook", "--managed-id"]:
        return False
    if script not in HOOK_SCRIPTS or managed != MANAGED_ID:
        return False
    if fallback != (2 if script in BLOCKING_HOOKS else 0):
        return False
    if not _is_absolute_portable(interpreter) or not _is_absolute_portable(root):
        return False
    expected_adapter = _join_portable(
        root,
        "agent-packs",
        "qoder",
        "hooks",
        "qoder_hook_adapter.py",
    )
    return _normalize_portable(adapter) == _normalize_portable(expected_adapter)


def _script_in_command(command: str) -> str | None:
    parsed = _command_parts(command)
    if parsed is None or len(parsed[0]) != 8:
        return None
    script = parsed[0][5]
    return script if script in HOOK_SCRIPTS else None


def _merge_hooks(existing: dict, specs: dict[str, list[dict]]) -> tuple[dict, bool]:
    merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    changed = False
    expected = {
        (event, block.get("matcher", ""), _script_in_command(hook["command"]))
        for event, canonical_blocks in specs.items()
        for block in canonical_blocks
        for hook in block["hooks"]
    }
    for event, raw_blocks in list(merged.items()):
        if not isinstance(raw_blocks, list):
            continue
        kept_blocks = []
        for block in raw_blocks:
            if not isinstance(block, dict):
                kept_blocks.append(block)
                continue
            hooks = block.get("hooks", [])
            if not isinstance(hooks, list):
                kept_blocks.append(block)
                continue
            matcher = block.get("matcher", "")
            has_foreign_hook = any(
                not isinstance(hook, dict)
                or not _owned_command(hook.get("command"))
                for hook in hooks
            )
            kept_hooks = []
            for hook in hooks:
                if not isinstance(hook, dict) or not _owned_command(hook.get("command")):
                    kept_hooks.append(hook)
                    continue
                script = _script_in_command(hook["command"])
                if (event, matcher, script) in expected and not has_foreign_hook:
                    kept_hooks.append(hook)
                else:
                    changed = True
            if kept_hooks:
                copied = dict(block)
                copied["hooks"] = kept_hooks
                kept_blocks.append(copied)
            elif not hooks:
                kept_blocks.append(block)
            else:
                changed = True
        if kept_blocks:
            merged[event] = kept_blocks
        else:
            merged.pop(event, None)
            changed = True

    for event, canonical_blocks in specs.items():
        raw_blocks = merged.get(event, [])
        blocks = list(raw_blocks) if isinstance(raw_blocks, list) else []
        present: dict[tuple[str, str], dict] = {}
        for block in blocks:
            if not isinstance(block, dict):
                continue
            matcher = block.get("matcher", "")
            hooks = block.get("hooks", [])
            if not isinstance(hooks, list):
                continue
            for hook in hooks:
                if not isinstance(hook, dict) or not _owned_command(hook.get("command")):
                    continue
                script = _script_in_command(hook["command"])
                if script:
                    present[(matcher, script)] = hook

        for canonical_block in canonical_blocks:
            matcher = canonical_block.get("matcher", "")
            additions: list[dict] = []
            for canonical_hook in canonical_block["hooks"]:
                script = _script_in_command(canonical_hook["command"])
                installed = present.get((matcher, script or ""))
                if installed is None:
                    additions.append(canonical_hook)
                    changed = True
                elif installed != canonical_hook:
                    installed.clear()
                    installed.update(canonical_hook)
                    changed = True
            if additions:
                target = next(
                    (
                        block
                        for block in blocks
                        if isinstance(block, dict)
                        and block.get("matcher", "") == matcher
                        and isinstance(block.get("hooks"), list)
                        and bool(block["hooks"])
                        and all(
                            isinstance(hook, dict)
                            and _owned_command(hook.get("command"))
                            for hook in block["hooks"]
                        )
                    ),
                    None,
                )
                if target is None:
                    blocks.append({"matcher": matcher, "hooks": additions})
                else:
                    hooks = target.get("hooks")
                    if not isinstance(hooks, list):
                        hooks = []
                        target["hooks"] = hooks
                    hooks.extend(additions)
        merged[event] = blocks
    return merged, changed


def _validate_hook_merge_surface(existing: dict, specs: dict[str, list[dict]]) -> None:
    for event in specs:
        if event not in existing:
            continue
        blocks = existing[event]
        if not isinstance(blocks, list):
            raise InstallError(f"settings hook event must be an array: {event}")
        for block in blocks:
            if not isinstance(block, dict):
                continue
            hooks = block.get("hooks", [])
            if not isinstance(hooks, list):
                raise InstallError(f"settings hook entries must be an array: {event}")


def _strip_hooks(existing: dict) -> tuple[dict, bool]:
    stripped: dict = {}
    changed = False
    for event, blocks in existing.items():
        if not isinstance(blocks, list):
            stripped[event] = blocks
            continue
        kept_blocks = []
        for block in blocks:
            if not isinstance(block, dict):
                kept_blocks.append(block)
                continue
            hooks = block.get("hooks", [])
            if not isinstance(hooks, list):
                kept_blocks.append(block)
                continue
            kept_hooks = [
                hook
                for hook in hooks
                if not (isinstance(hook, dict) and _owned_command(hook.get("command")))
            ]
            removed = len(kept_hooks) != len(hooks)
            changed = changed or removed
            if kept_hooks:
                copied = dict(block)
                copied["hooks"] = kept_hooks
                kept_blocks.append(copied)
            elif not removed:
                kept_blocks.append(block)
        if kept_blocks:
            stripped[event] = kept_blocks
    return stripped, changed


def _preflight_apply(
    args: argparse.Namespace,
    client_root: Path,
    aqg_root: Path,
    settings: dict,
    specs: dict[str, list[dict]],
    *,
    rule_required: bool,
) -> None:
    _validate_client_layout(client_root)
    _validate_hook_merge_surface(settings.get("hooks", {}) or {}, specs)
    for source in _skill_sources(aqg_root):
        target = client_root / "skills" / source.name
        if classify_install(target) != "missing":
            if _managed_skill(target) is None:
                raise InstallError(f"refusing to overwrite unmanaged skill: {target}")
    rule = client_root / "rules" / "aqg.md"
    if rule_required and (rule.exists() or rule.is_symlink()):
        if rule.is_symlink() or RULE_MARKER not in _read_text(rule, "managed rule"):
            raise InstallError(f"refusing to overwrite unmanaged rule: {rule}")


def _install_skill(
    source: Path,
    target: Path,
    source_digest: str,
    mode: str = "link",
) -> bool:
    if mode not in SKILL_INSTALL_MODES:
        raise InstallError(f"invalid skill install mode: {mode}")

    if mode == "link":
        marker = _managed_link_skill(target)
        marker_data = {
            "managed_by": MANAGED_ID,
            "mode": "link",
            "source": str(skill_link_source(source)),
            "source_digest": source_digest,
        }
        marker_text = json.dumps(marker_data, indent=2) + "\n"
        marker_path = _link_marker(target)
        if marker and _link_points_to(target, source):
            return _sync_link_marker(target, marker_text)

        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.parent / f".{source.name}.aqg-qoder-link-{os.getpid()}"
        counter = 1
        while classify_install(staged) != "missing":
            staged = target.parent / f".{source.name}.aqg-qoder-link-{os.getpid()}-{counter}"
            counter += 1
        _create_link(skill_link_source(source), staged)
        restored = False
        backup_dest: Path | None = None
        try:
            target_type = classify_install(target)
            if target_type != "missing":
                if _is_link_install(target):
                    _remove_link(target)
                    _remove_link_markers(target)
                else:
                    backup_dest = _backup(target)
                    remove_install(target)
            os.replace(staged, target)
            try:
                _atomic_write(marker_path, marker_text)
            except (OSError, UnicodeError):
                if _is_link_install(target):
                    _remove_link(target)
                if backup_dest is not None and backup_dest.is_dir() and not target.exists():
                    shutil.copytree(backup_dest, target, symlinks=True)
                    restored = True
                raise
        except OSError:
            if classify_install(staged) != "missing":
                _remove_link(staged)
            raise
        finally:
            if not restored and classify_install(staged) != "missing":
                _remove_link(staged)
        return True

    marker = _managed_copy_skill(target) if target.exists() else None
    if marker and marker.get("source_digest") == source_digest and _tree_digest(target) == source_digest:
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{source.name}.aqg-qoder-", dir=target.parent))
    try:
        shutil.rmtree(stage)
        shutil.copytree(source, stage)
        marker_data = {"managed_by": MANAGED_ID, "mode": "copy", "source_digest": source_digest}
        _atomic_write(stage / SKILL_MARKER, json.dumps(marker_data, indent=2) + "\n")

        target_type = classify_install(target)
        backup_dest: Path | None = None
        if target_type != "missing":
            if _is_link_install(target):
                _remove_link(target)
                _remove_link_markers(target)
            else:
                backup_dest = _backup(target)
                remove_install(target)
        try:
            os.replace(stage, target)
        except OSError:
            if backup_dest is not None and backup_dest.is_dir() and not target.exists():
                shutil.copytree(backup_dest, target, symlinks=True)
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return True


def _apply(args: argparse.Namespace, client_root: Path, aqg_root: Path) -> int:
    settings_path = client_root / "settings.json"
    _validate_client_layout(client_root)
    settings = _load_settings(settings_path)
    legacy_siblings = _legacy_shared_owners(
        aqg_root, client_root, args.client, settings, scope=args.scope
    )
    owners, _present = _read_root_owners(client_root, args.client, args.scope)
    prospective_owners = sorted(set(owners).union((args.client, *legacy_siblings)))
    effective_specs = _effective_hook_specs(aqg_root, prospective_owners)
    rule_required = any(
        _rule_supported(owner, args.scope) for owner in prospective_owners
    )
    _preflight_apply(
        args,
        client_root,
        aqg_root,
        settings,
        effective_specs,
        rule_required=rule_required,
    )
    skill_sources = [(source, _tree_digest(source)) for source in _skill_sources(aqg_root)]
    canonical_rule = _rule_text(aqg_root) if rule_required else None
    changed = False

    skills_dir = client_root / "skills"
    for source, source_digest in skill_sources:
        target = skills_dir / source.name
        changed = _install_skill(source, target, source_digest, args.mode) or changed
    changed = _cleanup_legacy_link_markers(client_root) or changed

    if canonical_rule is not None:
        rule_path = client_root / "rules" / "aqg.md"
        current_rule = _read_text(rule_path, "managed rule") if rule_path.exists() else None
        if current_rule != canonical_rule:
            if rule_path.exists():
                _backup(rule_path)
            _atomic_write(rule_path, canonical_rule)
            changed = True

    merged_hooks, hooks_changed = _merge_hooks(
        settings.get("hooks", {}) or {}, effective_specs
    )
    if hooks_changed:
        if settings_path.exists():
            _backup(settings_path)
        settings["hooks"] = merged_hooks
        _atomic_write(settings_path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        changed = True

    changed = _write_owners(client_root, prospective_owners) or changed

    if changed:
        print(f"OK: installed AQG for {args.client} at {client_root}")
    else:
        print(f"OK: AQG for {args.client} already installed (no changes)")
    print(f"support_level: {PROFILES[args.client]['level']}")
    if not PROFILES[args.client]["cli"]:
        print("limitations: SessionStart, PreCompact, WIP save/recover unavailable")
    return EXIT_OK


def _uninstall(
    client_root: Path, aqg_root: Path, *, client: str, scope: str
) -> int:
    settings_path = client_root / "settings.json"
    _validate_client_layout(client_root)
    settings = _load_settings(settings_path)
    owners, ledger_present = _read_root_owners(client_root, client, scope)
    if not ledger_present and _legacy_managed_state_present(
        aqg_root, client_root, settings
    ):
        raise InstallError(
            "legacy ownership is unknown; run --apply for each active Qoder "
            "profile before uninstall"
        )
    released, remaining_owners, ledger_present = _release_owner(
        client_root, client, scope
    )
    rule = client_root / "rules" / "aqg.md"
    managed_rule = (
        not rule.is_symlink()
        and rule.is_file()
        and RULE_MARKER in _read_text(rule, "managed rule")
    )
    changed = False

    if remaining_owners:
        hook_specs = _effective_hook_specs(aqg_root, remaining_owners)
        hooks = settings.get("hooks", {}) or {}
        merged_hooks, hooks_changed = _merge_hooks(
            hooks if isinstance(hooks, dict) else {}, hook_specs
        )
    else:
        hooks = settings.get("hooks", {}) or {}
        merged_hooks, hooks_changed = _strip_hooks(
            hooks if isinstance(hooks, dict) else {}
        )
    if hooks_changed:
        _backup(settings_path)
        if merged_hooks:
            settings["hooks"] = merged_hooks
        else:
            settings.pop("hooks", None)
        _atomic_write(settings_path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        changed = True

    if managed_rule and not any(
        _rule_supported(owner, scope) for owner in remaining_owners
    ):
        _backup(rule)
        rule.unlink()
        changed = True

    skills_dir = client_root / "skills"
    if not remaining_owners:
        if skills_dir.is_dir():
            for target in sorted(skills_dir.glob("aqg-*")):
                marker = _managed_skill(target)
                if marker is None:
                    continue
                if _is_link_install(target):
                    _remove_link(target)
                    _remove_link_markers(target)
                elif target.is_dir():
                    _backup(target)
                    shutil.rmtree(target)
                else:
                    continue
                changed = True
        changed = _cleanup_managed_link_markers(client_root) or changed
        changed = _cleanup_legacy_link_markers(client_root) or changed

    if released:
        changed = _write_owners(client_root, remaining_owners) or changed
    if remaining_owners:
        print(
            f"INFO: AQG skills kept at {skills_dir}; still claimed by "
            + ", ".join(remaining_owners)
        )

    message = (
        "OK: removed AQG-managed Qoder assets"
        if changed
        else "OK: no AQG-managed Qoder assets found"
    )
    print(message)
    run_dir = _BACKUP.run_dir if _BACKUP is not None else None
    if run_dir is not None:
        print(f"backup: {run_dir}")
    return EXIT_OK


def _check(client: str, scope: str, client_root: Path, aqg_root: Path, *, verbose: bool) -> int:
    try:
        _validate_client_layout(client_root)
        settings = _load_settings(client_root / "settings.json")
    except InstallError as exc:
        if verbose:
            print(f"AQG Qoder support: NOT INSTALLED ({client})")
            print(f"  - {exc}")
        return EXIT_ERROR
    missing: list[str] = []
    owners, ledger_present = _read_root_owners(client_root, client, scope)
    if not ledger_present and _legacy_managed_state_present(
        aqg_root, client_root, settings
    ):
        missing.append("legacy ownership ledger missing")
    # A sibling may have installed the shared root without this profile being
    # installed. Verify that profile's contract in that case; once it claims
    # the root, verify the union required by all active siblings.
    effective_clients = owners if ledger_present and client in owners else [client]
    specs = _effective_hook_specs(aqg_root, effective_clients)
    expected = {
        (event, block.get("matcher", ""), _script_in_command(hook["command"])): hook
        for event, blocks in specs.items()
        for block in blocks
        for hook in block["hooks"]
    }
    actual = {
        (event, block.get("matcher", ""), _script_in_command(hook.get("command", ""))): hook
        for event, blocks in (settings.get("hooks", {}) or {}).items()
        if isinstance(blocks, list)
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("hooks", []), list)
        for hook in block.get("hooks", [])
        if isinstance(hook, dict) and _owned_command(hook.get("command"))
    }
    for item, hook in sorted(expected.items(), key=lambda pair: str(pair[0])):
        if actual.get(item) != hook:
            missing.append(f"missing or stale hook: {item}")
    for item in sorted(set(actual) - set(expected), key=str):
        missing.append(f"extra managed hook: {item}")
    if _rule_supported(client, scope):
        rule = client_root / "rules" / "aqg.md"
        if (
            rule.is_symlink()
            or not rule.is_file()
            or _read_text(rule, "managed rule") != _rule_text(aqg_root)
        ):
            missing.append("missing managed rule")
    for source in _skill_sources(aqg_root):
        target = client_root / "skills" / source.name
        source_digest = _tree_digest(source)
        if not _installed_skill_ok(target, source, source_digest):
            missing.append(f"missing managed skill: {source.name}")

    if verbose:
        if missing:
            print(f"AQG Qoder support: NOT INSTALLED ({client})")
            for item in missing:
                print(f"  - {item}")
        else:
            print(f"AQG Qoder support: INSTALLED ({client})")
            print(f"support_level: {PROFILES[client]['level']}")
            print(f"target: {client_root}")
    return EXIT_ERROR if missing else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install AQG support for Qoder-family clients")
    parser.add_argument("--client", choices=sorted(PROFILES), required=True)
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--project-root")
    parser.add_argument("--aqg-root")
    parser.add_argument("--mode", choices=SKILL_INSTALL_MODES, default="link")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--is-installed", action="store_true")
    args = parser.parse_args(argv)

    try:
        aqg_root = _resolve_aqg_root(args.aqg_root)
        client_root = _client_root(args)
        # Read-only actions never touch files, so they run without a backup session.
        if not args.apply and not args.uninstall:
            return _check(args.client, args.scope, client_root, aqg_root, verbose=args.verify)
        _open_backups(args, client_root, aqg_root)
        ok = False
        try:
            result = (
                _apply(args, client_root, aqg_root)
                if args.apply
                else _uninstall(
                    client_root,
                    aqg_root,
                    client=args.client,
                    scope=args.scope,
                )
            )
            ok = result == EXIT_OK
            return result
        finally:
            _close_backups(ok)
    except (InstallError, OSError, UnicodeError, BackupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
