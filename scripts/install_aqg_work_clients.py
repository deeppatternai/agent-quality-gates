#!/usr/bin/env python3
"""Install managed AQG support for WorkBuddy, CodeBuddy, Trae, Kimi, and Qoder work clients."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

try:
    from _aqg_backup import BackupError, BackupSession, migrate_legacy
except ModuleNotFoundError:
    from scripts._aqg_backup import BackupError, BackupSession, migrate_legacy


# Persist the managed entrance in client configuration, not its current target.
REPO_ROOT = Path(__file__).absolute().parent.parent
CURSOR_ADAPTER = REPO_ROOT / "scripts" / "cursor_aqg_hook.py"
# Version-controlled skill roster manifest (same file check_fixture_mix.py anchors
# its I6 deletion-guard floor to) — the required-source set for an install.
SKILLS_MANIFEST = "skills.list"
MANAGED_MARKER = ".aqg-work-client-managed.json"
LINK_MARKER_DIR = "managed-links"
MANAGER = "aqg-work-client-support"
MARKER_MANAGERS = {MANAGER, "AQG"}
RULE_MARKER = "<!-- AQG WORK CLIENT MANAGED RULE: do not edit generated content -->"
REPORT_MARKER = "<!-- AQG WORK CLIENT SUPPORT REPORT: managed by AQG -->"
HOOK_SENTINEL = "cursor_aqg_hook.py"
BACKUP_SUFFIX = ".aqg-work-client.bak"  # legacy adjacent suffix, migrated into the central store
LEGACY_BACKUP_DIR = "aqg-backups"  # legacy owned dir, migrated into the central store

# Central-store sessions open for the current apply/uninstall run, as
# (source_root, session) pairs. Most clients need one (client_root); kimi-work
# skills may live outside client_root and get a second session rooted there.
_SESSIONS: "list[tuple[Path, BackupSession]]" = []
HOOK_EVENTS = (
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "Stop",
    "UserPromptSubmit",
)
JSON_HOOK_EVENTS = HOOK_EVENTS


@dataclass(frozen=True)
class WorkClientProfile:
    client_id: str
    support_level: str
    user_dir: str
    project_dir: str
    skills: bool
    rules: bool
    mcp: bool
    hooks: bool
    hooks_format: str
    hook_blocking: bool
    degradation: str
    docs: tuple[str, ...]


@dataclass(frozen=True)
class KimiWorkRootResolution:
    skills_root: Path
    discovery_source: str
    fallback_reason: str | None = None


@dataclass(frozen=True)
class SkillInstallSummary:
    changed: bool
    requested_mode: str
    effective_modes: Counter[str]
    fallback_reason: str | None = None

    @property
    def effective_mode(self) -> str:
        if len(self.effective_modes) == 1:
            return next(iter(self.effective_modes))
        return "mixed"


PROFILES = {
    "workbuddy": WorkClientProfile(
        client_id="workbuddy",
        support_level="partial",
        user_dir=".workbuddy",
        project_dir=".workbuddy",
        skills=True,
        rules=True,
        mcp=False,
        hooks=False,
        hooks_format="none",
        hook_blocking=False,
        degradation="WorkBuddy official public docs expose skills/agents, but no local lifecycle hook contract.",
        docs=("https://www.workbuddy.ai/docs", "https://www.workbuddy.ai/agents"),
    ),
    "codebuddy": WorkClientProfile(
        client_id="codebuddy",
        support_level="full",
        user_dir=".codebuddy",
        project_dir=".codebuddy",
        skills=True,
        rules=True,
        mcp=True,
        hooks=True,
        hooks_format="json",
        hook_blocking=True,
        degradation="None for the documented CLI profile; IDE behavior still depends on host hook loading.",
        docs=(
            "https://www.codebuddy.ai/docs/cli/hooks",
            "https://www.codebuddy.ai/docs/cli/skills",
            "https://www.codebuddy.ai/docs/cli/mcp",
        ),
    ),
    "workbuddy-ai": WorkClientProfile(
        client_id="workbuddy-ai",
        support_level="full",
        user_dir=".workbuddy-ai",
        project_dir=".workbuddy-ai",
        skills=True,
        rules=True,
        mcp=True,
        hooks=True,
        hooks_format="json",
        hook_blocking=True,
        degradation=(
            "None for the documented embedded-CLI profile; WorkBuddy AI is an independent "
            "product (bundle id com.workbuddy.workbuddy-ai) with its own root and ownership, "
            "not a shared identity with CodeBuddy Agent CLI or legacy WorkBuddy."
        ),
        docs=(
            "local evidence: WorkBuddy AI product.json customUserDataDir=.workbuddy-ai",
            "local evidence: WorkBuddy AI main process sets WORKBUDDY_CONFIG_DIR/"
            "CODEBUDDY_CONFIG_DIR to ~/.workbuddy-ai before launching its embedded CLI",
        ),
    ),
    "kimi-work": WorkClientProfile(
        client_id="kimi-work",
        support_level="partial",
        user_dir=".kimi-work",
        project_dir=".kimi-work",
        skills=True,
        rules=False,
        mcp=False,
        hooks=False,
        hooks_format="none",
        hook_blocking=False,
        degradation=(
            "Kimi Work Desktop local Daimon skills are manageable, but rules, MCP, and blocking "
            "lifecycle hook gates have no reliable public contract."
        ),
        docs=(
            "https://kimi.moonshot.cn/",
            "local evidence: Kimi Work Daimon shareDir logs/process command line",
        ),
    ),
    "kimi-code": WorkClientProfile(
        client_id="kimi-code",
        support_level="partial",
        user_dir=".kimi-code",
        project_dir=".kimi-code",
        skills=True,
        rules=True,
        mcp=True,
        hooks=True,
        hooks_format="toml",
        hook_blocking=False,
        degradation="Kimi Code hooks are documented fail-open, so AQG cannot treat them as the only safety gate.",
        docs=(
            "https://moonshotai.github.io/kimi-code/en/customization/hooks",
            "https://moonshotai.github.io/kimi-code/en/customization/skills.html",
            "https://moonshotai.github.io/kimi-code/en/customization/mcp.html",
        ),
    ),
    "qoderwork": WorkClientProfile(
        client_id="qoderwork",
        support_level="partial",
        user_dir=".qoderwork",
        project_dir=".qoderwork",
        skills=True,
        rules=True,
        mcp=True,
        hooks=True,
        hooks_format="json",
        hook_blocking=True,
        degradation="QoderWork hooks are documented, but SessionStart/PreCompact/WIP save-recover parity with Qoder CLI is not proven.",
        docs=(
            "https://docs.qoder.com/qoderwork/introduction",
            "https://docs.qoder.com/qoderwork/connectors",
            "https://docs.qoder.com/qoderwork/hooks",
        ),
    ),
    "qoderwake": WorkClientProfile(
        client_id="qoderwake",
        support_level="partial",
        user_dir=".qoderwake",
        project_dir=".qoderwake",
        skills=True,
        rules=True,
        mcp=True,
        hooks=False,
        hooks_format="none",
        hook_blocking=False,
        degradation="QoderWake Waker skills/connectors can carry AQG reminders; local blocking lifecycle hooks are not documented.",
        docs=(
            "https://docs.qoder.com/qoderwake/overview",
            "https://docs.qoder.com/qoderwake/skills-and-integrations",
            "https://docs.qoder.com/qoderwake/manage-wakers",
        ),
    ),
}

# Profiles that used to live here and now belong to another adapter. Kept as an
# accepted --client value so an existing script or runbook gets a redirect
# instead of an argparse "invalid choice" that hides where the profile went.
# `trae-work` writes the shared `~/.trae/skills` root, which is owned by the
# agent-client adapter along with trae / trae-cn / trae-work-cn.
RETIRED_PROFILES = {
    "trae-work": "install_aqg_agent_clients.py",
}


class InstallError(RuntimeError):
    pass


def _required_skill_names() -> set[str]:
    """Skill names the version-controlled skills.list roster manifest requires:
    one name per line, '#' comments and blank lines ignored (utf-8-sig so a
    stray BOM cannot forge a phantom name). The manifest is the deletion-guard
    FLOOR, so it is the REQUIRED set, not the allowed set — an extra packaged
    aqg-* skill beyond the roster stays installable. Absent/unreadable/empty is
    fail-closed: nothing anchors the required set, so an install that could
    silently ship no skills is refused."""
    manifest = REPO_ROOT / SKILLS_MANIFEST
    try:
        text = manifest.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise InstallError(
            f"cannot read AQG skill roster manifest {manifest}: {_safe_error_summary(exc)}"
        ) from exc
    required = {name for name in (line.strip() for line in text.splitlines()) if name and not name.startswith("#")}
    if not required:
        raise InstallError(f"AQG skill roster manifest declares no skills: {manifest}")
    return required


def _skill_sources() -> list[Path]:
    sources = sorted(path for path in (REPO_ROOT / "skills").glob("aqg-*") if (path / "SKILL.md").is_file())
    # An absent packaged source used to shrink this list silently, so _install_skills()
    # no-opped and apply/verify could report success with no AQG skills installed.
    missing = sorted(_required_skill_names() - {source.name for source in sources})
    if missing:
        raise InstallError(
            f"missing required packaged AQG skill source(s): {', '.join(missing)} "
            f"(expected skills/<name>/SKILL.md under {REPO_ROOT})"
        )
    return sources


def _safe_error_summary(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    for key in ("api_key", "accessToken", "refreshToken", "Authorization", "access_token", "refresh_token"):
        text = re.sub(re.escape(key) + r"\s*[:=]\s*\S+", "<redacted-key>=<redacted>", text, flags=re.IGNORECASE)
        text = re.sub(re.escape(key), "<redacted-key>", text, flags=re.IGNORECASE)
    return text[:240] or exc.__class__.__name__


def _source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in source.rglob("*") if item.is_file() and not item.is_symlink()):
        digest.update(path.relative_to(source).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _path_is_usable_for_discovery(skills_root: Path) -> bool:
    return skills_root.exists() or skills_root.parent.exists()


def _canonical_skills_root(path: Path) -> Path:
    return path.expanduser().resolve()


def _skills_root_from_share_dir(share_dir: Path) -> Path:
    return _canonical_skills_root(share_dir / "daimon" / "skills")


def _strip_command_token(token: str) -> str:
    return token.strip().strip('"').strip("'")


def _skills_root_from_kimi_command_line(command_line: str) -> KimiWorkRootResolution | None:
    config_match = re.search(r"--config(?:=|\s+)(\"[^\"]+\"|'[^']+'|\S+)", command_line)
    if config_match:
        config_path = Path(_strip_command_token(config_match.group(1)))
        parts = [part.lower() for part in config_path.parts]
        if "daimon-share" in parts and "runtime" in parts:
            share_index = parts.index("daimon-share")
            share_dir = Path(*config_path.parts[: share_index + 1])
            skills_root = _skills_root_from_share_dir(share_dir)
            if _path_is_usable_for_discovery(skills_root):
                return KimiWorkRootResolution(skills_root, "running Kimi/Daimon process --config")

    bundle_match = re.search(r"([A-Za-z]:[\\/][^\r\n\"']*?[\\/]daimon-bundle)[\\/]", command_line)
    if bundle_match:
        bundle = Path(bundle_match.group(1))
        skills_root = _skills_root_from_share_dir(bundle.parent / "daimon-share")
        if _path_is_usable_for_discovery(skills_root):
            return KimiWorkRootResolution(skills_root, "running Kimi/Daimon process bundle")
    return None


def _kimi_process_command_lines() -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    cmd = shutil.which("powershell.exe") or shutil.which("powershell")
    if cmd is None:
        return ()
    completed = subprocess.run(
        [
            cmd,
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match '^(Kimi|node|kimi-webbridge)\\.exe$' } | "
                "ForEach-Object { $_.CommandLine }"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ()
    return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())


def _is_same_or_under(path: Path, parent: Path) -> bool:
    path_key = os.path.normcase(str(path.expanduser().resolve()))
    parent_key = os.path.normcase(str(parent.expanduser().resolve()))
    return path_key == parent_key or path_key.startswith(parent_key + os.sep)


def _home_is_real_user_home(home: Path) -> bool:
    return _is_same_or_under(Path.home(), home) and _is_same_or_under(home, Path.home())


def _env_path_for_home(env_var: str, home: Path) -> Path | None:
    value = os.environ.get(env_var)
    if not value:
        return None
    path = Path(value).expanduser()
    if _is_same_or_under(path, home) or _home_is_real_user_home(home):
        return path
    return None


def _kimi_log_paths(home: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    appdata = _env_path_for_home("APPDATA", home)
    if appdata:
        paths.append(appdata / "kimi-desktop" / "logs" / "main.log")
    paths.append(home.expanduser() / "AppData" / "Roaming" / "kimi-desktop" / "logs" / "main.log")
    return tuple(dict.fromkeys(paths))


def _skills_root_from_kimi_log_line(line: str) -> tuple[Path, str] | None:
    share_match = re.search(r"shareDir seeded to\s+(.+?)(?:\s*$|\s+[;|])", line, flags=re.IGNORECASE)
    if share_match:
        return _skills_root_from_share_dir(Path(share_match.group(1).strip())), "Kimi Desktop log shareDir"
    release_match = re.search(r"released\b.*?skill\(s\).*?(?:->|→)\s+(.+?daimon[\\/]skills)\b", line, flags=re.IGNORECASE)
    if release_match:
        return _canonical_skills_root(Path(release_match.group(1).strip())), "Kimi Desktop log released skills"
    return None


def _kimi_rehome_marker_paths(home: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    appdata = _env_path_for_home("APPDATA", home)
    if appdata:
        candidates.append(appdata / "kimi-desktop" / "daimon-share" / "daimon" / ".rehomed")
    candidates.append(home.expanduser() / "AppData" / "Roaming" / "kimi-desktop" / "daimon-share" / "daimon" / ".rehomed")
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            candidates.append(Path(f"{letter}:\\KimiData\\daimon-share\\daimon\\.rehomed"))
    return tuple(dict.fromkeys(candidates))


def _kimi_exe_paths_from_registry_and_shortcuts(home: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    if os.name == "nt":
        try:
            import winreg

            registry_values = (
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Kimi", "DisplayIcon"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Kimi", "DisplayIcon"),
            )
            for hive, key_name, value_name in registry_values:
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        value, _kind = winreg.QueryValueEx(key, value_name)
                    if isinstance(value, str) and "Kimi.exe" in value:
                        paths.append(Path(value.strip('"').split(",")[0]))
                except OSError:
                    continue
        except ImportError:
            pass
    shortcut_candidates = (
        home.expanduser() / "Desktop" / "Kimi.lnk",
        home.expanduser() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Kimi.lnk",
    )
    for shortcut in shortcut_candidates:
        if shortcut.is_file():
            paths.append(shortcut)
    return tuple(dict.fromkeys(paths))


def _kimi_install_drive_share_candidates(exe_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    shares: list[Path] = []
    for exe_path in exe_paths:
        anchor = exe_path.anchor
        if not anchor:
            continue
        shares.append(Path(anchor) / "KimiData" / "daimon-share")
    return tuple(dict.fromkeys(shares))


def resolve_kimi_work_skills_root(
    *,
    explicit_skills_root: Path | None = None,
    home: Path | None = None,
    include_fallback: bool = True,
    allow_global_evidence: bool = True,
) -> KimiWorkRootResolution | None:
    resolved_home = Path.home() if home is None else home.expanduser()
    if explicit_skills_root is not None:
        expanded = explicit_skills_root.expanduser()
        if not expanded.is_absolute():
            raise InstallError("--skills-root must resolve to an absolute path")
        root = _canonical_skills_root(expanded)
        return KimiWorkRootResolution(root, "explicit --skills-root")

    for env_var in ("KIMI_WORK_SKILLS_ROOT", "KIMI_DESKTOP_SKILLS_ROOT"):
        value = os.environ.get(env_var)
        if value:
            return KimiWorkRootResolution(_canonical_skills_root(Path(value)), f"env {env_var}")

    if allow_global_evidence:
        for command_line in _kimi_process_command_lines():
            resolved = _skills_root_from_kimi_command_line(command_line)
            if resolved is not None:
                return resolved

    for log_path in _kimi_log_paths(resolved_home):
        if log_path.is_symlink() or not log_path.is_file():
            continue
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            parsed = _skills_root_from_kimi_log_line(line)
            if parsed is None:
                continue
            skills_root, source = parsed
            if _path_is_usable_for_discovery(skills_root):
                return KimiWorkRootResolution(skills_root, source)

    for marker in _kimi_rehome_marker_paths(resolved_home):
        if marker.is_symlink() or not marker.is_file():
            continue
        try:
            share_text = marker.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if not share_text:
            continue
        skills_root = _skills_root_from_share_dir(Path(share_text))
        if _path_is_usable_for_discovery(skills_root):
            return KimiWorkRootResolution(skills_root, "Kimi Daimon .rehomed marker")

    if allow_global_evidence:
        for share_dir in _kimi_install_drive_share_candidates(_kimi_exe_paths_from_registry_and_shortcuts(resolved_home)):
            skills_root = _skills_root_from_share_dir(share_dir)
            if share_dir.exists() or _path_is_usable_for_discovery(skills_root):
                return KimiWorkRootResolution(skills_root, "Kimi.exe install drive candidate")

    if not include_fallback:
        return None
    appdata = _env_path_for_home("APPDATA", resolved_home) or (resolved_home / "AppData" / "Roaming")
    return KimiWorkRootResolution(
        _canonical_skills_root(appdata / "kimi-desktop" / "daimon-share" / "daimon" / "skills"),
        "fallback APPDATA kimi-desktop daimon-share",
        "no higher-priority Kimi Work Daimon skills root evidence found",
    )


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
    data = FindData()
    handle = kernel32.FindFirstFileW(str(path), ctypes.byref(data))
    if handle == ctypes.c_void_p(-1).value:
        return False
    kernel32.FindClose(handle)
    return bool(data.attributes & 0x0010 and data.attributes & 0x0400 and data.reparse_tag == 0xA0000003)


def _is_link_install(path: Path) -> bool:
    return path.is_symlink() or _is_windows_junction(path)


def _same_resolved_path(left: Path, right: Path, *, strict: bool) -> bool:
    try:
        left_value = os.path.normpath(os.path.realpath(left))
        right_value = os.path.normpath(os.path.realpath(right))
        if _is_windows_host():
            left_value = os.path.normcase(left_value)
            right_value = os.path.normcase(right_value)
        if strict:
            left.resolve(strict=True)
            right.resolve(strict=True)
        return left_value == right_value
    except OSError:
        return False


def _refuse_symlink_path(path: Path, label: str) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink():
                raise InstallError(f"refusing to use {label} through symlink: {candidate}")


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


def _migrate_legacy_backups(
    client_root: Path, *, client_id: str, scope: str, project_root: Path | None, aqg_root: Path
) -> None:
    """Fold any legacy in-place backups (owned dir + adjacent .bak) into the central store."""
    items: list[tuple[Path, str]] = []
    legacy_owned = client_root / LEGACY_BACKUP_DIR
    if legacy_owned.is_dir() and not legacy_owned.is_symlink():
        items.append((legacy_owned, "_legacy/aqg-backups"))
    for path in sorted(client_root.rglob("*" + BACKUP_SUFFIX + "*")):
        if legacy_owned in path.parents:
            continue  # already covered by the owned-dir migration above
        if path.is_file() and not path.is_symlink():
            items.append((path, "_legacy/adjacent/" + path.relative_to(client_root).as_posix()))
    if items:
        migrate_legacy(
            client_id,
            client_root,
            items,
            scope=scope,
            project_root=project_root,
            installer="install_aqg_work_clients.py",
            aqg_root=aqg_root,
        )


def _open_backups(
    args: argparse.Namespace,
    profile: WorkClientProfile,
    client_root: Path,
    skills_root: Path,
) -> None:
    """Start central-store session(s) for this run, migrating legacy backups first."""
    global _SESSIONS
    scope = args.scope
    project_root = (
        Path(args.project_root).expanduser().resolve() if scope == "project" else None
    )
    aqg_root = args.aqg_root.expanduser().resolve()
    _migrate_legacy_backups(
        client_root,
        client_id=profile.client_id,
        scope=scope,
        project_root=project_root,
        aqg_root=aqg_root,
    )

    def _new(root: Path) -> BackupSession:
        return BackupSession(
            profile.client_id,
            root,
            scope=scope,
            project_root=project_root,
            installer="install_aqg_work_clients.py",
            aqg_root=aqg_root,
        )

    sessions: list[tuple[Path, BackupSession]] = [(client_root.resolve(), _new(client_root))]
    # kimi-work skills may live outside client_root (discovered/APPDATA root) -> back
    # them up in a session rooted there so relpath mirroring stays well-defined.
    if not _within(skills_root, client_root):
        sessions.append((skills_root.resolve(), _new(skills_root)))
    _SESSIONS = sessions


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


def _backup_adjacent(path: Path) -> Path | None:
    if not _SESSIONS:  # defensive: apply/uninstall always open a session first
        raise InstallError("internal error: backup session not open")
    _refuse_symlink_path(path, "backup source")
    return _dispatch(path).backup(path)


def _backup_owned(path: Path, client_root: Path) -> Path | None:
    if not _SESSIONS:  # defensive: apply/uninstall always open a session first
        raise InstallError("internal error: backup session not open")
    if path.is_symlink():
        raise InstallError(f"refusing to back up AQG asset through symlink: {path}")
    return _dispatch(path).backup(path)


def _marker_for(
    source: Path,
    *,
    requested_mode: str,
    effective_mode: str,
    target_client: str,
    skills_root: Path,
    discovery_source: str,
    fallback_reason: str | None = None,
) -> dict[str, object]:
    marker: dict[str, object] = {
        "manager": "AQG",
        "schema_version": 2,
        "skill": source.name,
        "target_client": target_client,
        "source": str(source.resolve()),
        "source_digest": _source_digest(source),
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
        "install_mode": effective_mode,
        "resolved_skills_root": str(skills_root.resolve()),
        "discovery_source": discovery_source,
    }
    if fallback_reason:
        marker["fallback_reason"] = fallback_reason
    if effective_mode == "link":
        marker["source_path"] = str(source.resolve())
    return marker


def _link_marker_path(target: Path) -> Path:
    return target.parent.parent / LINK_MARKER_DIR / f"{target.name}.json"


def _read_marker_file(marker: Path) -> dict[str, object] | None:
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get("manager") in MARKER_MANAGERS else None


def _read_marker(target: Path) -> dict[str, object] | None:
    if _is_link_install(target):
        return _read_marker_file(_link_marker_path(target))
    return _read_marker_file(target / MANAGED_MARKER)


def _marker_matches_expected(marker: dict[str, object] | None, expected: dict[str, object]) -> bool:
    if marker is None:
        return False
    if marker.get("manager") not in MARKER_MANAGERS:
        return False
    if marker.get("skill") != expected.get("skill"):
        return False
    marker_mode = marker.get("effective_mode") or marker.get("install_mode")
    expected_mode = expected.get("effective_mode") or expected.get("install_mode")
    if marker_mode != expected_mode:
        return False
    expected_client = expected.get("target_client")
    if expected_client is not None and marker.get("target_client", expected_client) != expected_client:
        return False
    expected_root = expected.get("resolved_skills_root")
    marker_root = marker.get("resolved_skills_root")
    if isinstance(expected_root, str) and isinstance(marker_root, str):
        if not _same_resolved_path(Path(marker_root), Path(expected_root), strict=False):
            return False
    if expected_mode != "link":
        return True
    source_path = marker.get("source_path")
    expected_source = expected.get("source_path") or expected.get("source")
    return isinstance(source_path, str) and isinstance(expected_source, str) and _same_resolved_path(
        Path(source_path), Path(expected_source), strict=False
    )


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
    except OSError:
        if not _create_windows_junction(source, target):
            raise


def _remove_managed_link(target: Path) -> None:
    if target.is_symlink():
        target.unlink()
    elif _is_windows_junction(target):
        os.rmdir(target)
    else:
        raise InstallError(f"refusing to remove non-link AQG skill as link: {target}")
    marker = _link_marker_path(target)
    if _read_marker_file(marker) is not None:
        marker.unlink()


def _install_skill(
    source: Path,
    skills_root: Path,
    client_root: Path,
    *,
    requested_mode: str,
    effective_mode: str,
    target_client: str,
    discovery_source: str,
    fallback_reason: str | None = None,
) -> bool:
    target = skills_root / source.name
    expected = _marker_for(
        source,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        target_client=target_client,
        skills_root=skills_root,
        discovery_source=discovery_source,
        fallback_reason=fallback_reason,
    )
    if target.exists() or target.is_symlink():
        marker = _read_marker(target)
        if marker is None:
            raise InstallError(f"refusing to overwrite existing skill: {target}")
        if effective_mode == "link" and _is_link_install(target) and _same_resolved_path(target, source, strict=True):
            marker_path = _link_marker_path(target)
            text = json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
            if marker_path.read_text(encoding="utf-8") != text:
                _atomic_write(marker_path, text)
                return True
            return False
        if _is_link_install(target):
            _remove_managed_link(target)
        else:
            _backup_owned(target, client_root)
            shutil.rmtree(target)
    skills_root.mkdir(parents=True, exist_ok=True)
    if effective_mode == "link":
        _atomic_write(_link_marker_path(target), json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
        try:
            _create_link(source.resolve(), target)
        except OSError:
            marker = _link_marker_path(target)
            if _read_marker_file(marker) is not None:
                marker.unlink()
            raise
        return True
    shutil.copytree(source, target)
    _atomic_write(target / MANAGED_MARKER, json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
    return True


def _install_skills(
    skills_root: Path,
    client_root: Path,
    *,
    requested_mode: str,
    target_client: str,
    discovery_source: str,
    strict_link: bool,
) -> SkillInstallSummary:
    changed = False
    effective_modes: Counter[str] = Counter()
    fallback_reason: str | None = None
    for source in _skill_sources():
        effective_mode = requested_mode
        reason = fallback_reason
        if requested_mode == "link" and fallback_reason is not None:
            effective_mode = "copy"
        try:
            changed = _install_skill(
                source,
                skills_root,
                client_root,
                requested_mode=requested_mode,
                effective_mode=effective_mode,
                target_client=target_client,
                discovery_source=discovery_source,
                fallback_reason=reason,
            ) or changed
        except OSError as exc:
            if requested_mode != "link" or strict_link:
                raise
            fallback_reason = _safe_error_summary(exc)
            changed = _install_skill(
                source,
                skills_root,
                client_root,
                requested_mode=requested_mode,
                effective_mode="copy",
                target_client=target_client,
                discovery_source=discovery_source,
                fallback_reason=fallback_reason,
            ) or changed
            effective_mode = "copy"
        effective_modes[effective_mode] += 1
    return SkillInstallSummary(changed, requested_mode, effective_modes, fallback_reason)


def _render_rule(profile: WorkClientProfile) -> str:
    template = (REPO_ROOT / "examples" / "aqg-codex-agents.example.md").read_text(encoding="utf-8")
    heading = "## Agent Quality Gates (AQG) engineering discipline"
    start = template.find(heading)
    if start < 0:
        raise InstallError(f"AQG rule heading missing from template: {heading}")
    # A literal <AQG_ROOT> in a rules file is a dangling pointer — nothing expands
    # it there, so the one line leading to the authoritative criteria would lead
    # nowhere. Resolve it against the checkout actually being installed from.
    body = template[start:].rstrip().replace("<AQG_ROOT>", str(REPO_ROOT))
    return f"{RULE_MARKER}\n\n# AQG support for {profile.client_id}\n\n{body}\n"


def _install_rule(client_root: Path, profile: WorkClientProfile) -> bool:
    rule_path = client_root / "rules" / "aqg.md"
    _refuse_symlink_path(rule_path, "AQG rule")
    rendered = _render_rule(profile)
    if rule_path.exists():
        current = rule_path.read_text(encoding="utf-8")
        if RULE_MARKER not in current:
            raise InstallError(f"refusing to overwrite existing rule: {rule_path}")
        if current == rendered:
            return False
        _backup_owned(rule_path, client_root)
    _atomic_write(rule_path, rendered)
    return True


def _install_mcp(client_root: Path, aqg_root: Path) -> bool:
    mcp_path = client_root / "mcp.json"
    _refuse_symlink_path(mcp_path, "MCP config")
    config = {"mcpServers": {}}
    if mcp_path.exists():
        try:
            config = json.loads(mcp_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise InstallError(f"cannot parse MCP config {mcp_path}: {exc}") from exc
        if not isinstance(config, dict):
            raise InstallError(f"MCP config must be a JSON object: {mcp_path}")
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError(f"mcpServers must be a JSON object: {mcp_path}")
    expected = {
        "command": "python3",
        "args": [str((aqg_root / "scripts" / "aqg_doctor.py").absolute()), "--no-cli"],
        "description": "AQG diagnostic connector placeholder; lifecycle gates remain hook/rule driven.",
    }
    if servers.get("aqg-support") == expected:
        return False
    servers["aqg-support"] = expected
    if mcp_path.exists():
        _backup_adjacent(mcp_path)
    _atomic_write(mcp_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return True


def _quoted_command(event: str, aqg_root: Path) -> str:
    args = [
        str(Path(sys.executable).resolve()),
        str((aqg_root / "scripts" / "cursor_aqg_hook.py").absolute()),
        _event_for_adapter(event),
        "--aqg-root",
        str(aqg_root.absolute()),
    ]
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    import shlex

    return " ".join(shlex.quote(arg) for arg in args)


def _event_for_adapter(event: str) -> str:
    return {
        "SessionStart": "sessionStart",
        "PreToolUse": "preToolUse",
        "PostToolUse": "postToolUse",
        "PostToolUseFailure": "postToolUseFailure",
        "PreCompact": "preCompact",
        "Stop": "stop",
        "UserPromptSubmit": "userPromptSubmit",
    }[event]


def _hook_entry(event: str, aqg_root: Path, profile: WorkClientProfile) -> dict[str, object]:
    entry: dict[str, object] = {"command": _quoted_command(event, aqg_root), "timeout": 30}
    if event == "PreToolUse" and profile.hook_blocking:
        entry["failClosed"] = True
    if event == "Stop":
        entry["loop_limit"] = 1
    return entry


def _json_hook_blocks(profile: WorkClientProfile, aqg_root: Path) -> dict[str, list[dict[str, object]]]:
    def command(event: str) -> dict[str, object]:
        entry = _hook_entry(event, aqg_root, profile)
        return {"type": "command", **entry}

    events = JSON_HOOK_EVENTS
    if profile.client_id == "qoderwork":
        events = ("PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit")

    blocks: dict[str, list[dict[str, object]]] = {}
    if "SessionStart" in events:
        blocks["SessionStart"] = [
            {"matcher": "startup|resume|clear|compact", "hooks": [command("SessionStart")]}
        ]
    if "PreToolUse" in events:
        blocks["PreToolUse"] = [
            {"matcher": "Shell|Bash|Write|Edit|MultiEdit", "hooks": [command("PreToolUse")]}
        ]
    if "PostToolUse" in events:
        blocks["PostToolUse"] = [
            {"matcher": "Write|Edit|MultiEdit", "hooks": [command("PostToolUse")]}
        ]
    if "PostToolUseFailure" in events:
        blocks["PostToolUseFailure"] = [
            {"matcher": "Shell|Bash", "hooks": [command("PostToolUseFailure")]}
        ]
    if "PreCompact" in events:
        blocks["PreCompact"] = [{"matcher": "", "hooks": [command("PreCompact")]}]
    if "Stop" in events:
        blocks["Stop"] = [{"matcher": "", "hooks": [command("Stop")]}]
    if "UserPromptSubmit" in events:
        blocks["UserPromptSubmit"] = [{"matcher": "", "hooks": [command("UserPromptSubmit")]}]
    return blocks


def _read_json(path: Path, default: dict[str, object]) -> dict[str, object]:
    _refuse_symlink_path(path, "JSON settings")
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise InstallError(f"cannot parse JSON settings {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"settings must be a JSON object: {path}")
    return value


def _is_managed_hook(value: object) -> bool:
    if isinstance(value, dict):
        if HOOK_SENTINEL in str(value.get("command", "")):
            return True
        hooks = value.get("hooks")
        return isinstance(hooks, list) and any(_is_managed_hook(item) for item in hooks)
    return False


def _install_json_hooks(client_root: Path, profile: WorkClientProfile, aqg_root: Path) -> bool:
    settings_path = client_root / "settings.json"
    settings = _read_json(settings_path, {"hooks": {}})
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"settings hooks must be a JSON object: {settings_path}")
    changed = False
    expected_blocks = _json_hook_blocks(profile, aqg_root)
    for event, expected in expected_blocks.items():
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise InstallError(f"settings hook event must be an array: {event}")
        filtered = [entry for entry in entries if not _is_managed_hook(entry)]
        replacement = filtered + expected
        if replacement != entries:
            hooks[event] = replacement
            changed = True
    if not changed:
        return False
    if settings_path.exists():
        _backup_adjacent(settings_path)
    _atomic_write(settings_path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    return True


def _toml_hook_block(aqg_root: Path) -> str:
    """Canonical owned block, shared with the read-only update inspector."""
    start = "# BEGIN AQG MANAGED HOOKS"
    end = "# END AQG MANAGED HOOKS"
    block_lines = [start, '# Kimi Code documents hook failures as fail-open; AQG labels these advisory.']
    for event in HOOK_EVENTS:
        command = _quoted_command(event, aqg_root).replace("\\", "\\\\").replace('"', '\\"')
        block_lines.extend(
            [
                "",
                "[[hooks]]",
                f'event = "{event}"',
                f'command = "{command}"',
                "timeout = 30",
            ]
        )
    block_lines.append(end)
    return '\n'.join(block_lines)


def _install_toml_hooks(client_root: Path, profile: WorkClientProfile, aqg_root: Path) -> bool:
    path = client_root / "config.toml"
    _refuse_symlink_path(path, "TOML hooks")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    prefix = existing.split('# BEGIN AQG MANAGED HOOKS', 1)[0].rstrip()
    rendered = ("\n\n".join(part for part in (prefix, _toml_hook_block(aqg_root)) if part) + "\n")
    if existing == rendered:
        return False
    if path.exists():
        _backup_adjacent(path)
    _atomic_write(path, rendered)
    return True


def _install_hooks(client_root: Path, profile: WorkClientProfile, aqg_root: Path) -> bool:
    if profile.hooks_format == "json":
        return _install_json_hooks(client_root, profile, aqg_root)
    if profile.hooks_format == "toml":
        return _install_toml_hooks(client_root, profile, aqg_root)
    return False


def _render_report(
    profile: WorkClientProfile,
    *,
    skills_root: Path | None = None,
    discovery_source: str | None = None,
    requested_mode: str | None = None,
    effective_mode: str | None = None,
    fallback_reason: str | None = None,
) -> str:
    docs = "\n".join(f"- {url}" for url in profile.docs)
    product = ""
    dynamic = ""
    if profile.client_id == "kimi-work":
        product = "- product=Kimi Work Desktop / Kimi Desktop\n"
        dynamic = (
            f"- resolved_skills_root: {skills_root.resolve() if skills_root is not None else '<unresolved>'}\n"
            f"- discovery_source: {discovery_source or '<unresolved>'}\n"
            f"- requested_mode: {requested_mode or '<unknown>'}\n"
            f"- effective_mode: {effective_mode or '<unknown>'}\n"
            f"- fallback: {'yes' if fallback_reason else 'no'}\n"
        )
        if fallback_reason:
            dynamic += f"- fallback_reason: {fallback_reason}\n"
        dynamic += (
            "- capability_boundary: AQG manages only `aqg-*` skills under the resolved Daimon skills "
            "root; Kimi Work rules, MCP, hooks, blocking gates, builtin skills, app.asar, and production "
            "configuration are not modified.\n"
        )
    return (
        f"{REPORT_MARKER}\n\n"
        f"# AQG client support report: {profile.client_id}\n\n"
        f"- client_id: {profile.client_id}\n"
        f"{product}"
        f"- support_level: {profile.support_level}\n"
        f"- skills: {'supported' if profile.skills else 'unsupported'}\n"
        f"- rules: {'supported' if profile.rules else 'unsupported'}\n"
        f"- MCP/connectors: {'supported' if profile.mcp else 'unsupported'}\n"
        f"- lifecycle hooks: {'supported' if profile.hooks else 'unsupported'}\n"
        f"- blocking hook gate: {'supported' if profile.hook_blocking else 'unsupported'}\n"
        f"{dynamic}"
        f"- degradation: {profile.degradation}\n\n"
        f"Official evidence:\n{docs}\n"
    )


def _install_report(
    client_root: Path,
    profile: WorkClientProfile,
    *,
    skills_root: Path | None = None,
    discovery_source: str | None = None,
    requested_mode: str | None = None,
    effective_mode: str | None = None,
    fallback_reason: str | None = None,
) -> bool:
    path = client_root / "aqg-support-report.md"
    rendered = _render_report(
        profile,
        skills_root=skills_root,
        discovery_source=discovery_source,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        fallback_reason=fallback_reason,
    )
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return False
    _atomic_write(path, rendered)
    return True


def _client_root(args: argparse.Namespace, profile: WorkClientProfile) -> Path:
    if args.scope == "user":
        return args.home.expanduser().resolve() / profile.user_dir
    if args.project_root is None:
        raise InstallError("--project-root is required for --scope project")
    return args.project_root.expanduser().resolve() / profile.project_dir


def _skills_root(args: argparse.Namespace, profile: WorkClientProfile, client_root: Path) -> KimiWorkRootResolution:
    if profile.client_id == "kimi-work":
        return resolve_kimi_work_skills_root(
            explicit_skills_root=args.skills_root,
            home=args.home,
            include_fallback=True,
        )  # type: ignore[return-value]
    return KimiWorkRootResolution(client_root / "skills", "client root skills directory")


def _mode_summary(modes: Counter[str]) -> str:
    return ", ".join(f"{mode}:{count}" for mode, count in sorted(modes.items())) or "none"


def _print_resolution(
    *,
    profile: WorkClientProfile,
    skills_root: Path,
    discovery_source: str,
    requested_mode: str,
    effective_mode: str,
    fallback_reason: str | None,
) -> None:
    print(f"client_id={profile.client_id}")
    if profile.client_id == "kimi-work":
        print(f"resolved_skills_root={skills_root.resolve()}")
        print(f"discovery_source={discovery_source}")
    print(f"requested_mode={requested_mode}")
    print(f"effective_mode={effective_mode}")
    if fallback_reason:
        print(f"fallback_reason={fallback_reason}")


def _check_collisions(client_root: Path, profile: WorkClientProfile, skills_root: Path) -> None:
    if profile.skills:
        for source in _skill_sources():
            target = skills_root / source.name
            if (target.exists() or target.is_symlink()) and _read_marker(target) is None:
                raise InstallError(f"refusing to overwrite existing skill: {target}")
    if profile.rules:
        rule = client_root / "rules" / "aqg.md"
        _refuse_symlink_path(rule, "AQG rule")
        if rule.exists() and RULE_MARKER not in rule.read_text(encoding="utf-8"):
            raise InstallError(f"refusing to overwrite existing rule: {rule}")
    for path in (client_root / "settings.json", client_root / "config.toml", client_root / "mcp.json"):
        if path.exists() or path.is_symlink():
            _refuse_symlink_path(path, "managed config")


def _apply(args: argparse.Namespace) -> int:
    profile = PROFILES[args.client]
    aqg_root = args.aqg_root.expanduser().absolute()
    client_root = _client_root(args, profile)
    resolution = _skills_root(args, profile, client_root)
    skills_root = resolution.skills_root
    _check_collisions(client_root, profile, skills_root)
    changed = False
    skill_summary = SkillInstallSummary(False, args.mode, Counter(), None)
    if profile.skills:
        skill_summary = _install_skills(
            skills_root,
            client_root,
            requested_mode=args.mode,
            target_client=profile.client_id,
            discovery_source=resolution.discovery_source,
            strict_link=args.strict_link,
        )
        changed = skill_summary.changed or changed
    effective_mode = skill_summary.effective_mode if profile.skills else args.mode
    changed = _install_report(
        client_root,
        profile,
        skills_root=skills_root,
        discovery_source=resolution.discovery_source,
        requested_mode=args.mode,
        effective_mode=effective_mode,
        fallback_reason=skill_summary.fallback_reason or resolution.fallback_reason,
    ) or changed
    if profile.rules:
        changed = _install_rule(client_root, profile) or changed
    if profile.mcp:
        changed = _install_mcp(client_root, aqg_root) or changed
    if profile.hooks and not args.no_hooks:
        if not CURSOR_ADAPTER.is_file():
            raise InstallError(f"AQG hook adapter missing: {CURSOR_ADAPTER}")
        changed = _install_hooks(client_root, profile, aqg_root) or changed
    print(f"OK: AQG {profile.client_id} support {'updated' if changed else 'already current'} at {client_root}")
    _print_resolution(
        profile=profile,
        skills_root=skills_root,
        discovery_source=resolution.discovery_source,
        requested_mode=args.mode,
        effective_mode=effective_mode,
        fallback_reason=skill_summary.fallback_reason or resolution.fallback_reason,
    )
    print(f"support_level: {profile.support_level}")
    if profile.degradation and profile.degradation.lower() != "none":
        print(f"degradation: {profile.degradation}")
    if args.no_hooks:
        print("INFO: lifecycle hooks skipped by --no-hooks")
    return 0


def _skill_current(
    source: Path,
    skills_root: Path,
    *,
    profile: WorkClientProfile,
    requested_mode: str,
    discovery_source: str,
) -> tuple[bool, str | None]:
    target = skills_root / source.name
    marker = _read_marker(target) if target.exists() or target.is_symlink() else None
    effective_mode = marker.get("effective_mode") or marker.get("install_mode") if marker else None
    if not isinstance(effective_mode, str):
        effective_mode = "link" if _is_link_install(target) else "copy" if target.is_dir() else None
    if effective_mode is None:
        return False, None
    expected = _marker_for(
        source,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        target_client=profile.client_id,
        skills_root=skills_root,
        discovery_source=discovery_source,
        fallback_reason=marker.get("fallback_reason") if marker and isinstance(marker.get("fallback_reason"), str) else None,
    )
    has_skill_file = (target / "SKILL.md").is_file()
    if effective_mode == "link":
        ok = (
            _is_link_install(target)
            and has_skill_file
            and _marker_matches_expected(marker, expected)
            and _same_resolved_path(target, source, strict=True)
        )
        return ok, effective_mode
    ok = target.is_dir() and not target.is_symlink() and has_skill_file and _marker_matches_expected(marker, expected)
    return ok, effective_mode


def _has_managed_install(client_root: Path, profile: WorkClientProfile, skills_root: Path) -> bool:
    report = client_root / "aqg-support-report.md"
    if report.is_file() and REPORT_MARKER in report.read_text(encoding="utf-8"):
        return True
    if any(_read_marker(path) is not None for path in skills_root.glob("aqg-*")):
        return True
    for path in (client_root / "settings.json", client_root / "config.toml", client_root / "mcp.json"):
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8")
        if HOOK_SENTINEL in text or "aqg-support" in text:
            return True
    rule = client_root / "rules" / "aqg.md"
    return rule.is_file() and not rule.is_symlink() and RULE_MARKER in rule.read_text(encoding="utf-8")


def _verify(args: argparse.Namespace) -> int:
    profile = PROFILES[args.client]
    client_root = _client_root(args, profile)
    resolution = _skills_root(args, profile, client_root)
    skills_root = resolution.skills_root
    problems: list[str] = []
    report = client_root / "aqg-support-report.md"
    if not report.is_file() or REPORT_MARKER not in report.read_text(encoding="utf-8"):
        problems.append("support report drift/missing")
    if profile.client_id == "kimi-work" and not skills_root.is_dir():
        problems.append(f"resolved skills root missing: {skills_root}")
    mode_counts: Counter[str] = Counter()
    if profile.skills:
        for source in _skill_sources():
            current, effective_mode = _skill_current(
                source,
                skills_root,
                profile=profile,
                requested_mode=args.mode,
                discovery_source=resolution.discovery_source,
            )
            if effective_mode:
                mode_counts[effective_mode] += 1
            if not current:
                problems.append(f"skill drift/missing: {source.name}")
    if profile.rules:
        rule = client_root / "rules" / "aqg.md"
        if not rule.is_file() or RULE_MARKER not in rule.read_text(encoding="utf-8"):
            problems.append("rule drift/missing: aqg.md")
    if profile.mcp:
        mcp = client_root / "mcp.json"
        if not mcp.is_file() or "aqg-support" not in mcp.read_text(encoding="utf-8"):
            problems.append("MCP drift/missing: aqg-support")
    if profile.hooks and not args.no_hooks:
        hook_path = client_root / ("settings.json" if profile.hooks_format == "json" else "config.toml")
        if not hook_path.is_file() or HOOK_SENTINEL not in hook_path.read_text(encoding="utf-8"):
            problems.append("hook drift/missing")
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(f"OK: verified AQG {profile.client_id} support at {client_root}")
    _print_resolution(
        profile=profile,
        skills_root=skills_root,
        discovery_source=resolution.discovery_source,
        requested_mode=args.mode,
        effective_mode="mixed" if len(mode_counts) > 1 else (next(iter(mode_counts)) if mode_counts else args.mode),
        fallback_reason=resolution.fallback_reason,
    )
    if profile.skills:
        print(f"effective_mode_summary={_mode_summary(mode_counts)}")
    if profile.client_id == "kimi-work":
        print("support_surface=skills-supported")
    print(f"support_level: {profile.support_level}")
    return 0


def _remove_file_if_contains(path: Path, marker: str) -> bool:
    if path.is_symlink():
        raise InstallError(f"refusing to remove managed file through symlink: {path}")
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if marker not in text:
        return False
    _backup_adjacent(path)
    path.unlink()
    return True


def _strip_json_hooks(client_root: Path) -> bool:
    path = client_root / "settings.json"
    if not path.exists() and not path.is_symlink():
        return False
    settings = _read_json(path, {})
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        filtered = [entry for entry in entries if not _is_managed_hook(entry)]
        if filtered != entries:
            changed = True
            if filtered:
                hooks[event] = filtered
            else:
                del hooks[event]
    if not changed:
        return False
    if not hooks:
        settings.pop("hooks", None)
    _backup_adjacent(path)
    if settings:
        _atomic_write(path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    else:
        path.unlink()
    return True


def _strip_toml_hooks(client_root: Path) -> bool:
    path = client_root / "config.toml"
    if path.is_symlink():
        raise InstallError(f"refusing to remove managed TOML through symlink: {path}")
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    start = "# BEGIN AQG MANAGED HOOKS"
    end = "# END AQG MANAGED HOOKS"
    if start not in text or end not in text:
        return False
    before, rest = text.split(start, 1)
    _managed, after = rest.split(end, 1)
    rendered = (before.rstrip() + "\n\n" + after.lstrip()).strip() + "\n"
    _backup_adjacent(path)
    if rendered.strip():
        _atomic_write(path, rendered)
    else:
        path.unlink()
    return True


def _strip_mcp(client_root: Path) -> bool:
    path = client_root / "mcp.json"
    if not path.exists() and not path.is_symlink():
        return False
    config = _read_json(path, {})
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or "aqg-support" not in servers:
        return False
    del servers["aqg-support"]
    if not servers:
        config.pop("mcpServers", None)
    _backup_adjacent(path)
    if config:
        _atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    else:
        path.unlink()
    return True


def _uninstall(args: argparse.Namespace) -> int:
    profile = PROFILES[args.client]
    client_root = _client_root(args, profile)
    resolution = _skills_root(args, profile, client_root)
    skills_root = resolution.skills_root
    changed = False
    for target in list(skills_root.glob("aqg-*")):
        marker = _read_marker(target) if target.exists() or target.is_symlink() else None
        if marker is not None and _is_link_install(target):
            _remove_managed_link(target)
            changed = True
        elif marker is not None and target.is_dir():
            _backup_owned(target, client_root)
            shutil.rmtree(target)
            changed = True
        else:
            print(f"SKIP: unmanaged AQG skill not removed: {target}")
    marker_dir = skills_root.parent / LINK_MARKER_DIR
    if marker_dir.is_dir() and not marker_dir.is_symlink():
        for marker in marker_dir.glob("aqg-*.json"):
            if _read_marker_file(marker) is not None:
                marker.unlink()
                changed = True
        if not any(marker_dir.iterdir()):
            marker_dir.rmdir()
    changed = _remove_file_if_contains(client_root / "rules" / "aqg.md", RULE_MARKER) or changed
    changed = _remove_file_if_contains(client_root / "aqg-support-report.md", REPORT_MARKER) or changed
    changed = _strip_mcp(client_root) or changed
    changed = _strip_json_hooks(client_root) or changed
    changed = _strip_toml_hooks(client_root) or changed
    print(f"OK: AQG {profile.client_id} support {'uninstalled' if changed else 'not installed'} at {client_root}")
    _print_resolution(
        profile=profile,
        skills_root=skills_root,
        discovery_source=resolution.discovery_source,
        requested_mode=args.mode,
        effective_mode=args.mode,
        fallback_reason=resolution.fallback_reason,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install AQG support for WorkBuddy/CodeBuddy/Trae/Kimi/Qoder work clients")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--is-installed", action="store_true")
    parser.add_argument(
        "--client", choices=sorted({*PROFILES, *RETIRED_PROFILES}), required=True
    )
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--aqg-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--mode", choices=("link", "copy"), default="link")
    parser.add_argument("--skills-root", type=Path, help="explicit Kimi Work Daimon skills root override")
    parser.add_argument(
        "--strict-link",
        action="store_true",
        help="with --mode link, fail closed instead of falling back to copy when link creation fails",
    )
    parser.add_argument("--no-hooks", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    replacement = RETIRED_PROFILES.get(args.client)
    if replacement is not None:
        print(
            f"ERROR: {args.client} is no longer a work-client profile: it writes the "
            f"shared Trae skills root and is managed by {replacement}. "
            f'Run: python3 "$AQG_ROOT/scripts/{replacement}" --client {args.client} '
            '--scope user --aqg-root "$AQG_ROOT" --mode link --apply',
            file=sys.stderr,
        )
        return 2
    try:
        profile = PROFILES[args.client]
        client_root = _client_root(args, profile)
        resolution = _skills_root(args, profile, client_root)
        if args.is_installed:
            return 0 if _has_managed_install(client_root, profile, resolution.skills_root) else 1
        if args.verify:
            return _verify(args)
        # apply / uninstall mutate managed files -> run inside central-store session(s)
        action = _apply if args.apply else _uninstall
        _open_backups(args, profile, client_root, resolution.skills_root)
        ok = False
        try:
            result = action(args)
            ok = result == 0
            return result
        finally:
            _close_backups(ok)
    except (OSError, InstallError, BackupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
