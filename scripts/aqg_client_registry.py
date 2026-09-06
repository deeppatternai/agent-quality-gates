#!/usr/bin/env python3
"""Supported-client registry and command contract for AQG adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple


ACTION_PLAN = "plan"
ACTION_APPLY = "apply"
ACTION_VERIFY = "verify"
ACTION_UNINSTALL = "uninstall"
ACTION_IS_INSTALLED = "is-installed"
ADAPTER_ACTIONS = (
    ACTION_PLAN,
    ACTION_APPLY,
    ACTION_VERIFY,
    ACTION_UNINSTALL,
    ACTION_IS_INSTALLED,
)

VALID_SUPPORT_STATUSES = ("supported", "full", "partial", "unsupported", "unknown")
VALID_SCOPES = ("user", "project")

#: How AQG hooks reach a client, as machine-readable data rather than the prose
#: in ``hooks_surface``. Descriptive only: this records what is true of each
#: host. What any consumer must DO about it belongs in that consumer, not here —
#: an obligation stated in one module's vocabulary block is not enforced by it.
#: (``extension`` currently has no consumer at all; ``pi`` has no adapter yet.)
#:
#: * ``managed-merge`` — AQG merges owned entries into a host-owned config file.
#: * ``extension`` — the host loads an AQG-provided extension instead.
#: * ``none`` — AQG has no verified lifecycle-hook delivery for this host. This
#:   is an evidence level, not a capability claim: for several of these the
#:   host's own prose says the surface is *unverified*, which is not the same as
#:   absent. Re-evaluating one is a registry edit, not a code change.
HOOK_DELIVERIES = ("managed-merge", "extension", "none")

#: Markers that a spec's ``hooks_surface`` prose is denying a surface rather
#: than describing one.
_NO_SURFACE_MARKERS = (
    "no hooks installed",
    "no official",
    "no managed hooks",
    "has no documented",
)


def hooks_surface_denies_a_surface(hooks_surface: Tuple[str, ...]) -> bool:
    """Whether this spec's prose says AQG has no hook surface on that host."""
    prose = " ".join(hooks_surface).lower()
    return any(marker in prose for marker in _NO_SURFACE_MARKERS)


CommandGroup = Tuple[str, ...]


@dataclass(frozen=True)
class ClientAdapterSpec:
    client_id: str
    support_status: str
    supported_scopes: Tuple[str, ...]
    skills_source: str
    skills_install_mode_default: str
    installer_command: CommandGroup
    verify_command: CommandGroup
    uninstall_command: CommandGroup
    is_installed_command: CommandGroup
    rules_surface: Tuple[str, ...]
    hooks_surface: Tuple[str, ...]
    hook_delivery: str
    supports_no_hooks: bool
    capability_evidence: Tuple[str, ...]
    adapter_actions: Dict[str, CommandGroup]


def _commands(*commands: str) -> CommandGroup:
    return tuple(commands)


def _actions(
    *,
    plan: CommandGroup = (),
    apply_commands: CommandGroup = (),
    verify: CommandGroup = (),
    uninstall: CommandGroup = (),
    is_installed: CommandGroup = (),
) -> Dict[str, CommandGroup]:
    return {
        ACTION_PLAN: plan,
        ACTION_APPLY: apply_commands,
        ACTION_VERIFY: verify,
        ACTION_UNINSTALL: uninstall,
        ACTION_IS_INSTALLED: is_installed,
    }


def _spec(
    *,
    client_id: str,
    support_status: str,
    supported_scopes: Tuple[str, ...],
    skills_source: str,
    skills_install_mode_default: str,
    installer_command: CommandGroup,
    verify_command: CommandGroup,
    uninstall_command: CommandGroup,
    rules_surface: Tuple[str, ...],
    hooks_surface: Tuple[str, ...],
    hook_delivery: str,
    supports_no_hooks: bool,
    capability_evidence: Tuple[str, ...],
    is_installed_command: CommandGroup,
) -> ClientAdapterSpec:
    return ClientAdapterSpec(
        client_id=client_id,
        support_status=support_status,
        supported_scopes=supported_scopes,
        skills_source=skills_source,
        skills_install_mode_default=skills_install_mode_default,
        installer_command=installer_command,
        verify_command=verify_command,
        uninstall_command=uninstall_command,
        is_installed_command=is_installed_command,
        rules_surface=rules_surface,
        hooks_surface=hooks_surface,
        hook_delivery=hook_delivery,
        supports_no_hooks=supports_no_hooks,
        capability_evidence=capability_evidence,
        adapter_actions=_actions(
            plan=installer_command,
            apply_commands=installer_command,
            verify=verify_command,
            uninstall=uninstall_command,
            is_installed=is_installed_command,
        ),
    )


def _qoder_commands(client_id: str, action: str, *, scope: str = "user") -> CommandGroup:
    mode = " --mode link" if action in (ACTION_APPLY, ACTION_VERIFY) else ""
    base = (
        'python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" '
        f'--client {client_id} --scope {scope} --aqg-root "$AQG_ROOT"{mode} --{action}'
    )
    if scope == "project":
        base = (
            'python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" '
            f'--client {client_id} --scope project --project-root "$PROJECT_ROOT" '
            f'--aqg-root "$AQG_ROOT"{mode} --{action}'
        )
    return _commands(base)


def _qoder_desktop_commands(client_id: str, action: str) -> CommandGroup:
    """User-scope command first, then the separate project-rules command.

    Qoder Desktop's user surface (16 skills plus the partial hook set) does not
    need a project root. Project rules do, so they are their own command: a
    missing PROJECT_ROOT drops that one command, never the whole Desktop install.
    """
    return _qoder_commands(client_id, action) + _qoder_commands(
        client_id, action, scope="project"
    )


def _work_client_commands(client_id: str, action: str, *, scope: str = "user") -> CommandGroup:
    mode = " --mode link" if action in (ACTION_APPLY, ACTION_VERIFY) else ""
    base = (
        'python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" '
        f'--client {client_id} --scope {scope} --aqg-root "$AQG_ROOT"{mode} --{action}'
    )
    if scope == "project":
        base = (
            'python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" '
            f'--client {client_id} --scope project --project-root "$PROJECT_ROOT" '
            f'--aqg-root "$AQG_ROOT"{mode} --{action}'
        )
    return _commands(base)


def _agent_client_commands(client_id: str, action: str, *, scope: str = "user") -> CommandGroup:
    mode = " --mode link" if action in (ACTION_APPLY, ACTION_VERIFY) else ""
    base = (
        'python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" '
        f'--client {client_id} --scope {scope} --aqg-root "$AQG_ROOT"{mode} --{action}'
    )
    if scope == "project":
        base = (
            'python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" '
            f'--client {client_id} --scope project --project-root "$PROJECT_ROOT" '
            f'--aqg-root "$AQG_ROOT"{mode} --{action}'
        )
    return _commands(base)


def _pi_commands(action: str, *, scope: str = "user") -> CommandGroup:
    mode = " --mode link" if action in (ACTION_APPLY, ACTION_VERIFY) else ""
    base = f'python3 "$AQG_ROOT/scripts/install_aqg_pi.py" --scope {scope} --aqg-root "$AQG_ROOT"{mode} --{action}'
    if scope == "project":
        base = (
            'python3 "$AQG_ROOT/scripts/install_aqg_pi.py" '
            f'--scope project --project-root "$PROJECT_ROOT" --aqg-root "$AQG_ROOT"{mode} --{action}'
        )
    return _commands(base)


def _codex_skill_check() -> CommandGroup:
    return _commands(
        "bash -lc 'for skill in \"$AQG_ROOT\"/skills/aqg-*; do "
        'test -e "${CODEX_HOME:-$HOME/.codex}/skills/${skill##*/}" || exit 1; '
        "done'"
    )


def _claude_skill_check() -> CommandGroup:
    return _commands(
        "bash -lc 'for skill in \"$AQG_ROOT\"/agent-packs/claude-code/skills/aqg-*; do "
        'test -e "$HOME/.claude/skills/${skill##*/}" || exit 1; '
        "done'"
    )


_CLIENTS = (
    _spec(
        client_id="codex",
        support_status="supported",
        supported_scopes=("user",),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_commands(
            '"$AQG_ROOT/scripts/install.sh" --force --no-hooks',
            'python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$AQG_ROOT"',
            'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex --apply --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not installed for codex; skills and hooks are on disk but Gate A will not fire until this is fixed - see the error above" >&2',
        ),
        verify_command=_commands(
            *_codex_skill_check(),
            'python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --verify --aqg-root "$AQG_ROOT"',
            'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex --verify --aqg-root "$AQG_ROOT"',
        ),
        uninstall_command=_commands(
            'python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --uninstall --aqg-root "$AQG_ROOT"',
            'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex --uninstall --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not removed for codex; remove it by hand - see the error above" >&2',
        ),
        rules_surface=("AGENTS.md rule block",),
        hooks_surface=("~/.codex/hooks.json via scripts/install_aqg_codex_hooks.py",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "AI_SETUP.md supported-client table: codex",
            "README.md supported coding agents matrix: Codex supported",
            "scripts/install.sh",
            "scripts/aqg_skill_install.py",
            "scripts/install_aqg_codex_hooks.py",
            "examples/aqg-codex-agents.example.md",
        ),
        is_installed_command=_commands(
            *_codex_skill_check(),
            'python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --is-installed --aqg-root "$AQG_ROOT"',
            'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex --is-installed --aqg-root "$AQG_ROOT"',
        ),
    ),
    _spec(
        client_id="claude-code",
        support_status="supported",
        supported_scopes=("user", "project"),
        skills_source="agent-packs/claude-code/skills/",
        skills_install_mode_default="link",
        installer_command=_commands(
            '"$AQG_ROOT/agent-packs/claude-code/install.sh" --scope user --mode link --force --no-hooks',
            'python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --apply --aqg-root "$AQG_ROOT"',
            'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client claude-code --apply --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not installed for claude-code; skills and hooks are on disk but Gate A will not fire until this is fixed - see the error above" >&2',
        ),
        verify_command=_commands(
            *_claude_skill_check(),
            'python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --verify --aqg-root "$AQG_ROOT"',
            'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client claude-code --verify --aqg-root "$AQG_ROOT"',
        ),
        uninstall_command=_commands(
            'python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --uninstall --aqg-root "$AQG_ROOT"',
            'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client claude-code --uninstall --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not removed for claude-code; remove it by hand - see the error above" >&2',
        ),
        rules_surface=("CLAUDE.md rule block",),
        hooks_surface=("~/.claude/settings.json via scripts/install_aqg_hooks.py",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "AI_SETUP.md supported-client table: claude-code",
            "README.md supported coding agents matrix: Claude Code supported",
            "agent-packs/claude-code/install.sh",
            "agent-packs/claude-code/hooks/",
            "examples/aqg-claude-rules.example.md",
        ),
        is_installed_command=_commands(
            *_claude_skill_check(),
            'python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --is-installed --aqg-root "$AQG_ROOT"',
            'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client claude-code --is-installed --aqg-root "$AQG_ROOT"',
        ),
    ),
    _spec(
        client_id="cursor",
        support_status="supported",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_commands(
            'python3 "$AQG_ROOT/scripts/install_cursor_support.py" --apply --scope user --mode link'
        ),
        verify_command=_commands(
            'python3 "$AQG_ROOT/scripts/install_cursor_support.py" --verify --scope user --mode link'
        ),
        uninstall_command=_commands(
            'python3 "$AQG_ROOT/scripts/install_cursor_support.py" --uninstall --scope user'
        ),
        rules_surface=(".cursor/rules/aqg.mdc for project scope; user rules are UI-managed",),
        hooks_surface=(".cursor/hooks.json via scripts/install_cursor_support.py",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "AI_SETUP.md supported-client table: cursor",
            "README.md supported coding agents matrix: Cursor supported",
            "scripts/install_cursor_support.py",
            "scripts/cursor_aqg_hook.py",
        ),
        is_installed_command=_commands(
            'python3 "$AQG_ROOT/scripts/install_cursor_support.py" --is-installed --scope user'
        ),
    ),
    _spec(
        client_id="workbuddy",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_work_client_commands("workbuddy", "apply"),
        verify_command=_work_client_commands("workbuddy", "verify"),
        uninstall_command=_work_client_commands("workbuddy", "uninstall"),
        rules_surface=("WorkBuddy AQG report/rules bundle under .workbuddy; auto-load behavior unknown",),
        hooks_surface=("No official WorkBuddy lifecycle hook contract found; no hooks installed",),
        hook_delivery="none",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/client-support-matrix.zh-CN.md WorkBuddy row",
            "WorkBuddy official docs: https://www.workbuddy.ai/docs",
            "WorkBuddy official docs: https://www.workbuddy.ai/agents",
            "scripts/install_aqg_work_clients.py PROFILES workbuddy support_level=partial",
        ),
        is_installed_command=_work_client_commands("workbuddy", "is-installed"),
    ),
    _spec(
        client_id="codebuddy",
        support_status="full",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_work_client_commands("codebuddy", "apply"),
        verify_command=_work_client_commands("codebuddy", "verify"),
        uninstall_command=_work_client_commands("codebuddy", "uninstall"),
        rules_surface=("CodeBuddy rules/aqg.md plus AQG support report",),
        hooks_surface=("CodeBuddy settings.json lifecycle hooks for SessionStart/PreToolUse/PostToolUse/PostToolUseFailure/PreCompact/Stop/UserPromptSubmit",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/client-support-matrix.zh-CN.md CodeBuddy row",
            "CodeBuddy official docs: https://www.codebuddy.ai/docs/cli/hooks",
            "CodeBuddy official docs: https://www.codebuddy.ai/docs/cli/skills",
            "CodeBuddy official docs: https://www.codebuddy.ai/docs/cli/mcp",
            "scripts/install_aqg_work_clients.py PROFILES codebuddy support_level=full",
        ),
        is_installed_command=_work_client_commands("codebuddy", "is-installed"),
    ),
    _spec(
        client_id="workbuddy-ai",
        support_status="full",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_work_client_commands("workbuddy-ai", "apply"),
        verify_command=_work_client_commands("workbuddy-ai", "verify"),
        uninstall_command=_work_client_commands("workbuddy-ai", "uninstall"),
        rules_surface=("WorkBuddy AI rules/aqg.md plus AQG support report under ~/.workbuddy-ai",),
        hooks_surface=("WorkBuddy AI settings.json lifecycle hooks for SessionStart/PreToolUse/PostToolUse/PostToolUseFailure/PreCompact/Stop/UserPromptSubmit",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "WorkPacket AQG-026: WorkBuddy AI product.json customUserDataDir=.workbuddy-ai",
            "WorkPacket AQG-026: WorkBuddy AI main process sets WORKBUDDY_CONFIG_DIR/CODEBUDDY_CONFIG_DIR to ~/.workbuddy-ai",
            "macOS product identity: WorkBuddy AI.app, bundle id com.workbuddy.workbuddy-ai",
            "scripts/install_aqg_work_clients.py PROFILES workbuddy-ai support_level=full, user_dir=.workbuddy-ai",
        ),
        is_installed_command=_work_client_commands("workbuddy-ai", "is-installed"),
    ),
    _spec(
        client_id="trae-work",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_agent_client_commands("trae-work", "apply"),
        verify_command=_agent_client_commands("trae-work", "verify"),
        uninstall_command=_agent_client_commands("trae-work", "uninstall"),
        rules_surface=("TRAE Work project AGENTS.md; user rules are UI-managed",),
        hooks_surface=("no managed hooks: Work lifecycle-hook schema not verified",),
        hook_delivery="none",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md: Trae Work Skills/Rules/MCP evidence; hooks unknown",
            "macOS product identity: TRAE SOLO.app, bundle id com.trae.solo.app",
            "TRAE official docs: https://docs.trae.ai/ide/skills?_lang=en",
            "TRAE official docs: https://docs.trae.ai/ide/mcp?_lang=en",
            "scripts/install_aqg_agent_clients.py PROFILES trae-work level=partial shares ~/.trae/skills",
        ),
        is_installed_command=_agent_client_commands("trae-work", "is-installed"),
    ),
    _spec(
        client_id="kimi-work",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_work_client_commands("kimi-work", "apply"),
        verify_command=_work_client_commands("kimi-work", "verify"),
        uninstall_command=_work_client_commands("kimi-work", "uninstall"),
        rules_surface=("No managed Kimi Work rules path; AQG writes an advisory support report alongside skills",),
        hooks_surface=("No official Kimi Work lifecycle hook or blocking gate contract found",),
        hook_delivery="none",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/client-support-matrix.zh-CN.md Kimi Work row",
            "Kimi official product site: https://kimi.moonshot.cn/",
            "Local Daimon evidence: Kimi Work Desktop reads skills from daimon-share/daimon/skills",
            "scripts/install_aqg_work_clients.py PROFILES kimi-work support_level=partial",
        ),
        is_installed_command=_work_client_commands("kimi-work", "is-installed"),
    ),
    _spec(
        client_id="kimi-code",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_work_client_commands("kimi-code", "apply"),
        verify_command=_work_client_commands("kimi-code", "verify"),
        uninstall_command=_work_client_commands("kimi-code", "uninstall"),
        rules_surface=("Kimi Code skills/rules/MCP bundle under .kimi-code",),
        hooks_surface=("Kimi Code config.toml hooks; official docs describe fail-open hook failures",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/client-support-matrix.zh-CN.md Kimi Code row",
            "Kimi Code official docs: https://moonshotai.github.io/kimi-code/en/customization/hooks",
            "Kimi Code official docs: https://moonshotai.github.io/kimi-code/en/customization/skills.html",
            "Kimi Code official docs: https://moonshotai.github.io/kimi-code/en/customization/mcp.html",
            "scripts/install_aqg_work_clients.py PROFILES kimi-code support_level=partial",
        ),
        is_installed_command=_work_client_commands("kimi-code", "is-installed"),
    ),
    _spec(
        client_id="qoder-cli",
        support_status="full",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_qoder_commands("qoder-cli", "apply"),
        verify_command=_qoder_commands("qoder-cli", "verify"),
        uninstall_command=_qoder_commands("qoder-cli", "uninstall"),
        rules_surface=("Qoder CLI user/project rules/aqg.md",),
        hooks_surface=("Qoder settings.json with CLI lifecycle hooks, SessionStart, PreCompact, WIP save/recover",),
        hook_delivery="managed-merge",
        supports_no_hooks=False,
        capability_evidence=(
            "AI_SETUP.md supported-client table: qoder-cli / qoder-cli-cn",
            "README.md supported coding agents matrix: Qoder CLI full",
            "scripts/install_aqg_qoder.py PROFILES qoder-cli level=full",
            "agent-packs/qoder/hooks/",
        ),
        is_installed_command=_qoder_commands("qoder-cli", "is-installed"),
    ),
    _spec(
        client_id="qoder-cli-cn",
        support_status="full",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_qoder_commands("qoder-cli-cn", "apply"),
        verify_command=_qoder_commands("qoder-cli-cn", "verify"),
        uninstall_command=_qoder_commands("qoder-cli-cn", "uninstall"),
        rules_surface=("Qoder CLI CN user/project rules/aqg.md",),
        hooks_surface=("Qoder CLI CN settings.json with SessionStart, PreCompact, WIP save/recover",),
        hook_delivery="managed-merge",
        supports_no_hooks=False,
        capability_evidence=(
            "AI_SETUP.md supported-client table: qoder-cli / qoder-cli-cn",
            "README.md supported coding agents matrix: Qoder CLI CN full",
            "scripts/install_aqg_qoder.py PROFILES qoder-cli-cn level=full",
            "agent-packs/qoder/hooks/",
        ),
        is_installed_command=_qoder_commands("qoder-cli-cn", "is-installed"),
    ),
    _spec(
        client_id="qoder",
        support_status="partial",
        # Desktop user scope installs the 16 skills and the partial hook set.
        # Project rules are a separate project-scope command, so a missing
        # PROJECT_ROOT must not drop the whole Desktop install.
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_qoder_desktop_commands("qoder", "apply"),
        verify_command=_qoder_desktop_commands("qoder", "verify"),
        uninstall_command=_qoder_desktop_commands("qoder", "uninstall"),
        rules_surface=("Qoder IDE project rules/aqg.md only; user scope installs skills and hooks",),
        hooks_surface=("Qoder settings.json without SessionStart, PreCompact, or WIP save/recover",),
        hook_delivery="managed-merge",
        supports_no_hooks=False,
        capability_evidence=(
            "AI_SETUP.md known non-target status: qoder partial",
            "README.md supported coding agents matrix: Qoder IDE partial",
            "macOS product identities: Qoder.app / com.qoder.app; Qoder IDE.app / com.qoder.ide",
            "scripts/install_aqg_qoder.py PROFILES qoder level=partial user_dir=.qoder",
        ),
        is_installed_command=_qoder_desktop_commands("qoder", "is-installed"),
    ),
    _spec(
        client_id="qoder-cn",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_qoder_desktop_commands("qoder-cn", "apply"),
        verify_command=_qoder_desktop_commands("qoder-cn", "verify"),
        uninstall_command=_qoder_desktop_commands("qoder-cn", "uninstall"),
        rules_surface=("Qoder CN IDE project rules/aqg.md only; user scope installs skills and hooks",),
        hooks_surface=("Qoder CN settings.json without SessionStart, PreCompact, or WIP save/recover",),
        hook_delivery="managed-merge",
        supports_no_hooks=False,
        capability_evidence=(
            "AI_SETUP.md known non-target status: qoder-cn partial",
            "README.md supported coding agents matrix: Qoder IDE / Tongyi Lingma partial",
            "macOS product identities: Qoder CN.app / com.qodercn.app; Qoder CN IDE.app / com.aliyun.lingma.ide",
            "scripts/install_aqg_qoder.py PROFILES qoder-cn level=partial user_dir=.qoder-cn",
        ),
        is_installed_command=_qoder_desktop_commands("qoder-cn", "is-installed"),
    ),
    _spec(
        client_id="trae",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_agent_client_commands("trae", "apply"),
        verify_command=_agent_client_commands("trae", "verify"),
        uninstall_command=_agent_client_commands("trae", "uninstall"),
        rules_surface=("TRAE project AGENTS.md; user rules are UI-managed",),
        hooks_surface=("TRAE hooks.json without PreCompact",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md: Trae IDE official Skills/Rules/MCP/Hooks evidence",
            "scripts/install_aqg_agent_clients.py PROFILES trae level=partial",
            "scripts/agent_client_aqg_hook.py",
        ),
        is_installed_command=_agent_client_commands("trae", "is-installed"),
    ),
    _spec(
        client_id="trae-cn",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_agent_client_commands("trae-cn", "apply"),
        verify_command=_agent_client_commands("trae-cn", "verify"),
        uninstall_command=_agent_client_commands("trae-cn", "uninstall"),
        rules_surface=("TRAE CN project AGENTS.md; user rules are UI-managed",),
        hooks_surface=("TRAE CN hooks.json without PreCompact",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md: Trae CN official Skills/Rules/MCP/Hooks evidence",
            "scripts/install_aqg_agent_clients.py PROFILES trae-cn level=partial",
            "scripts/agent_client_aqg_hook.py",
        ),
        is_installed_command=_agent_client_commands("trae-cn", "is-installed"),
    ),
    _spec(
        client_id="trae-work-cn",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_agent_client_commands("trae-work-cn", "apply"),
        verify_command=_agent_client_commands("trae-work-cn", "verify"),
        uninstall_command=_agent_client_commands("trae-work-cn", "uninstall"),
        rules_surface=("TRAE Work CN project AGENTS.md; user rules are UI-managed",),
        hooks_surface=("no managed hooks: Work lifecycle-hook schema not verified",),
        hook_delivery="none",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md: Trae Work CN Skills/Rules/MCP evidence; hooks unknown",
            "scripts/install_aqg_agent_clients.py PROFILES trae-work-cn level=partial",
        ),
        is_installed_command=_agent_client_commands("trae-work-cn", "is-installed"),
    ),
    _spec(
        client_id="zed",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_agent_client_commands("zed", "apply"),
        verify_command=_agent_client_commands("zed", "verify"),
        uninstall_command=_agent_client_commands("zed", "uninstall"),
        rules_surface=("Zed AGENTS.md instructions",),
        hooks_surface=("no managed hooks: official lifecycle-hook surface not verified",),
        hook_delivery="none",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md: Zed Skills/Instructions/MCP evidence; hooks unavailable",
            "scripts/install_aqg_agent_clients.py PROFILES zed level=partial",
        ),
        is_installed_command=_agent_client_commands("zed", "is-installed"),
    ),
    _spec(
        client_id="devin",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_agent_client_commands("devin", "apply"),
        verify_command=_agent_client_commands("devin", "verify"),
        uninstall_command=_agent_client_commands("devin", "uninstall"),
        rules_surface=("Devin AGENTS.md rules",),
        hooks_surface=("Devin CLI hooks without PreCompact-before-compaction",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md: Devin Rules/Skills/MCP/Hooks evidence",
            "scripts/install_aqg_agent_clients.py PROFILES devin level=partial",
            "scripts/agent_client_aqg_hook.py",
        ),
        is_installed_command=_agent_client_commands("devin", "is-installed"),
    ),
    _spec(
        client_id="qoderwork",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_work_client_commands("qoderwork", "apply"),
        verify_command=_work_client_commands("qoderwork", "verify"),
        uninstall_command=_work_client_commands("qoderwork", "uninstall"),
        rules_surface=("QoderWork AQG skills/rules/MCP support bundle under .qoderwork",),
        hooks_surface=("QoderWork settings.json hooks for PreToolUse/PostToolUse/Stop/UserPromptSubmit; no SessionStart/PreCompact/WIP save-recover parity",),
        hook_delivery="managed-merge",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/client-support-matrix.zh-CN.md QoderWork row",
            "QoderWork official docs: https://docs.qoder.com/qoderwork/introduction",
            "QoderWork official docs: https://docs.qoder.com/qoderwork/connectors",
            "QoderWork official docs: https://docs.qoder.com/qoderwork/hooks",
            "scripts/install_aqg_work_clients.py PROFILES qoderwork support_level=partial",
        ),
        is_installed_command=_work_client_commands("qoderwork", "is-installed"),
    ),
    _spec(
        client_id="qoderwake",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_work_client_commands("qoderwake", "apply"),
        verify_command=_work_client_commands("qoderwake", "verify"),
        uninstall_command=_work_client_commands("qoderwake", "uninstall"),
        rules_surface=("QoderWake AQG skills/rules/MCP support bundle under .qoderwake",),
        hooks_surface=("QoderWake Waker automation has no documented local blocking lifecycle hook contract",),
        hook_delivery="none",
        supports_no_hooks=True,
        capability_evidence=(
            "docs/client-support-matrix.zh-CN.md QoderWake row",
            "QoderWake official docs: https://docs.qoder.com/qoderwake/overview",
            "QoderWake official docs: https://docs.qoder.com/qoderwake/skills-and-integrations",
            "QoderWake official docs: https://docs.qoder.com/qoderwake/manage-wakers",
            "scripts/install_aqg_work_clients.py PROFILES qoderwake support_level=partial",
        ),
        is_installed_command=_work_client_commands("qoderwake", "is-installed"),
    ),
    _spec(
        client_id="pi",
        support_status="partial",
        supported_scopes=("user", "project"),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=_pi_commands("apply"),
        verify_command=_pi_commands("verify"),
        uninstall_command=_pi_commands("uninstall"),
        rules_surface=("Pi SKILL.md skills plus AQG support report; no separate managed rules file",),
        hooks_surface=("Pi TypeScript extension for session_start/tool_call/tool_result/input/session_before_compact/session_shutdown",),
        hook_delivery="extension",
        supports_no_hooks=True,
        capability_evidence=(
            "Pi official docs: https://pi.dev/docs/latest/extensions#events",
            "Pi official docs: https://pi.dev/docs/latest/skills",
            "Pi official docs: https://pi.dev/docs/latest",
            "scripts/install_aqg_pi.py support_level=partial",
        ),
        is_installed_command=_pi_commands("is-installed"),
    ),
)

CLIENT_REGISTRY = {entry.client_id: entry for entry in _CLIENTS}


def list_clients(support_status: Optional[str] = None) -> Tuple[ClientAdapterSpec, ...]:
    if support_status is None:
        return tuple(CLIENT_REGISTRY.values())
    return tuple(
        entry for entry in CLIENT_REGISTRY.values() if entry.support_status == support_status
    )


def get_client(client_id: str) -> ClientAdapterSpec:
    try:
        return CLIENT_REGISTRY[client_id]
    except KeyError as exc:
        raise KeyError(f"unknown AQG client_id: {client_id}") from exc


def validate_registry(
    registry: Optional[Iterable[ClientAdapterSpec]] = None,
) -> None:
    entries = tuple(CLIENT_REGISTRY.values() if registry is None else registry)
    seen = set()
    for entry in entries:
        if not isinstance(entry, ClientAdapterSpec):
            raise ValueError("registry entries must be ClientAdapterSpec instances")
        if not entry.client_id:
            raise ValueError("client_id must be non-empty")
        if entry.client_id in seen:
            raise ValueError(f"duplicate client_id: {entry.client_id}")
        seen.add(entry.client_id)
        if entry.support_status not in VALID_SUPPORT_STATUSES:
            raise ValueError(f"{entry.client_id}: invalid support_status")
        if not entry.supported_scopes:
            raise ValueError(f"{entry.client_id}: supported_scopes must be non-empty")
        invalid_scopes = set(entry.supported_scopes) - set(VALID_SCOPES)
        if invalid_scopes:
            raise ValueError(f"{entry.client_id}: invalid supported_scopes")
        for field_name in (
            "skills_source",
            "skills_install_mode_default",
            "rules_surface",
            "hooks_surface",
            "capability_evidence",
        ):
            if not getattr(entry, field_name):
                raise ValueError(f"{entry.client_id}: {field_name} must be non-empty")
        if entry.hook_delivery not in HOOK_DELIVERIES:
            raise ValueError(
                f"{entry.client_id}: invalid hook_delivery {entry.hook_delivery!r}; "
                f"expected one of {list(HOOK_DELIVERIES)}"
            )
        # A transcription check, not a correctness oracle: the delivery values
        # were derived by hand from the prose on this same spec, so a mistyped
        # one is the realistic failure. It cannot catch prose that is itself
        # wrong, and no stronger signal exists in the registry -- most
        # managed-merge hosts install hooks through a generic script whose
        # command string never mentions them.
        if hooks_surface_denies_a_surface(entry.hooks_surface) != (
            entry.hook_delivery == "none"
        ):
            raise ValueError(
                f"{entry.client_id}: hook_delivery={entry.hook_delivery!r} "
                f"contradicts its own hooks_surface prose"
            )
        if not isinstance(entry.supports_no_hooks, bool):
            raise ValueError(f"{entry.client_id}: supports_no_hooks must be boolean")
        if set(entry.adapter_actions) != set(ADAPTER_ACTIONS):
            raise ValueError(f"{entry.client_id}: adapter action contract drift")
        if entry.adapter_actions[ACTION_APPLY] != entry.installer_command:
            raise ValueError(f"{entry.client_id}: apply command drift")
        if entry.adapter_actions[ACTION_VERIFY] != entry.verify_command:
            raise ValueError(f"{entry.client_id}: verify command drift")
        if entry.adapter_actions[ACTION_UNINSTALL] != entry.uninstall_command:
            raise ValueError(f"{entry.client_id}: uninstall command drift")
        if entry.adapter_actions[ACTION_IS_INSTALLED] != entry.is_installed_command:
            raise ValueError(f"{entry.client_id}: is-installed command drift")
        for action_name, commands in entry.adapter_actions.items():
            if not isinstance(commands, tuple):
                raise ValueError(f"{entry.client_id}: {action_name} commands must be a tuple")
            if any(not isinstance(command, str) or not command.strip() for command in commands):
                raise ValueError(f"{entry.client_id}: empty command in {action_name}")
        for field_name in (
            "installer_command",
            "verify_command",
            "uninstall_command",
            "is_installed_command",
        ):
            if not getattr(entry, field_name):
                raise ValueError(f"{entry.client_id}: {field_name} required")


validate_registry()
