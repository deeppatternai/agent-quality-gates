"""The managed update check has to reach every host that can start a session.

`agent-packs/claude-code/hooks/sessionstart_update_check.sh` is the trigger that
starts the managed update in the background. It shipped wired into exactly one
installer — Claude Code — and the other twelve hook-capable hosts got nothing,
silently: each installer carries its own hook table, and none of them is
compared against the others for THIS script.

That is the same failure shape `test_installer_specs_agree.py` was written for,
one layer out: not "the two installers disagree" but "eleven installers were
never asked". So the invariant here is coverage, expressed as an OUTSIDE spec —
`EXPECTED_UPDATE_TRIGGER` is written by hand, deliberately not derived from the
installers, so that adding a host or a lifecycle event fails here until someone
decides what the new host should do.

A host that CANNOT take the trigger is recorded with its reason rather than
omitted. "No entry" is the one thing this file does not allow, because a silent
omission is exactly how twelve hosts lost the update check in the first place.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
TRIGGER = "sessionstart_update_check.sh"


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _registry():
    return _load("aqg_registry_for_update_coverage", "scripts/aqg_client_registry.py")


# The outside spec. `None` means "the trigger must be mounted"; a string is the
# reason this host gets none, and is asserted to be a real sentence rather than a
# placeholder. Hosts with `hook_delivery="none"` are absent on purpose: they have
# no hook surface at all, so there is nothing here to decide.
EXPECTED_UPDATE_TRIGGER: dict[str, str | None] = {
    "claude-code": None,
    "codex": None,
    "cursor": None,
    "codebuddy": None,
    "workbuddy-ai": None,
    "kimi-code": None,
    "qoder-cli": None,
    "qoder-cli-cn": None,
    "trae": None,
    "trae-cn": None,
    "devin": None,
    "pi": None,
    "qoder": (
        "Qoder Desktop (cli=False) has no verified SessionStart surface, so the "
        "installer mounts no session-start event for it to hang on"
    ),
    "qoder-cn": (
        "Qoder CN Desktop (cli=False) has no verified SessionStart surface, so the "
        "installer mounts no session-start event for it to hang on"
    ),
    "qoderwork": (
        "QoderWork SessionStart parity with Qoder CLI is unproven, so the installer "
        "restricts it to PreToolUse/PostToolUse/Stop/UserPromptSubmit"
    ),
}


def _scripts_on_event(spec: dict, event: str) -> set[str]:
    """Hook-script basenames mounted on `event`, whatever the command shape.

    Installers store `command` as a shell string (Claude, Qoder), as a list
    (Codex), or as a dict entry; scanning tokens rather than shapes keeps this
    one scanner working across all of them.
    """
    found: set[str] = set()
    for entry in spec.get(event, []):
        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            for token in str(command).split():
                token = token.strip("\"'")
                if token.endswith(".sh"):
                    found.add(token.rsplit("/", 1)[-1])
    return found


def _cursor_family_adapter():
    return _load("aqg_cursor_adapter_for_update_coverage", "scripts/cursor_aqg_hook.py")


ADAPTER = "cursor_aqg_hook.py"


def _sessionstart_spawns(module) -> list[str]:
    """Run the adapter's sessionStart with the process spawn captured.

    `Popen`, not `run`: the trigger is fire-and-forget, so a test that watched
    `run` would go green again the moment someone reintroduced a blocking wait.
    """
    spawned: list[str] = []

    def fake_popen(command, *args, **kwargs):
        spawned.append(" ".join(str(part) for part in command))
        return mock.MagicMock()

    with mock.patch.object(module.subprocess, "Popen", fake_popen):
        module._session_start({}, REPO, None)
    return spawned


def _cursor_family_adapter_launches_trigger() -> bool:
    """Does the shared Cursor-family adapter start the trigger on sessionStart?

    Cursor, CodeBuddy, WorkBuddy AI and Kimi Code all mount ONE command per
    lifecycle event — `cursor_aqg_hook.py sessionStart` — instead of naming hook
    scripts in their config. For them the trigger cannot live in the installer's
    table; it lives in the adapter, so that is where it has to be observed.
    """
    return any(TRIGGER in call for call in _sessionstart_spawns(_cursor_family_adapter()))


def _command_of(entry: object) -> str:
    """The command string out of a host hook entry, whatever it is wrapped in."""
    if isinstance(entry, dict):
        command = entry.get("command", "")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        if command:
            return str(command)
        return " ".join(_command_of(item) for item in entry.get("hooks", []) or [])
    if isinstance(entry, list):
        return " ".join(_command_of(item) for item in entry)
    return ""


def _mounts_update_trigger(client_id: str) -> bool:
    """Whether an install of `client_id` ends up running the trigger at session start."""
    if client_id == "claude-code":
        installer = _load("aqg_claude_installer_uc", "scripts/install_aqg_hooks.py")
        return TRIGGER in _scripts_on_event(installer._aqg_hook_specs(REPO), "SessionStart")

    if client_id == "codex":
        installer = _load("aqg_codex_installer_uc", "scripts/install_aqg_codex_hooks.py")
        spec = installer._aqg_hook_specs(REPO, python_executable=Path(sys.executable))
        return TRIGGER in _scripts_on_event(spec, "SessionStart")

    if client_id in {"qoder", "qoder-cn", "qoder-cli", "qoder-cli-cn"}:
        installer = _load("aqg_qoder_installer_uc", "scripts/install_aqg_qoder.py")
        cli = bool(installer.PROFILES[client_id]["cli"])
        spec = installer._hook_specs(REPO, cli)
        return TRIGGER in _scripts_on_event(spec, "SessionStart")

    if client_id in {"trae", "trae-cn", "devin"}:
        installer = _load("aqg_agent_installer_uc", "scripts/install_aqg_agent_clients.py")
        spec = installer._hook_specs(REPO, installer.PROFILES[client_id])
        return TRIGGER in _scripts_on_event(spec, "SessionStart")

    if client_id == "pi":
        installer = _load("aqg_pi_installer_uc", "scripts/install_aqg_pi.py")
        # Pi's hook table is a rendered TypeScript extension, so "mounted on the
        # session-start event" means "inside that handler" — bounded by the next
        # `pi.on(`, because `});` also closes every runAqg call inside it.
        rendered = installer._render_extension(REPO).split('pi.on("session_start"', 1)
        if len(rendered) != 2:
            return False
        return TRIGGER in rendered[1].split("pi.on(", 1)[0]

    # The Cursor family needs BOTH halves checked, and checking only the second
    # is the trap: the adapter can start the trigger perfectly while the host's
    # configured session-start command points somewhere else entirely. So the
    # generated command has to be shown to reach THIS adapter.
    if client_id == "cursor":
        installer = _load("aqg_cursor_installer_uc", "scripts/install_cursor_support.py")
        spec = installer._hook_specs().get("sessionStart")
        if spec is None or ADAPTER not in _command_of(spec):
            return False
        return _cursor_family_adapter_launches_trigger()

    if client_id in {"codebuddy", "workbuddy-ai", "kimi-code", "qoderwork"}:
        installer = _load("aqg_work_installer_uc", "scripts/install_aqg_work_clients.py")
        profile = installer.PROFILES[client_id]
        if profile.hooks_format == "toml":
            # The TOML renderer emits one block per event out of HOOK_EVENTS,
            # each carrying the same adapter command the JSON hosts get.
            if "SessionStart" not in installer.HOOK_EVENTS:
                return False
            command = installer._quoted_command("SessionStart", REPO)
        else:
            blocks = installer._json_hook_blocks(profile, REPO).get("SessionStart")
            if blocks is None:
                return False
            command = _command_of(blocks)
        if ADAPTER not in command:
            return False
        return _cursor_family_adapter_launches_trigger()

    raise AssertionError(f"no coverage resolver for client_id={client_id!r}")


def test_the_spec_covers_exactly_the_hook_capable_hosts() -> None:
    """A new host with a hook surface must be decided here, not defaulted."""
    registry = _registry()
    hook_capable = {
        entry.client_id
        for entry in registry.CLIENT_REGISTRY.values()
        if entry.hook_delivery != "none"
    }
    assert set(EXPECTED_UPDATE_TRIGGER) == hook_capable, (
        "the update-trigger coverage spec drifted from the client registry; "
        f"missing={sorted(hook_capable - set(EXPECTED_UPDATE_TRIGGER))} "
        f"stale={sorted(set(EXPECTED_UPDATE_TRIGGER) - hook_capable)}"
    )


@pytest.mark.parametrize("client_id", sorted(EXPECTED_UPDATE_TRIGGER))
def test_each_hook_capable_host_matches_its_declared_update_coverage(client_id: str) -> None:
    expected_reason = EXPECTED_UPDATE_TRIGGER[client_id]
    actual = _mounts_update_trigger(client_id)
    if expected_reason is None:
        assert actual, (
            f"{client_id} is declared as covered but its installer never mounts "
            f"{TRIGGER}; that host would never check for an AQG update"
        )
    else:
        assert not actual, (
            f"{client_id} carries an exemption reason but IS covered now — delete "
            "its reason from EXPECTED_UPDATE_TRIGGER and set it to None"
        )
        assert len(expected_reason.split()) >= 8, (
            f"{client_id}'s exemption reason is too thin to be a reason: {expected_reason!r}"
        )


def test_the_coverage_scan_is_not_vacuous() -> None:
    """A resolver that silently returned False everywhere would pass by omission."""
    covered = [c for c, reason in EXPECTED_UPDATE_TRIGGER.items() if reason is None]
    assert len(covered) >= 12, f"only {len(covered)} hosts declared covered: {covered}"


def test_the_cursor_family_adapter_triggers_before_it_gives_up(capsys) -> None:
    """The adapter returns early when there is no workspace. The update check does
    not need one, so it has to be started before that return — and it still may not
    put a byte on stdout, which is the host's JSON channel."""
    module = _cursor_family_adapter()
    spawned: list[str] = []
    waited: list[str] = []

    def fake_popen(command, *args, **kwargs):
        spawned.append(" ".join(str(part) for part in command))
        return mock.MagicMock()

    def fake_run(command, *args, **kwargs):
        waited.append(" ".join(str(part) for part in command))
        return subprocess.CompletedProcess(command, 0, "", "")

    with mock.patch.object(module.subprocess, "Popen", fake_popen), \
            mock.patch.object(module.subprocess, "run", fake_run):
        code = module._session_start({}, REPO, None)

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out) == {}
    assert captured.err == ""
    assert [call for call in spawned if TRIGGER in call], spawned
    # And nothing waits on it. This adapter is one command for the whole event
    # and already spends up to 35s on preflight and WIP recovery against a 30s
    # budget on the work clients; a wait here is taken out of that.
    assert not [call for call in waited if TRIGGER in call], waited


@pytest.mark.parametrize(
    ("relpath", "attribute"),
    [
        ("scripts/run_aqg_codex_hook.py", "MANAGED_SCRIPTS"),
        ("scripts/agent_client_aqg_hook.py", "HOOK_SCRIPTS"),
        ("agent-packs/qoder/hooks/qoder_hook_adapter.py", "HOOK_SCRIPTS"),
        ("scripts/pi_aqg_hook.py", "HOOK_SCRIPTS"),
    ],
)
def test_every_runner_accepts_the_trigger(relpath: str, attribute: str) -> None:
    """The runners validate `--hook` against an allowlist. An installer that mounts
    a script its own runner refuses produces a hook that fails at every session
    start — loudly on the hosts that surface stderr, invisibly on the rest."""
    module = _load(f"aqg_runner_{Path(relpath).stem}", relpath)
    assert TRIGGER in getattr(module, attribute), (
        f"{relpath}:{attribute} would reject {TRIGGER} with an argparse error"
    )


@pytest.mark.parametrize(
    ("relpath", "attribute"),
    [
        ("scripts/install_aqg_hooks.py", "_AQG_BLOCKING_HOOK_SCRIPTS"),
        ("scripts/install_aqg_agent_clients.py", "BLOCKING_HOOKS"),
        ("scripts/install_aqg_qoder.py", "BLOCKING_HOOKS"),
        ("scripts/run_aqg_codex_hook.py", "BLOCKING_SCRIPTS"),
        ("scripts/agent_client_aqg_hook.py", "BLOCKING_HOOKS"),
        ("agent-packs/qoder/hooks/qoder_hook_adapter.py", "BLOCKING_HOOKS"),
    ],
)
def test_the_trigger_is_blocking_nowhere(relpath: str, attribute: str) -> None:
    """A session-start hook that can fail the session is a session a slow remote
    can stop from starting. This holds per host, not just on Claude Code."""
    module = _load(f"aqg_blocking_{Path(relpath).stem}", relpath)
    assert TRIGGER not in getattr(module, attribute)
