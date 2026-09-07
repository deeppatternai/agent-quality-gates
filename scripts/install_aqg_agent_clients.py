#!/usr/bin/env python3
"""Install AQG support for Trae-family, Zed, and Devin clients."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from aqg_skill_install import classify_install, create_windows_junction, remove_install
except ModuleNotFoundError:
    from scripts.aqg_skill_install import classify_install, create_windows_junction, remove_install

try:
    from _aqg_backup import BackupError, BackupSession, migrate_legacy
except ModuleNotFoundError:
    from scripts._aqg_backup import BackupError, BackupSession, migrate_legacy


EXIT_OK = 0
EXIT_ERROR = 1
MANAGED_ID = "aqg-agent-client-v1"
SKILL_MARKER = ".aqg-agent-client-managed.json"
LINK_MARKER_DIR = "managed-links"
# Trae/Work profile pairs share one user skills root, so a skill directory has no
# single owner. This ledger records which profiles currently claim the root, and
# uninstall only removes the skills once the last claim is released.
OWNERS_MARKER = ".aqg-agent-client-owners.json"
RULE_MARKER = "<!-- AQG-MANAGED: aqg-agent-client-v1 -->"
BACKUP_DIR = ".aqg-backups"  # legacy in-place dir (under each root), migrated into the central store
SETTINGS_BACKUP_SUFFIX = ".aqg-agent-client.bak"  # legacy backup name suffix
SKILL_INSTALL_MODES = ("link", "copy")

# Central-store sessions open for the current apply/uninstall run, as
# (source_root, session) pairs. Clients may write to several disjoint roots
# (e.g. zed skills under ~/.agents vs config under ~/.config/zed).
_SESSIONS: "list[tuple[Path, BackupSession]]" = []
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
    {"pretooluse_bash_skill_validator.sh", "pretooluse_secret_scan.sh"}
)
ZED_WINDOWS_CONFIG_DIRNAME = "Zed"
DEVIN_WINDOWS_CONFIG_DIRNAME = "devin"


@dataclass(frozen=True)
class ClientProfile:
    client_id: str
    support_level: str
    user_config_root: str
    project_config_root: str
    user_skills_root: str
    project_skills_root: str
    user_rules_root: str | None
    project_rules_root: str | None
    user_rules_file: str | None
    project_rules_file: str | None
    hooks_kind: str | None
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class ClientPaths:
    config_root: Path
    rules_root: Path | None
    skills_root: Path
    rules_path: Path | None
    settings_path: Path | None


PROFILES: dict[str, ClientProfile] = {
    "trae": ClientProfile(
        "trae",
        "partial",
        ".trae",
        ".trae",
        "$CONFIG/skills",
        ".trae/skills",
        None,
        ".",
        None,
        "AGENTS.md",
        "trae",
        ("PreCompact unavailable; WIP save is Stop-only.", "User rules remain UI-managed."),
    ),
    "trae-cn": ClientProfile(
        "trae-cn",
        "partial",
        ".trae-cn",
        ".trae",
        "$CONFIG/skills",
        ".trae/skills",
        None,
        ".",
        None,
        "AGENTS.md",
        "trae",
        ("PreCompact unavailable; WIP save is Stop-only.", "User rules remain UI-managed."),
    ),
    # TRAE Work (`TRAE SOLO.app`, com.trae.solo.app) reads the same user skills
    # root as the international Trae IDE; Work CN (`TRAE SOLO CN.app`,
    # cn.trae.solo.app) shares the CN root. A private `.trae-work*` root would put
    # the skills where neither product looks.
    "trae-work": ClientProfile(
        "trae-work",
        "partial",
        ".trae",
        ".trae",
        "$CONFIG/skills",
        ".trae/skills",
        None,
        ".",
        None,
        "AGENTS.md",
        None,
        ("No official Work lifecycle-hook schema verified.", "User rules remain UI-managed."),
    ),
    "trae-work-cn": ClientProfile(
        "trae-work-cn",
        "partial",
        ".trae-cn",
        ".trae",
        "$CONFIG/skills",
        ".trae/skills",
        None,
        ".",
        None,
        "AGENTS.md",
        None,
        ("No official Work lifecycle-hook schema verified.", "User rules remain UI-managed."),
    ),
    "zed": ClientProfile(
        "zed",
        "partial",
        ".config/zed",
        ".zed",
        "$HOME/.agents/skills",
        ".agents/skills",
        "$CONFIG",
        ".",
        "AGENTS.md",
        "AGENTS.md",
        None,
        ("No official lifecycle-hook surface verified.",),
    ),
    "devin": ClientProfile(
        "devin",
        "partial",
        ".config/devin",
        ".devin",
        "$CONFIG/skills",
        ".agents/skills",
        "$CONFIG",
        ".",
        "AGENTS.md",
        "AGENTS.md",
        "devin",
        ("No PreCompact-before-compaction hook; PostCompaction is not a WIP-save substitute.",),
    ),
}

# 2026-09-03 host-recovery incident (blocker 3): trae-cn and trae-work-cn both
# declare project_config_root=".trae", identical to trae/trae-work, with no
# verified TRAE CN product evidence that project scope is actually shared
# rather than living under its own ".trae-cn". Project scope fails closed for
# these two until that evidence exists; user scope is unaffected.
UNVERIFIED_PROJECT_SCOPE_CLIENTS = frozenset({"trae-cn", "trae-work-cn"})


class InstallError(RuntimeError):
    pass


def _resolve_aqg_root(value: str | None) -> Path:
    root = Path(value).expanduser().resolve() if value else Path(__file__).resolve().parent.parent
    if not (root / "VERSION").is_file() or not (root / "skills").is_dir():
        raise InstallError(f"invalid AQG root: {root}")
    return root


def _home_root(args: argparse.Namespace) -> Path:
    return Path(args.home).expanduser().resolve()


def _is_same_or_under(path: Path, parent: Path) -> bool:
    path_key = os.path.normcase(str(path))
    parent_key = os.path.normcase(str(parent))
    return path_key == parent_key or path_key.startswith(parent_key + os.sep)


def _env_appdata_for_home(home: Path) -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    appdata_path = Path(appdata).expanduser().resolve()
    if _is_same_or_under(appdata_path, home):
        return appdata_path
    if home == Path.home().expanduser().resolve():
        return appdata_path
    return None


def _zed_user_config_root(home: Path) -> Path:
    appdata = _env_appdata_for_home(home)
    if appdata:
        return (appdata / ZED_WINDOWS_CONFIG_DIRNAME).resolve()
    if os.name == "nt":
        return (home / "AppData" / "Roaming" / ZED_WINDOWS_CONFIG_DIRNAME).resolve()
    return (home / ".config" / "zed").resolve()


def _devin_user_config_root(home: Path) -> Path:
    # Canonical install target is lowercase `%APPDATA%/devin`; detection accepts
    # both `%APPDATA%/devin` and `%APPDATA%/Devin` in install_aqg_clients.py.
    appdata = _env_appdata_for_home(home)
    if appdata:
        return (appdata / DEVIN_WINDOWS_CONFIG_DIRNAME).resolve()
    if os.name == "nt":
        return (home / "AppData" / "Roaming" / DEVIN_WINDOWS_CONFIG_DIRNAME).resolve()
    return (home / ".config" / "devin").resolve()


def _user_config_root(home: Path, profile: ClientProfile) -> Path:
    if profile.client_id == "zed":
        return _zed_user_config_root(home)
    if profile.client_id == "devin":
        return _devin_user_config_root(home)
    return (home / Path(profile.user_config_root)).resolve()


def _resolve_user_root_spec(spec: str | None, home: Path, config_root: Path) -> Path | None:
    if spec is None:
        return None
    if spec == "$CONFIG":
        return config_root
    if spec.startswith("$CONFIG/"):
        return (config_root / Path(spec.removeprefix("$CONFIG/"))).resolve()
    if spec == "$HOME":
        return home
    if spec.startswith("$HOME/"):
        return (home / Path(spec.removeprefix("$HOME/"))).resolve()
    return (home / Path(spec)).resolve()


def _resolve_project_root_spec(spec: str | None, project: Path, config_root: Path) -> Path | None:
    if spec is None:
        return None
    if spec == "$CONFIG":
        return config_root
    if spec.startswith("$CONFIG/"):
        return (config_root / Path(spec.removeprefix("$CONFIG/"))).resolve()
    return (project / Path(spec)).resolve()


def _settings_path(config_root: Path, profile: ClientProfile, scope: str) -> Path | None:
    if profile.hooks_kind == "trae":
        return config_root / "hooks.json"
    if profile.hooks_kind == "devin":
        return config_root / ("config.json" if scope == "user" else "hooks.v1.json")
    return None


def _client_paths(args: argparse.Namespace, profile: ClientProfile) -> ClientPaths:
    if args.scope == "user":
        home = _home_root(args)
        config_root = _user_config_root(home, profile)
        rules_root = _resolve_user_root_spec(profile.user_rules_root, home, config_root)
        skills_root = _resolve_user_root_spec(profile.user_skills_root, home, config_root)
        if skills_root is None:
            raise InstallError(f"missing user skills root for {profile.client_id}")
        rules_path = (
            (rules_root / profile.user_rules_file).resolve()
            if rules_root is not None and profile.user_rules_file is not None
            else None
        )
        return ClientPaths(
            config_root=config_root,
            rules_root=rules_root,
            skills_root=skills_root,
            rules_path=rules_path,
            settings_path=_settings_path(config_root, profile, args.scope),
        )
    if profile.client_id in UNVERIFIED_PROJECT_SCOPE_CLIENTS:
        # 2026-09-03 host-recovery incident: project_config_root for these two
        # profiles is ".trae", identical to trae/trae-work, with no verified
        # product evidence that TRAE CN actually reads project config from a
        # directory shared with the international product. Fail closed rather
        # than silently writing into that shared directory.
        raise InstallError(
            f"{profile.client_id} project scope is not verified: no product evidence "
            "that TRAE CN reads project config from a directory shared with the "
            "international Trae/Trae Work project scope. Refusing to write "
            "--scope project for this client; use --scope user, which is supported."
        )
    if args.project_root is None:
        raise InstallError("--project-root is required with --scope project")
    project = Path(args.project_root).expanduser().resolve()
    if not project.is_dir():
        raise InstallError(f"project root does not exist: {project}")
    config_root = (project / Path(profile.project_config_root)).resolve()
    rules_root = _resolve_project_root_spec(profile.project_rules_root, project, config_root)
    skills_root = _resolve_project_root_spec(profile.project_skills_root, project, config_root)
    if skills_root is None:
        raise InstallError(f"missing project skills root for {profile.client_id}")
    rules_path = (
        (rules_root / profile.project_rules_file).resolve()
        if rules_root is not None and profile.project_rules_file is not None
        else None
    )
    return ClientPaths(
        config_root=config_root,
        rules_root=rules_root,
        skills_root=skills_root,
        rules_path=rules_path,
        settings_path=_settings_path(config_root, profile, args.scope),
    )


def _client_root(args: argparse.Namespace, profile: ClientProfile) -> Path:
    return _client_paths(args, profile).config_root


def _skill_sources(aqg_root: Path) -> list[Path]:
    return sorted(path for path in (aqg_root / "skills").glob("aqg-*") if path.is_dir())


def _atomic_write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise InstallError(f"refusing to write through symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-aqg-agent-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _dispatch(path: Path) -> BackupSession:
    """Pick the open session whose source_root contains path (longest match wins)."""
    best: tuple[Path, BackupSession] | None = None
    for root, session in _SESSIONS:
        if _within(path, root) and (best is None or len(str(root)) > len(str(best[0]))):
            best = (root, session)
    if best is None:
        raise InstallError(f"internal error: no backup session covers {path}")
    return best[1]


def _distinct_roots(paths: ClientPaths) -> list[Path]:
    """Every distinct resolved root this client may write assets or legacy backups under."""
    raw = [paths.config_root, paths.skills_root]
    if paths.rules_root is not None:
        raw.append(paths.rules_root)
    resolved: list[Path] = []
    for root in raw:
        candidate = root.expanduser().resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    return resolved


def _outermost(roots: list[Path]) -> list[Path]:
    """Keep only roots not contained within another, so we open the fewest sessions."""
    return [
        root
        for root in roots
        if not any(other != root and _within(root, other) for other in roots)
    ]


def _open_backups(
    args: argparse.Namespace, profile: ClientProfile, paths: ClientPaths, aqg_root: Path
) -> None:
    """Start central-store session(s) for this run, migrating legacy backups first."""
    global _SESSIONS
    scope = args.scope
    project_root = (
        Path(args.project_root).expanduser().resolve() if scope == "project" else None
    )
    resolved = _distinct_roots(paths)
    # Legacy backups sat under <root>/.aqg-backups for each root (including inner
    # ones like skills_root), so migrate every distinct root, not just the outermost.
    for root in resolved:
        legacy = root / BACKUP_DIR
        if legacy.is_dir() and not legacy.is_symlink():
            migrate_legacy(
                profile.client_id,
                root,
                [(legacy, "_legacy/aqg-backups")],
                scope=scope,
                project_root=project_root,
                installer="install_aqg_agent_clients.py",
                aqg_root=aqg_root,
            )
    _SESSIONS = [
        (
            root,
            BackupSession(
                profile.client_id,
                root,
                scope=scope,
                project_root=project_root,
                installer="install_aqg_agent_clients.py",
                aqg_root=aqg_root,
            ),
        )
        for root in _outermost(resolved)
    ]


def _close_backups(ok: bool) -> None:
    """Finalize every run session: keep+prune on success, discard on failure."""
    global _SESSIONS
    sessions = _SESSIONS
    _SESSIONS = []
    for _root, session in sessions:
        if ok:
            session.close()
            session.gc()
        else:
            session.discard()


def _backup(path: Path, root: Path) -> Path | None:
    if not _SESSIONS:  # defensive: apply/uninstall always open a session first
        raise InstallError("internal error: backup session not open")
    return _dispatch(path).backup(path)


def _copytree_digest(root: Path) -> str:
    import hashlib

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


def _marker_path(target: Path) -> Path:
    if _is_link_install(target):
        return target.parent.parent / LINK_MARKER_DIR / f"{target.name}.json"
    return target / SKILL_MARKER


def _read_marker(target: Path) -> dict[str, object] | None:
    marker = _marker_path(target)
    return _read_marker_file(marker)


def _read_marker_file(marker: Path) -> dict[str, object] | None:
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("managed_by") == MANAGED_ID else None


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except OSError:
        return False


def _is_windows_junction(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction(path)) if isjunction else False


def _is_link_install(path: Path) -> bool:
    return path.is_symlink() or _is_windows_junction(path)


def _create_link(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source, target_is_directory=True)
        return
    except OSError:
        if not create_windows_junction(source, target):
            raise


def _remove_link(path: Path) -> None:
    if not _is_link_install(path):
        raise InstallError(f"refusing to remove non-link skill as link: {path}")
    marker = _marker_path(path)
    remove_install(path)
    if _read_marker_file(marker) is not None:
        marker.unlink()


def _install_skill(source: Path, target: Path, client_root: Path, mode: str) -> bool:
    if mode not in SKILL_INSTALL_MODES:
        raise InstallError(f"invalid skill install mode: {mode}")
    source_digest = _copytree_digest(source)
    expected = {
        "managed_by": MANAGED_ID,
        "mode": mode,
        "source": str(source.resolve()),
        "source_digest": source_digest,
    }
    marker = _read_marker(target) if target.exists() else None
    if marker == expected:
        if mode == "link" and _is_link_install(target) and _same_resolved_path(target, source):
            return False
        if mode == "copy" and target.is_dir() and _copytree_digest(target) == source_digest:
            return False
    if target.exists() or target.is_symlink():
        if marker is None:
            raise InstallError(f"refusing to overwrite unmanaged skill: {target}")
        if _is_link_install(target):
            _remove_link(target)
        else:
            _backup(target, client_root)
            shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "link":
        _create_link(source.resolve(), target)
        _atomic_write(_marker_path(target), json.dumps(expected, indent=2) + "\n")
        return True
    shutil.copytree(source, target)
    _atomic_write(_marker_path(target), json.dumps(expected, indent=2) + "\n")
    return True


def _owners_path(skills_root: Path) -> Path:
    return skills_root.parent / LINK_MARKER_DIR / OWNERS_MARKER


def _read_owners(skills_root: Path) -> list[str]:
    path = _owners_path(skills_root)
    if path.is_symlink() or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict) or data.get("managed_by") != MANAGED_ID:
        return []
    owners = data.get("owners")
    if not isinstance(owners, list):
        return []
    return [item for item in owners if isinstance(item, str)]


def _write_owners(skills_root: Path, owners: list[str]) -> None:
    path = _owners_path(skills_root)
    if not owners:
        if path.is_file() and not path.is_symlink():
            path.unlink()
        return
    _atomic_write(
        path,
        json.dumps(
            {"managed_by": MANAGED_ID, "owners": sorted(set(owners))}, indent=2
        )
        + "\n",
    )


def _claim_skills_root(skills_root: Path, client_id: str) -> bool:
    owners = _read_owners(skills_root)
    if client_id in owners:
        return False
    _write_owners(skills_root, [*owners, client_id])
    return True


def _release_skills_root(skills_root: Path, client_id: str) -> tuple[bool, list[str]]:
    """Drop this profile's claim; return (changed, profiles still holding the root).

    An absent ledger means no profile ever claimed the root through this code
    path, so the caller keeps the pre-ownership behavior of removing its own
    managed skills.
    """
    owners = _read_owners(skills_root)
    remaining = [item for item in owners if item != client_id]
    if remaining == owners:
        return False, remaining
    _write_owners(skills_root, remaining)
    return True, remaining


def _sibling_client_ids(client_id: str) -> tuple[str, ...]:
    """Other profiles whose user-scope skills root is structurally the same dir.

    Trae IDE and TRAE SOLO always read the same `.trae/skills`; that sharing is
    a fact of the product family, not something that varies with which
    profiles happen to be installed, so it is derived from the static PROFILES
    table rather than from what has actually been applied on this host.
    """
    profile = PROFILES[client_id]
    return tuple(
        other_id
        for other_id, other in PROFILES.items()
        if other_id != client_id
        and other.user_config_root == profile.user_config_root
        and other.user_skills_root == profile.user_skills_root
    )


def _self_heal_legacy_owners(aqg_root: Path, skills_root: Path, client_id: str) -> tuple[str, ...]:
    """Adopt sibling profiles into the ledger for a pre-024 root with no ledger.

    A shared root that already has valid AQG-managed skills but no owners
    ledger predates the ownership-tracking feature this WorkPacket added
    (2026-09-03 host-recovery incident: a sibling's uninstall wiped skills a
    ledger-less legacy profile still needed). Claiming for every structural
    sibling - not just the profile currently applying - means a later
    uninstall of any single one of them cannot delete content another sibling
    may still depend on. A genuinely fresh root (no pre-existing skills) is
    left alone: the caller's own claim is the only truth there.
    """
    if _read_owners(skills_root):
        return ()
    has_legacy_content = any(
        _read_marker(skills_root / source.name) is not None
        for source in _skill_sources(aqg_root)
    )
    if not has_legacy_content:
        return ()
    return _sibling_client_ids(client_id)


def _rule_text(aqg_root: Path, client_id: str) -> str:
    template = (aqg_root / "examples" / "aqg-codex-agents.example.md").read_text(encoding="utf-8")
    lines = template.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if "Agent Quality Gates (AQG) engineering discipline" in line
    )
    body = "\n".join(lines[start:]).rstrip() + "\n"
    # A literal <AQG_ROOT> in a rules file is a dangling pointer — nothing expands
    # it there, so the one line leading to the authoritative criteria would lead
    # nowhere. Resolve it against the checkout actually being installed from.
    body = body.replace("<AQG_ROOT>", str(aqg_root))
    return f"{RULE_MARKER}\nclient_id: {client_id}\n\n{body}"


def _install_rule(path: Path, root: Path, text: str) -> bool:
    if path.is_symlink():
        raise InstallError(f"refusing to write rule through symlink: {path}")
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if RULE_MARKER in current:
        start = current.index(RULE_MARKER)
        prefix = current[:start].rstrip()
        rendered = (prefix + "\n\n" if prefix else "") + text
    else:
        rendered = (current.rstrip() + "\n\n" if current.strip() else "") + text
    if current == rendered:
        return False
    if path.exists():
        _backup(path, root)
    _atomic_write(path, rendered)
    return True


def _load_json(path: Path, default: dict[str, object]) -> dict[str, object]:
    if not path.exists():
        return copy.deepcopy(default)
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"refusing non-regular settings file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot parse settings {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"settings top-level must be a JSON object: {path}")
    return value


def _hook_command(aqg_root: Path, client_id: str, script: str) -> str:
    values = [
        sys.executable,
        str((aqg_root / "scripts" / "agent_client_aqg_hook.py").resolve()),
        "--client",
        "trae" if client_id.startswith("trae") else "devin",
        "--aqg-root",
        str(aqg_root.resolve()),
        "--hook",
        script,
        "--managed-id",
        MANAGED_ID,
    ]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return " ".join(shlex.quote(value) for value in values)


def _hook_specs(aqg_root: Path, profile: ClientProfile) -> dict[str, list[dict[str, object]]]:
    if profile.hooks_kind is None:
        return {}
    for script in HOOK_SCRIPTS:
        if not (aqg_root / "agent-packs" / "claude-code" / "hooks" / script).is_file():
            raise InstallError(f"AQG hook script is missing: {script}")

    def entry(script: str) -> dict[str, object]:
        item: dict[str, object] = {
            "type": "command",
            "command": _hook_command(aqg_root, profile.client_id, script),
        }
        if script in BLOCKING_HOOKS:
            item["blocking"] = True
        return item

    specs: dict[str, list[dict[str, object]]] = {
        "UserPromptSubmit": [{"matcher": "", "hooks": [entry("userpromptsubmit_handoff_mandate.sh")]}],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [entry("pretooluse_bash_skill_validator.sh")]},
            {"matcher": "Bash|Write|Edit|MultiEdit", "hooks": [entry("pretooluse_secret_scan.sh")]},
        ],
        "PostToolUse": [
            {
                "matcher": "Bash|Write|Edit|MultiEdit",
                "hooks": [
                    entry("posttooluse_bash_error_debugging_reminder.sh"),
                    entry("posttooluse_skill_edit_reminder.sh"),
                    entry("posttooluse_code_construction_reminder.sh"),
                    entry("posttooluse_test_quality_reminder.sh"),
                    entry("posttooluse_security_review_reminder.sh"),
                ],
            }
        ],
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    entry("precompact_closeout_reminder.sh"),
                    entry("wip_checkpoint_save.sh"),
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "",
                "hooks": [
                    entry("sessionstart_preflight.sh"),
                    entry("wip_checkpoint_recover.sh"),
                    # Managed update check. A trigger only: it starts a detached
                    # process and returns, so no session waits on a network round
                    # trip, and it is kept out of BLOCKING_HOOKS so a slow remote
                    # can never stop a session from starting.
                    entry("sessionstart_update_check.sh"),
                ],
            }
        ],
    }
    if profile.hooks_kind == "devin":
        specs["PostCompaction"] = [
            {"matcher": "", "hooks": [entry("precompact_closeout_reminder.sh")]}
        ]
    return specs


def _owned_hook(hook: object) -> bool:
    return isinstance(hook, dict) and MANAGED_ID in str(hook.get("command", ""))


def _block_has_owned_hook(block: object) -> bool:
    if not isinstance(block, dict):
        return False
    hooks = block.get("hooks", [])
    if not isinstance(hooks, list):
        return False
    return any(_owned_hook(hook) for hook in hooks)


def _merge_hooks(existing: dict[str, object], specs: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for event, blocks in existing.items():
        if isinstance(blocks, list):
            kept = []
            for block in blocks:
                if not isinstance(block, dict):
                    kept.append(block)
                    continue
                hooks = block.get("hooks", [])
                if not isinstance(hooks, list):
                    kept.append(block)
                    continue
                kept_hooks = [hook for hook in hooks if not _owned_hook(hook)]
                if kept_hooks:
                    copied = dict(block)
                    copied["hooks"] = kept_hooks
                    kept.append(copied)
            if kept:
                result[event] = kept
        else:
            result[event] = blocks
    for event, blocks in specs.items():
        result[event] = [*result.get(event, []), *copy.deepcopy(blocks)] if isinstance(result.get(event), list) else copy.deepcopy(blocks)
    return result


def _settings_hooks(path: Path, profile: ClientProfile, scope: str) -> dict[str, object]:
    data = _load_json(path, {})
    if profile.hooks_kind == "devin" and scope == "user":
        hooks = data.get("hooks", {})
        return hooks if isinstance(hooks, dict) else {}
    return data


def _write_hooks(path: Path, root: Path, profile: ClientProfile, scope: str, hooks: dict[str, object]) -> bool:
    data = _load_json(path, {})
    if profile.hooks_kind == "devin" and scope == "user":
        data["hooks"] = hooks
        rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        rendered = json.dumps(hooks, ensure_ascii=False, indent=2) + "\n"
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if current == rendered:
        return False
    if path.exists():
        _backup(path, root)
    _atomic_write(path, rendered)
    return True


def _install_hooks(path: Path, root: Path, profile: ClientProfile, scope: str, aqg_root: Path) -> bool:
    specs = _hook_specs(aqg_root, profile)
    existing = _settings_hooks(path, profile, scope)
    merged = _merge_hooks(existing, specs)
    return _write_hooks(path, root, profile, scope, merged)


def _strip_hooks(existing: dict[str, object]) -> tuple[dict[str, object], bool]:
    result: dict[str, object] = {}
    changed = False
    for event, blocks in existing.items():
        if not isinstance(blocks, list):
            result[event] = blocks
            continue
        kept = []
        for block in blocks:
            if not isinstance(block, dict):
                kept.append(block)
                continue
            hooks = block.get("hooks", [])
            if not isinstance(hooks, list):
                kept.append(block)
                continue
            kept_hooks = [hook for hook in hooks if not _owned_hook(hook)]
            changed = changed or len(kept_hooks) != len(hooks)
            if kept_hooks:
                copied = dict(block)
                copied["hooks"] = kept_hooks
                kept.append(copied)
        if kept:
            result[event] = kept
    return result, changed


def _apply(args: argparse.Namespace, profile: ClientProfile, paths: ClientPaths, aqg_root: Path) -> int:
    changed = False
    # Inspect state as it exists before this apply's own writes: a legacy root
    # only self-heals when its content predates this call, not when this call
    # is the one creating it.
    legacy_siblings = (
        _self_heal_legacy_owners(aqg_root, paths.skills_root, profile.client_id)
        if args.scope == "user"
        else ()
    )
    for source in _skill_sources(aqg_root):
        changed = (
            _install_skill(source, paths.skills_root / source.name, paths.skills_root, args.mode)
            or changed
        )
    changed = _claim_skills_root(paths.skills_root, profile.client_id) or changed
    for sibling_id in legacy_siblings:
        changed = _claim_skills_root(paths.skills_root, sibling_id) or changed
    if paths.rules_path is not None and paths.rules_root is not None:
        changed = (
            _install_rule(paths.rules_path, paths.rules_root, _rule_text(aqg_root, profile.client_id))
            or changed
        )
    if paths.settings_path is not None and not args.no_hooks:
        changed = (
            _install_hooks(paths.settings_path, paths.config_root, profile, args.scope, aqg_root)
            or changed
        )
    print(
        f"OK: AQG {profile.client_id} {args.scope} support "
        f"{'updated' if changed else 'already current'} at {paths.config_root}"
    )
    print(f"support_level: {profile.support_level}")
    for item in profile.limitations:
        print(f"limitation: {item}")
    if args.no_hooks:
        print("INFO: lifecycle hooks skipped by --no-hooks")
    return EXIT_OK


def _verify(args: argparse.Namespace, profile: ClientProfile, paths: ClientPaths, aqg_root: Path) -> int:
    problems: list[str] = []
    for source in _skill_sources(aqg_root):
        target = paths.skills_root / source.name
        marker = _read_marker(target) if target.exists() else None
        if marker is None:
            problems.append(f"missing managed skill: {source.name}")
    if paths.rules_path is not None:
        expected = _rule_text(aqg_root, profile.client_id)
        if not paths.rules_path.is_file() or expected not in paths.rules_path.read_text(encoding="utf-8"):
            problems.append("missing managed rule")
    if paths.settings_path is not None and not args.no_hooks:
        try:
            actual = _settings_hooks(paths.settings_path, profile, args.scope)
        except InstallError as exc:
            problems.append(str(exc))
            actual = {}
        expected = _hook_specs(aqg_root, profile)
        for event in expected:
            blocks = actual.get(event)
            if not isinstance(blocks, list) or not any(_block_has_owned_hook(block) for block in blocks):
                problems.append(f"missing managed hook event: {event}")
    if problems:
        print(f"AQG {profile.client_id} support: NOT INSTALLED")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_ERROR
    print(f"AQG {profile.client_id} support: INSTALLED")
    print(f"support_level: {profile.support_level}")
    print(f"target: {paths.config_root}")
    return EXIT_OK


def _uninstall(args: argparse.Namespace, profile: ClientProfile, paths: ClientPaths, aqg_root: Path) -> int:
    changed = False
    # Symmetric with _apply: a pre-024 shared root carries managed skills but no
    # owners ledger, so an absent ledger does not mean this profile is the last
    # claimant - it means ownership is unprovable. Adopting the structural
    # siblings before releasing keeps skills a sibling may still be using;
    # uninstalling that sibling afterwards is what finally removes them. Without
    # this, a direct uninstall (no fixed apply first) wiped the shared root.
    legacy_siblings = (
        _self_heal_legacy_owners(aqg_root, paths.skills_root, profile.client_id)
        if args.scope == "user"
        else ()
    )
    for sibling_id in legacy_siblings:
        changed = _claim_skills_root(paths.skills_root, sibling_id) or changed
    released, remaining_owners = _release_skills_root(paths.skills_root, profile.client_id)
    changed = released or changed
    if legacy_siblings:
        print(
            f"INFO: AQG skills kept at {paths.skills_root}; the root predates AQG "
            "ownership tracking, so it is assumed to still serve "
            + ", ".join(legacy_siblings)
            + ". Uninstall those profiles to remove the shared skills."
        )
    elif remaining_owners:
        print(
            f"INFO: AQG skills kept at {paths.skills_root}; still claimed by "
            + ", ".join(remaining_owners)
        )
    elif paths.skills_root.is_dir():
        for target in sorted(paths.skills_root.glob("aqg-*")):
            if _read_marker(target) is None:
                continue
            if _is_link_install(target):
                _remove_link(target)
            else:
                _backup(target, paths.skills_root)
                shutil.rmtree(target)
            changed = True
    if paths.rules_path is not None and paths.rules_path.is_file():
        current = paths.rules_path.read_text(encoding="utf-8")
        if RULE_MARKER in current:
            _backup(paths.rules_path, paths.rules_root or paths.config_root)
            prefix = current[: current.index(RULE_MARKER)].rstrip()
            _atomic_write(paths.rules_path, prefix + ("\n" if prefix else ""))
            changed = True
    if paths.settings_path is not None and paths.settings_path.exists():
        existing = _settings_hooks(paths.settings_path, profile, args.scope)
        stripped, hook_changed = _strip_hooks(existing)
        if hook_changed:
            changed = (
                _write_hooks(paths.settings_path, paths.config_root, profile, args.scope, stripped)
                or changed
            )
    print(
        f"OK: AQG {profile.client_id} {args.scope} support "
        f"{'uninstalled' if changed else 'not installed'} at {paths.config_root}"
    )
    return EXIT_OK


def _is_installed(args: argparse.Namespace, profile: ClientProfile, paths: ClientPaths, aqg_root: Path) -> int:
    return EXIT_OK if _verify(args, profile, paths, aqg_root) == EXIT_OK else EXIT_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install AQG support for Trae, Zed, and Devin clients")
    parser.add_argument("--client", choices=sorted(PROFILES), required=True)
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--aqg-root")
    parser.add_argument("--mode", choices=SKILL_INSTALL_MODES, default="link")
    parser.add_argument("--no-hooks", action="store_true")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--is-installed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    profile = PROFILES[args.client]
    try:
        aqg_root = _resolve_aqg_root(args.aqg_root)
        paths = _client_paths(args, profile)
        # Read-only actions never touch files, so they run without a backup session.
        if args.verify:
            return _verify(args, profile, paths, aqg_root)
        if not args.apply and not args.uninstall:
            return _is_installed(args, profile, paths, aqg_root)
        action = _apply if args.apply else _uninstall
        _open_backups(args, profile, paths, aqg_root)
        ok = False
        try:
            result = action(args, profile, paths, aqg_root)
            ok = result == EXIT_OK
            return result
        finally:
            _close_backups(ok)
    except (InstallError, OSError, UnicodeError, BackupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
