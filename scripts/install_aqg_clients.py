#!/usr/bin/env python3
"""AQG multi-client installer wrapper.

Batch installation is opt-in by client id. The all mode expands every adapter
registered in the AQG client registry, regardless of support status label.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.parsers.expat import ExpatError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.aqg_client_registry import (
    ACTION_APPLY,
    ACTION_IS_INSTALLED,
    ACTION_UNINSTALL,
    ACTION_VERIFY,
    CLIENT_REGISTRY,
    ClientAdapterSpec,
    get_client,
)
from scripts.install_aqg_work_clients import resolve_kimi_work_skills_root


# main() exit-code contract (consumed by decision-engine install.sh):
#   0 every selected client succeeded
#   2 argument / client-selection error
#   3 no supported client detected on this host (not a failure)
#   4 at least one client failed and at least one succeeded
#   5 every selected client failed
# Client-level failures never reuse a child process return code, so callers can
# always tell "a client failed" apart from "the wrapper was called wrong".
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_CLIENTS = 3
EXIT_PARTIAL_FAILURE = 4
EXIT_ALL_FAILED = 5

CORE_CLIENTS = ("codex", "claude-code")
CURSOR_CLIENTS = ("cursor",)
PROJECT_ROOT_FRAGMENT = "$PROJECT_ROOT"
INSTALLED_SUPPORTED_STATUSES = ("supported", "full", "partial")
PROJECT_ROOT_ACTIONS = (
    ACTION_APPLY,
    ACTION_VERIFY,
    ACTION_UNINSTALL,
    ACTION_IS_INSTALLED,
)
HOOK_INSTALLER_FRAGMENTS = (
    "install_aqg_codex_hooks.py",
    "install_aqg_hooks.py",
)
NO_HOOKS_FLAG_INSTALLER_FRAGMENTS = (
    "install_cursor_support.py",
    "install_aqg_work_clients.py",
    "install_aqg_agent_clients.py",
    "install_aqg_pi.py",
)


@dataclass(frozen=True)
class RunContext:
    aqg_root: str
    git_remote: str
    git_commit: str


@dataclass(frozen=True)
class PlannedCommand:
    client_id: str
    command: str
    requires_project_root: bool = False


@dataclass(frozen=True)
class ClientRunResult:
    client_id: str
    ok: bool
    failed_command: str | None = None
    returncode: int | None = None
    skipped_commands: int = 0


@dataclass(frozen=True)
class SkippedClient:
    client_id: str
    reason: str


@dataclass(frozen=True)
class ClientDetectionEvidence:
    evidence_type: str
    value: str
    env_var: str | None = None


def _home_relative(relative: str) -> ClientDetectionEvidence:
    return ClientDetectionEvidence("home-relative", relative)


def _env_relative(env_var: str, relative: str = "") -> ClientDetectionEvidence:
    return ClientDetectionEvidence("env-relative", relative, env_var)


def _path_binary(*names: str) -> ClientDetectionEvidence:
    return ClientDetectionEvidence("path-binary", os.pathsep.join(names))


CLIENT_DETECTION_CANDIDATES: dict[str, tuple[ClientDetectionEvidence, ...]] = {
    "claude-code": (_home_relative(".claude"),),
    "cursor": (_home_relative(".cursor"),),
    "workbuddy": (_home_relative(".workbuddy"),),
    "workbuddy-ai": (_home_relative(".workbuddy-ai"),),
    "codebuddy": (_home_relative(".codebuddy"),),
    "kimi-work": (
        _env_relative("KIMI_WORK_SKILLS_ROOT"),
        _env_relative("KIMI_DESKTOP_SKILLS_ROOT"),
    ),
    "kimi-code": (
        _env_relative("KIMI_CODE_HOME"),
        _home_relative(".kimi-code"),
    ),
    "zed": (
        _env_relative("APPDATA", "Zed"),
        _env_relative("APPDATA", "zed"),
        _home_relative("AppData/Roaming/Zed"),
        _home_relative(".config/zed"),
        _path_binary("zed", "zed.exe", "zed.cmd"),
    ),
    "devin": (
        _env_relative("APPDATA", "devin"),
        _env_relative("APPDATA", "Devin"),
        _home_relative(".devin"),
        _home_relative(".config/devin"),
    ),
    "qoderwork": (_home_relative(".qoderwork"),),
    "qoderwake": (_home_relative(".qoderwake"),),
    "pi": (_home_relative(".pi/agent"),),
}


# Product identity, not directory presence. `~/.qoder` is shared by Qoder Desktop
# and Qoder CLI, and `~/.trae` by Trae IDE and TRAE SOLO, so a directory cannot
# say which product is installed - or whether one still is. These families are
# selected from exact evidence only: a macOS bundle id for the desktop apps, an
# executable on PATH for the CLIs.
APP_DIRS_ENV = "AQG_APP_DIRS"


@dataclass(frozen=True)
class MacOSProduct:
    app_name: str
    bundle_id: str


MACOS_PRODUCT_IDENTITIES: dict[str, tuple[MacOSProduct, ...]] = {
    "qoder": (
        MacOSProduct("Qoder.app", "com.qoder.app"),
        MacOSProduct("Qoder IDE.app", "com.qoder.ide"),
    ),
    "qoder-cn": (
        MacOSProduct("Qoder CN.app", "com.qodercn.app"),
        MacOSProduct("Qoder CN IDE.app", "com.aliyun.lingma.ide"),
    ),
    "trae": (MacOSProduct("Trae.app", "com.trae.app"),),
    "trae-cn": (MacOSProduct("Trae CN.app", "cn.trae.app"),),
    "trae-work": (MacOSProduct("TRAE SOLO.app", "com.trae.solo.app"),),
    "trae-work-cn": (MacOSProduct("TRAE SOLO CN.app", "cn.trae.solo.app"),),
}

CLI_RUNTIME_IDENTITIES: dict[str, tuple[str, ...]] = {
    "qoder-cli": ("qoder",),
    "qoder-cli-cn": ("qoder-cn",),
}

# Profiles that write different hook contracts into one settings.json. If both
# runtimes are proven, auto-selecting either would silently pick a contract for
# the operator, so neither is selected and the ambiguity is reported instead.
SHARED_HOOK_SURFACES: tuple[tuple[str, ...], ...] = (
    ("qoder", "qoder-cli"),
    ("qoder-cn", "qoder-cli-cn"),
)

IDENTITY_GATED_CLIENTS = frozenset(MACOS_PRODUCT_IDENTITIES) | frozenset(
    CLI_RUNTIME_IDENTITIES
)


@dataclass(frozen=True)
class DetectionResult:
    selected: tuple[str, ...]
    evidence: dict[str, str]
    conflicts: tuple[tuple[str, ...], ...]


class ClientSelectionError(ValueError):
    pass


class NoInstalledClientsError(ClientSelectionError):
    """--installed-supported found no supported client on this host.

    Distinct from a usage error: the caller asked a legitimate question and the
    answer is "nothing to configure here".
    """


def _commands_require_project_root(commands: tuple[PlannedCommand, ...]) -> bool:
    return any(item.requires_project_root for item in commands)


def _skipped_project_scope_clients(
    commands: tuple[PlannedCommand, ...],
) -> tuple[SkippedClient, ...]:
    skipped: list[SkippedClient] = []
    seen: set[str] = set()
    for item in commands:
        if not item.requires_project_root or item.client_id in seen:
            continue
        skipped.append(
            SkippedClient(
                client_id=item.client_id,
                reason="project-scope command(s) require --project-root /absolute/path/to/project",
            )
        )
        seen.add(item.client_id)
    return tuple(skipped)


def _should_skip_project_scope_for_installed_supported(
    args: argparse.Namespace,
    *,
    action: str,
) -> bool:
    if not args.installed_supported:
        return False
    if action not in PROJECT_ROOT_ACTIONS:
        return False
    return args.project_root is None and "PROJECT_ROOT" not in os.environ


def _should_show_installed_supported_skip_notice(args: argparse.Namespace) -> bool:
    return (
        args.installed_supported
        and args.project_root is None
        and "PROJECT_ROOT" not in os.environ
    )


def _filter_skipped_project_scope_commands(
    commands: tuple[PlannedCommand, ...],
    *,
    skip_project_scope: bool,
) -> tuple[tuple[PlannedCommand, ...], tuple[SkippedClient, ...]]:
    if not skip_project_scope:
        return commands, ()
    skipped_clients = _skipped_project_scope_clients(commands)
    # Drop only the commands that actually need a project root. A client whose
    # user-scope surface is installable without one keeps that command: dropping
    # the whole client would silently skip its skills and hooks too.
    return (
        tuple(item for item in commands if not item.requires_project_root),
        skipped_clients,
    )


def _resolve_project_root(
    project_root_arg: str | None,
    *,
    commands: tuple[PlannedCommand, ...],
    action: str,
) -> str | None:
    requires_project_root = _commands_require_project_root(commands)
    if not requires_project_root and project_root_arg is None:
        return None

    project_root = project_root_arg
    if project_root is None and requires_project_root and action != "plan":
        project_root = os.environ.get("PROJECT_ROOT")
    if not project_root:
        if action != "plan" and requires_project_root:
            raise ClientSelectionError(
                "PROJECT_ROOT required: selected project-scope adapter(s) need "
                "--project-root /absolute/path/to/project before commands can run."
            )
        return None
    path = Path(project_root).expanduser()
    if not path.is_absolute():
        raise ClientSelectionError("PROJECT_ROOT must be an absolute path")
    path = path.resolve()
    if not path.is_dir():
        raise ClientSelectionError(f"PROJECT_ROOT does not exist: {path}")
    return str(path)


def registry_batch_clients() -> tuple[str, ...]:
    return tuple(spec.client_id for spec in CLIENT_REGISTRY.values())


def no_hooks_unsupported_clients() -> tuple[str, ...]:
    return tuple(
        spec.client_id
        for spec in CLIENT_REGISTRY.values()
        if not spec.supports_no_hooks
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or run AQG installation for selected registry client adapters",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--clients",
        help="comma-separated registry client ids: " + ",".join(registry_batch_clients()),
    )
    target.add_argument(
        "--core",
        action="store_true",
        help="install the core client set: codex,claude-code",
    )
    target.add_argument(
        "--all-registry",
        "--all-supported",
        dest="all_supported",
        action="store_true",
        help=(
            "install every registry client adapter regardless of support_status label; "
            "--all-supported is a backward-compatible alias"
        ),
    )
    target.add_argument(
        "--installed-supported",
        action="store_true",
        help=(
            "install every locally detected registry client whose support_status is "
            "supported, full, or partial"
        ),
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--apply",
        action="store_true",
        help="execute apply commands; without this the default action is dry-run plan",
    )
    action.add_argument(
        "--verify",
        action="store_true",
        help="execute read-only verify commands",
    )
    action.add_argument(
        "--uninstall",
        action="store_true",
        help="execute uninstall commands for AQG-managed assets",
    )
    action.add_argument(
        "--is-installed",
        action="store_true",
        help="execute read-only installed-state checks",
    )
    hooks = parser.add_mutually_exclusive_group()
    hooks.add_argument(
        "--hooks",
        action="store_true",
        help="include hook install/refresh commands (default; retained for compatibility)",
    )
    hooks.add_argument(
        "--no-hooks",
        action="store_true",
        help=(
            "skip hook install/refresh where the selected installer supports it; "
            "Qoder-family profiles do not currently expose --no-hooks"
        ),
    )
    parser.add_argument(
        "--aqg-root",
        help="AQG checkout root to expose as AQG_ROOT; defaults to this repository",
    )
    parser.add_argument(
        "--project-root",
        help="project root required by project-scope adapters such as Qoder IDE profiles",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="home directory used for --installed-supported client detection",
    )
    return parser.parse_args(argv)


def _local_client_root(client_id: str, home: Path) -> Path | None:
    roots = _local_client_roots(client_id, home)
    return roots[0] if roots else None


def _dedupe_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _is_same_or_under(path: Path, parent: Path) -> bool:
    path_key = os.path.normcase(str(path))
    parent_key = os.path.normcase(str(parent))
    return path_key == parent_key or path_key.startswith(parent_key + os.sep)


def _env_appdata_for_home(home: Path) -> Path | None:
    return _env_root_for_home("APPDATA", home)


def _env_root_for_home(env_var: str, home: Path) -> Path | None:
    value = os.environ.get(env_var)
    if not value:
        return None
    env_path = Path(value).expanduser().resolve()
    home_path = home.expanduser().resolve()
    if _is_same_or_under(env_path, home_path):
        return env_path
    if home_path == Path.home().expanduser().resolve():
        return env_path
    return None


def _detection_candidate_paths(
    candidate: ClientDetectionEvidence,
    home: Path,
) -> tuple[Path, ...]:
    if candidate.evidence_type == "home-relative":
        return (home / Path(candidate.value),)
    if candidate.evidence_type == "env-relative":
        if candidate.env_var is None:
            return ()
        env_root = _env_root_for_home(candidate.env_var, home)
        if env_root is None:
            return ()
        if not candidate.value:
            return (env_root,)
        return (env_root / Path(candidate.value),)
    return ()


def _local_client_roots(client_id: str, home: Path) -> tuple[Path, ...]:
    if client_id == "codex":
        override = os.environ.get("CODEX_HOME")
        return (Path(override).expanduser(),) if override else (home / ".codex",)
    candidates = CLIENT_DETECTION_CANDIDATES.get(client_id, ())
    paths: list[Path] = []
    for candidate in candidates:
        paths.extend(_detection_candidate_paths(candidate, home))
    return _dedupe_paths(paths)


def _path_binary_exists(candidate: ClientDetectionEvidence) -> bool:
    if candidate.evidence_type != "path-binary":
        return False
    return any(shutil.which(name) is not None for name in candidate.value.split(os.pathsep))


def _is_installed_client_root(path: Path) -> bool:
    try:
        return path.expanduser().is_dir() and not path.is_symlink()
    except OSError:
        return False


def _macos_app_dirs(home: Path) -> tuple[Path, ...]:
    """Application directories to probe for product bundles.

    A fixture home must not inherit the real ``/Applications``, otherwise a test
    (or a caller passing ``--home``) would detect whatever is installed on the
    machine. Same containment rule the APPDATA evidence already uses.
    """
    override = os.environ.get(APP_DIRS_ENV)
    if override:
        return tuple(
            Path(item).expanduser() for item in override.split(os.pathsep) if item
        )
    if sys.platform != "darwin":
        return ()
    if home.expanduser().resolve() != Path.home().expanduser().resolve():
        return ()
    return (Path("/Applications"), home / "Applications")


def _bundle_identifier(app: Path) -> str | None:
    plist = app / "Contents" / "Info.plist"
    if plist.is_symlink() or not plist.is_file():
        return None
    try:
        with plist.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, ExpatError):
        # Unreadable or malformed metadata is not an identity: fail closed.
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("CFBundleIdentifier")
    return value if isinstance(value, str) and value else None


def _detect_macos_products(home: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for app_dir in _macos_app_dirs(home):
        for client_id, products in MACOS_PRODUCT_IDENTITIES.items():
            if client_id in found:
                continue
            for product in products:
                app = app_dir / product.app_name
                if not app.is_dir():
                    continue
                if _bundle_identifier(app) != product.bundle_id:
                    continue
                found[client_id] = (
                    f"macOS bundle {product.app_name} ({product.bundle_id}) in {app_dir}"
                )
                break
    return found


def _detect_cli_runtimes() -> dict[str, str]:
    found: dict[str, str] = {}
    for client_id, names in CLI_RUNTIME_IDENTITIES.items():
        for name in names:
            location = shutil.which(name)
            if location:
                found[client_id] = f"CLI executable {name} on PATH at {location}"
                break
    return found


def _directory_evidence(client_id: str, home: Path, allow_global_evidence: bool) -> str | None:
    if client_id == "kimi-work":
        resolved = resolve_kimi_work_skills_root(
            home=home,
            include_fallback=False,
            allow_global_evidence=allow_global_evidence,
        )
        if resolved is not None and (
            resolved.skills_root.exists() or resolved.skills_root.parent.exists()
        ):
            return f"Kimi Work skills root {resolved.skills_root} ({resolved.discovery_source})"
        return None
    for root in _local_client_roots(client_id, home):
        if _is_installed_client_root(root):
            return f"config directory {root}"
    for candidate in CLIENT_DETECTION_CANDIDATES.get(client_id, ()):
        if _path_binary_exists(candidate):
            return f"executable on PATH: {candidate.value}"
    return None


def detect_clients(home: Path | str | None = None) -> DetectionResult:
    """Select locally installed registry clients and say why each was selected."""
    resolved_home = Path.home() if home is None else Path(home).expanduser()
    allow_global_evidence = (
        resolved_home.expanduser().resolve() == Path.home().expanduser().resolve()
    )
    identity_evidence = {
        **_detect_macos_products(resolved_home),
        **_detect_cli_runtimes(),
    }

    conflicts: list[tuple[str, ...]] = []
    blocked: set[str] = set()
    for group in SHARED_HOOK_SURFACES:
        proven = tuple(client_id for client_id in group if client_id in identity_evidence)
        if len(proven) > 1:
            conflicts.append(group)
            blocked.update(proven)

    selected: list[str] = []
    evidence: dict[str, str] = {}
    for spec in CLIENT_REGISTRY.values():
        if spec.support_status not in INSTALLED_SUPPORTED_STATUSES:
            continue
        client_id = spec.client_id
        if client_id in IDENTITY_GATED_CLIENTS:
            if client_id in identity_evidence and client_id not in blocked:
                selected.append(client_id)
                evidence[client_id] = identity_evidence[client_id]
            continue
        found = _directory_evidence(client_id, resolved_home, allow_global_evidence)
        if found is not None:
            selected.append(client_id)
            evidence[client_id] = found
    return DetectionResult(tuple(selected), evidence, tuple(conflicts))


def installed_supported_clients(home: Path | str | None = None) -> tuple[str, ...]:
    return detect_clients(home).selected


def selected_clients(args: argparse.Namespace) -> tuple[str, ...]:
    if args.core:
        return CORE_CLIENTS
    if args.all_supported:
        return registry_batch_clients()
    if args.installed_supported:
        clients = installed_supported_clients(args.home)
        if not clients:
            raise NoInstalledClientsError(
                "no locally installed supported AQG clients detected; create a supported "
                "client config directory or pass --clients explicitly."
            )
        return clients
    raw = args.clients or ""
    requested = tuple(client.strip() for client in raw.split(",") if client.strip())
    if not requested:
        raise ClientSelectionError("--clients requires at least one client id")

    seen: set[str] = set()
    clients: list[str] = []
    rejected_unknown: list[str] = []
    for client_id in requested:
        spec = CLIENT_REGISTRY.get(client_id)
        if spec is None:
            rejected_unknown.append(client_id)
            continue
        if client_id not in seen:
            clients.append(client_id)
            seen.add(client_id)
    errors: list[str] = []
    if rejected_unknown:
        errors.append("unknown client(s): " + ", ".join(rejected_unknown))
    if errors:
        allowed = ", ".join(registry_batch_clients())
        raise ClientSelectionError("; ".join(errors) + f". Accepted registry clients: {allowed}.")
    return tuple(clients)


def _action(args: argparse.Namespace) -> str:
    if args.apply:
        return ACTION_APPLY
    if args.verify:
        return ACTION_VERIFY
    if args.uninstall:
        return ACTION_UNINSTALL
    if args.is_installed:
        return ACTION_IS_INSTALLED
    return "plan"


def _hooks_enabled(args: argparse.Namespace) -> bool:
    return not bool(args.no_hooks)


def _no_hooks_requested(args: argparse.Namespace) -> bool:
    return bool(args.no_hooks)


def _is_hook_installer_command(command: str) -> bool:
    return any(fragment in command for fragment in HOOK_INSTALLER_FRAGMENTS)


def _with_flag(command: str, flag: str) -> str:
    if flag in command.split():
        return command
    return f"{command} {flag}"


def _validate_hooks_request(
    clients: tuple[str, ...],
    *,
    action: str,
    no_hooks_requested: bool,
) -> None:
    if not no_hooks_requested:
        return
    unsupported = [client for client in clients if client in no_hooks_unsupported_clients()]
    if unsupported:
        raise ClientSelectionError(
            "--no-hooks cannot be honored for "
            + ", ".join(unsupported)
            + ": selected adapter(s) do not expose --no-hooks. "
            "Omit --no-hooks or install those profile(s) with dedicated installer options."
        )


def _commands_for_client(
    spec: ClientAdapterSpec,
    *,
    action: str,
    hooks_enabled: bool,
    no_hooks_requested: bool,
) -> tuple[str, ...]:
    if action == "plan":
        commands = spec.adapter_actions[action]
    else:
        commands = spec.adapter_actions[action]
    if action in ("plan", ACTION_APPLY, ACTION_VERIFY, ACTION_IS_INSTALLED) and not hooks_enabled:
        commands = tuple(command for command in commands if not _is_hook_installer_command(command))
    if (
        no_hooks_requested
        and spec.supports_no_hooks
        and action in ("plan", ACTION_APPLY, ACTION_VERIFY)
    ):
        commands = tuple(
            _with_flag(command, "--no-hooks")
            if any(fragment in command for fragment in NO_HOOKS_FLAG_INSTALLER_FRAGMENTS)
            else command
            for command in commands
        )
    return commands


def build_commands(
    clients: Iterable[str],
    *,
    action: str,
    hooks_enabled: bool,
    no_hooks_requested: bool = False,
) -> tuple[PlannedCommand, ...]:
    planned: list[PlannedCommand] = []
    for client_id in clients:
        spec = get_client(client_id)
        for command in _commands_for_client(
            spec,
            action=action,
            hooks_enabled=hooks_enabled,
            no_hooks_requested=no_hooks_requested,
        ):
            requires_project_root = (
                spec.supported_scopes == ("project",)
                or PROJECT_ROOT_FRAGMENT in command
            )
            planned.append(
                PlannedCommand(
                    client_id=client_id,
                    command=command,
                    requires_project_root=requires_project_root,
                )
            )
    return tuple(planned)


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parent.parent


def _git_value(aqg_root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(aqg_root),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    value = proc.stdout.strip()
    return value or "unknown"


def _collect_context(aqg_root_arg: str | None) -> RunContext:
    aqg_root = Path(aqg_root_arg or _repo_root_from_script())
    aqg_root = aqg_root.expanduser().resolve()
    return RunContext(
        aqg_root=str(aqg_root),
        git_remote=_git_value(aqg_root, "config", "--get", "remote.origin.url"),
        git_commit=_git_value(aqg_root, "rev-parse", "--verify", "HEAD"),
    )


def _hooks_line(hooks_enabled: bool) -> str:
    if hooks_enabled:
        return "supported lifecycle hooks install by default; integrated client installers use their defaults"
    return "lifecycle hooks skipped by --no-hooks where the selected adapter supports it"


def _no_hooks_line(no_hooks_requested: bool) -> str:
    if no_hooks_requested:
        return "requested; supported integrated installers receive --no-hooks"
    return "not requested"


def _client_status_line(clients: tuple[str, ...]) -> str:
    return ", ".join(
        f"{client_id}[{get_client(client_id).support_status}]"
        for client_id in clients
    )


def _capability_notice_lines(clients: tuple[str, ...]) -> tuple[str, ...]:
    notices: list[str] = []
    for client_id in clients:
        spec = get_client(client_id)
        if spec.support_status in ("supported", "full"):
            continue
        notices.append(
            f"- {client_id}: support_status={spec.support_status}; "
            "adapter is included by the selected mode, but capability coverage may be degraded."
        )
    return tuple(notices)


def _resolution_notice_lines(clients: tuple[str, ...], home: Path) -> tuple[str, ...]:
    lines: list[str] = []
    if "kimi-work" in clients:
        resolved = resolve_kimi_work_skills_root(
            home=home,
            include_fallback=True,
            allow_global_evidence=home.expanduser().resolve() == Path.home().expanduser().resolve(),
        )
        if resolved is not None:
            lines.extend(
                [
                    "- client_id=kimi-work",
                    f"  resolved_skills_root={resolved.skills_root}",
                    f"  discovery_source={resolved.discovery_source}",
                ]
            )
            if resolved.fallback_reason:
                lines.append(f"  fallback_reason={resolved.fallback_reason}")
    return tuple(lines)


def print_plan(
    *,
    context: RunContext,
    clients: tuple[str, ...],
    hooks_enabled: bool,
    no_hooks_requested: bool,
    action: str,
    commands: tuple[PlannedCommand, ...],
    project_root: str | None,
    home: Path,
    skipped_project_scope_clients: tuple[SkippedClient, ...] = (),
    detection: DetectionResult | None = None,
) -> None:
    mode = "dry-run" if action == "plan" else action
    print("AQG multi-client wrapper plan")
    print(f"- mode: {mode}")
    print(f"- AQG_ROOT: {context.aqg_root}")
    print(f"- git remote: {context.git_remote}")
    print(f"- git commit: {context.git_commit}")
    print(f"- clients: {_client_status_line(clients)}")
    if detection is not None and detection.evidence:
        print("- detection evidence:")
        for client_id in clients:
            found = detection.evidence.get(client_id)
            if found:
                print(f"  {client_id}: {found}")
    if _commands_require_project_root(commands) or skipped_project_scope_clients:
        print(f"- PROJECT_ROOT: {project_root or '<required for apply/verify/uninstall/is-installed>'}")
    if skipped_project_scope_clients:
        print("- skipped project-scope clients:")
        for skipped in skipped_project_scope_clients:
            print(f"  {skipped.client_id}: {skipped.reason}")
    print(f"- hooks: {_hooks_line(hooks_enabled)}")
    print(f"- no-hooks: {_no_hooks_line(no_hooks_requested)}")
    notices = _capability_notice_lines(clients)
    if notices:
        print("- capability notices:")
        for notice in notices:
            print(f"  {notice}")
    resolution_notices = _resolution_notice_lines(clients, home)
    if resolution_notices:
        print("- resolved targets:")
        for notice in resolution_notices:
            print(f"  {notice}")
    print("- commands:")
    if not commands:
        print("  (none)")
        return
    for index, item in enumerate(commands, start=1):
        print(f"  {index}. [{item.client_id}] {item.command}")


def _run_command(command: str, env: dict[str, str]) -> None:
    subprocess.run(command, shell=True, check=True, env=env)


def _group_commands_by_client(
    commands: tuple[PlannedCommand, ...],
) -> tuple[tuple[str, tuple[PlannedCommand, ...]], ...]:
    """Group planned commands by client id, preserving first-seen client order.

    A client is one atomic unit: its skills install and its hook install must
    either both run or the client is reported as failed.
    """
    grouped: dict[str, list[PlannedCommand]] = {}
    for item in commands:
        grouped.setdefault(item.client_id, []).append(item)
    return tuple((client_id, tuple(items)) for client_id, items in grouped.items())


def _execute_commands(
    commands: tuple[PlannedCommand, ...],
    env: dict[str, str],
) -> tuple[ClientRunResult, ...]:
    """Run every client group, isolating failures to the client that caused them."""
    results: list[ClientRunResult] = []
    for client_id, items in _group_commands_by_client(commands):
        result = ClientRunResult(client_id=client_id, ok=True)
        for index, item in enumerate(items):
            try:
                _run_command(item.command, env)
            except subprocess.CalledProcessError as exc:
                skipped = len(items) - index - 1
                result = ClientRunResult(
                    client_id=client_id,
                    ok=False,
                    failed_command=item.command,
                    returncode=exc.returncode,
                    skipped_commands=skipped,
                )
                print(
                    f"ERROR: [{client_id}] command failed with exit {exc.returncode}: "
                    f"{item.command}",
                    file=sys.stderr,
                )
                if skipped:
                    print(
                        f"ERROR: [{client_id}] skipping {skipped} remaining command(s) "
                        "for this client",
                        file=sys.stderr,
                    )
                print(
                    f"ERROR: [{client_id}] continuing with the remaining clients",
                    file=sys.stderr,
                )
                sys.stderr.flush()
                break
        results.append(result)
    return tuple(results)


def _print_execution_summary(results: tuple[ClientRunResult, ...]) -> None:
    if not results:
        return
    failures = tuple(result for result in results if not result.ok)
    print("- results:")
    for result in results:
        if result.ok:
            print(f"  [{result.client_id}] ok")
        else:
            print(
                f"  [{result.client_id}] FAILED (exit {result.returncode}): "
                f"{result.failed_command}"
            )
    if failures:
        failed_ids = ", ".join(result.client_id for result in failures)
        print(
            f"- client failures: {len(failures)}/{len(results)} ({failed_ids})",
            file=sys.stderr,
        )


def _exit_code_for_results(results: tuple[ClientRunResult, ...]) -> int:
    failures = [result for result in results if not result.ok]
    if not failures:
        return EXIT_OK
    if len(failures) == len(results):
        return EXIT_ALL_FAILED
    return EXIT_PARTIAL_FAILURE


def _execution_env(context: RunContext, project_root: str | None, home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["AQG_ROOT"] = context.aqg_root
    # Every child adapter defaults its own --home to Path.home(), which reads
    # HOME from the process environment. Overriding it here is what makes the
    # top-level --home actually constrain apply/verify/uninstall/is-installed
    # instead of only detection and display (2026-09-03 host-recovery incident).
    env["HOME"] = str(home.expanduser().resolve())
    if project_root:
        env["PROJECT_ROOT"] = project_root
    return env


def _print_detection_conflicts(detection: DetectionResult) -> None:
    for group in detection.conflicts:
        names = ", ".join(group)
        print(
            f"NOTE: ambiguous host identity for {names}: a desktop bundle and a CLI "
            "runtime were both found for the same settings surface, and they install "
            "different hook contracts. Neither was auto-selected; choose one "
            f"explicitly, e.g. --clients {group[0]}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    detection: DetectionResult | None = None
    try:
        args = parse_args(argv)
        if args.installed_supported:
            detection = detect_clients(args.home)
            _print_detection_conflicts(detection)
        clients = selected_clients(args)
    except NoInstalledClientsError as exc:
        print(f"NOTE: {exc}", file=sys.stderr)
        return EXIT_NO_CLIENTS
    except ClientSelectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    action = _action(args)
    hooks_enabled = _hooks_enabled(args)
    no_hooks_requested = _no_hooks_requested(args)
    try:
        _validate_hooks_request(
            clients,
            action=action,
            no_hooks_requested=no_hooks_requested,
        )
    except ClientSelectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    context = _collect_context(args.aqg_root)
    commands = build_commands(
        clients,
        action=action,
        hooks_enabled=hooks_enabled,
        no_hooks_requested=no_hooks_requested,
    )
    skip_project_scope = (
        _should_skip_project_scope_for_installed_supported(args, action=action)
        or (action == "plan" and _should_show_installed_supported_skip_notice(args))
    )
    commands, skipped_project_scope_clients = _filter_skipped_project_scope_commands(
        commands,
        skip_project_scope=skip_project_scope,
    )
    try:
        project_root = _resolve_project_root(
            args.project_root,
            commands=commands,
            action=action,
        )
    except ClientSelectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not commands:
        if not skipped_project_scope_clients:
            print(
                f"ERROR: no commands available for action {action} and clients: {', '.join(clients)}",
                file=sys.stderr,
            )
            return EXIT_USAGE
    print_plan(
        context=context,
        clients=clients,
        hooks_enabled=hooks_enabled,
        no_hooks_requested=no_hooks_requested,
        action=action,
        commands=commands,
        project_root=project_root,
        home=args.home.expanduser(),
        skipped_project_scope_clients=skipped_project_scope_clients,
        detection=detection,
    )
    sys.stdout.flush()

    if action == "plan":
        print("Dry-run only: no commands executed. Pass --apply to install.")
        return 0

    env = _execution_env(context, project_root, args.home)
    results = _execute_commands(commands, env)
    _print_execution_summary(results)
    return _exit_code_for_results(results)


if __name__ == "__main__":
    raise SystemExit(main())
