"""Behavior contract for the Codex AQG lifecycle-hook adapter.

All filesystem writes use pytest's temporary directory.  These tests must never
read or modify the user's real ``CODEX_HOME``.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


installer = _load("install_aqg_codex_hooks", "install_aqg_codex_hooks.py")
claude_installer = _load("install_aqg_hooks_for_doctor", "install_aqg_hooks.py")
runner = _load("run_aqg_codex_hook", "run_aqg_codex_hook.py")
doctor = _load("aqg_doctor_codex_hooks", "aqg_doctor.py")


@pytest.fixture(autouse=True)
def _isolate_central_backups(tmp_path, monkeypatch):
    """Route the centralized backup store into this test's tmp dir so no test ever
    writes to the real ~/.deeppattern (backups are centralized, not in-place)."""
    monkeypatch.setenv("AQG_BACKUP_DIR", str(tmp_path / ".aqg-central"))
    monkeypatch.delenv("AQG_BACKUP_KEEP", raising=False)


def _commands(settings: dict) -> list[str]:
    commands: list[str] = []
    for blocks in settings.get("hooks", {}).values():
        for block in blocks:
            for hook in block.get("hooks", []):
                commands.append(hook.get("command", ""))
    return commands


def _apply_patch(*paths_and_bodies: tuple[str, str]) -> dict:
    chunks = ["*** Begin Patch"]
    for path, body in paths_and_bodies:
        chunks.extend((f"*** Update File: {path}", "@@", f"+{body}"))
    chunks.append("*** End Patch")
    return {
        "hook_event_name": "PreToolUse",
        "cwd": str(REPO),
        "tool_name": "apply_patch",
        "tool_input": {"command": "\n".join(chunks)},
    }


def test_specs_cover_every_required_codex_lifecycle_gate():
    specs = installer._aqg_hook_specs(REPO, python_executable=Path(sys.executable))
    assert set(specs) == {
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "Stop",
        "SessionStart",
        "UserPromptSubmit",
    }

    serialized = json.dumps(specs)
    for script in installer.CODEX_HOOK_SCRIPTS:
        assert script in serialized

    for blocks in specs.values():
        for block in blocks:
            for hook in block["hooks"]:
                assert hook["type"] == "command"
                assert "run_aqg_codex_hook.py" in hook["command"]
                assert "commandWindows" in hook
                assert hook["timeout"] > 0

    handoff_hooks = specs["UserPromptSubmit"]
    assert len(handoff_hooks) == 1
    assert handoff_hooks[0]["matcher"] == ""
    assert installer._owned_script(handoff_hooks[0]["hooks"][0]) == (
        "userpromptsubmit_handoff_mandate.sh"
    )


def test_hook_reinstall_keeps_the_managed_root_spelling(tmp_path):
    root = tmp_path / "logical-root"
    root.symlink_to(REPO, target_is_directory=True)
    assert installer._resolve_aqg_root(str(root)) == root
    assert claude_installer._resolve_aqg_root(str(root)) == root
    specs = installer._aqg_hook_specs(root, python_executable=Path(sys.executable))
    for blocks in specs.values():
        for block in blocks:
            for hook in block["hooks"]:
                assert "logical-root" in hook["command"]
                assert "logical-root" in hook["commandWindows"]


def test_codex_sessionstart_pins_one_invocation_to_its_version(tmp_path):
    # Persisted commands follow the logical entrance, but a running invocation
    # must not mix generations. run.check maps this physical root back to the
    # entrance; test_reinstall_auto_update exercises that real update path.
    target = tmp_path / "versions" / ("a" * 40)
    scripts = target / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPTS / "run_aqg_codex_hook.py", scripts)
    hook = target / "agent-packs/claude-code/hooks/sessionstart_update_check.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text('if [ -L "$AQG_ROOT" ]; then echo logical-root; else echo pinned-version; fi\n', encoding="utf-8")
    root = tmp_path / "logical-root"
    root.symlink_to(target, target_is_directory=True)
    result = subprocess.run(
        [sys.executable, "-B", str(root / "scripts/run_aqg_codex_hook.py"), hook.name],
        input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(tmp_path)}),
        capture_output=True, text=True, check=True, timeout=15,
    )
    assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "pinned-version"


def test_apply_preserves_user_hooks_and_is_a_true_second_run_noop(tmp_path, capsys):
    target = tmp_path / "hooks.json"
    original = {
        "description": "user hooks",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo user-policy"}],
                }
            ]
        },
    }
    target.write_text(json.dumps(original), encoding="utf-8")

    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    installed = json.loads(target.read_text(encoding="utf-8"))
    assert "echo user-policy" in _commands(installed)
    assert any("run_aqg_codex_hook.py" in value for value in _commands(installed))
    central = tmp_path / ".aqg-central"
    assert len(list(central.rglob("hooks.json"))) == 1  # one central backup
    first_bytes = target.read_bytes()

    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    assert target.read_bytes() == first_bytes
    assert len(list(central.rglob("hooks.json"))) == 1  # no-op 2nd run adds no backup
    output = capsys.readouterr().out
    assert "on-disk hook definitions already match" in output
    assert "runtime discovery" in output
    assert "/hooks trust" in output


def test_managed_commands_bind_trust_to_runner_and_policy_content():
    specs = installer._aqg_hook_specs(REPO, python_executable=Path(sys.executable))
    commands = [
        hook["command"]
        for blocks in specs.values()
        for block in blocks
        for hook in block["hooks"]
    ]
    assert commands
    assert all("--bundle-sha256" in command for command in commands)
    assert all("--managed-id aqg-codex-v1" in command for command in commands)


def test_uninstall_removes_only_owned_entries_and_keeps_top_level_metadata(tmp_path):
    target = tmp_path / "hooks.json"
    target.write_text(
        json.dumps(
            {
                "description": "keep me",
                "custom": {"keep": True},
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo third-party"}],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    assert installer.cmd_is_installed(target) == 0
    assert installer.cmd_uninstall(target) == 0
    after = json.loads(target.read_text(encoding="utf-8"))
    assert after["description"] == "keep me"
    assert after["custom"] == {"keep": True}
    assert _commands(after) == ["echo third-party"]
    assert installer.cmd_is_installed(target) == installer.EXIT_GENERIC


def test_apply_rejects_malformed_config_without_overwrite(tmp_path):
    target = tmp_path / "hooks.json"
    before = b'{"hooks": ["wrong-shape"]}'
    target.write_bytes(before)
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == installer.EXIT_GENERIC
    assert target.read_bytes() == before
    assert not list((tmp_path / ".aqg-central").rglob("hooks.json"))  # rejected apply = no backup


def test_apply_rejects_missing_python_without_creating_config(tmp_path):
    target = tmp_path / "hooks.json"
    missing_python = tmp_path / "missing-python"
    assert (
        installer.cmd_apply(target, REPO, missing_python)
        == installer.EXIT_GENERIC
    )
    assert not target.exists()


def test_merge_does_not_mutate_caller_input():
    existing = {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user"}]}
        ]
    }
    snapshot = copy.deepcopy(existing)
    merged, changes = installer._merge_hooks(
        existing,
        installer._aqg_hook_specs(REPO, python_executable=Path(sys.executable)),
    )
    assert changes
    assert existing == snapshot
    assert merged != existing


def test_reapply_moves_owned_hook_from_stale_matcher(tmp_path):
    target = tmp_path / "hooks.json"
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    config = json.loads(target.read_text(encoding="utf-8"))
    for block in config["hooks"]["PreToolUse"]:
        for hook in block["hooks"]:
            if "pretooluse_secret_scan.sh" in hook.get("command", ""):
                block["matcher"] = "Bash"
    target.write_text(json.dumps(config), encoding="utf-8")

    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    repaired = json.loads(target.read_text(encoding="utf-8"))
    locations = [
        block["matcher"]
        for block in repaired["hooks"]["PreToolUse"]
        for hook in block["hooks"]
        if "pretooluse_secret_scan.sh" in hook.get("command", "")
    ]
    assert locations == ["Bash|apply_patch"]


def test_reapply_removes_duplicate_and_misplaced_owned_hooks(tmp_path):
    target = tmp_path / "hooks.json"
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    config = json.loads(target.read_text(encoding="utf-8"))
    owned = copy.deepcopy(config["hooks"]["PreToolUse"][0]["hooks"][0])
    config["hooks"]["PreToolUse"][0]["hooks"].append(copy.deepcopy(owned))
    config["hooks"].setdefault("PostToolUse", []).append(
        {"matcher": "Bash", "hooks": [copy.deepcopy(owned)]}
    )
    target.write_text(json.dumps(config), encoding="utf-8")

    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    repaired = json.loads(target.read_text(encoding="utf-8"))
    occurrences = [
        (event, matcher)
        for event, matcher, hook in installer._iter_entries(repaired["hooks"])
        if installer._owned_script(hook) == "pretooluse_bash_skill_validator.sh"
    ]
    assert occurrences == [("PreToolUse", "Bash")]


def test_reapply_repairs_partially_corrupted_marked_hook(tmp_path):
    target = tmp_path / "hooks.json"
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    config = json.loads(target.read_text(encoding="utf-8"))
    hook = config["hooks"]["PreToolUse"][0]["hooks"][0]
    hook.pop("commandWindows")
    target.write_text(json.dumps(config), encoding="utf-8")
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    repaired = json.loads(target.read_text(encoding="utf-8"))
    owned = [
        item
        for _event, _matcher, item in installer._iter_entries(repaired["hooks"])
        if installer._owned_script(item) == "pretooluse_bash_skill_validator.sh"
    ]
    assert len(owned) == 1
    assert "commandWindows" in owned[0]


def test_substring_collision_without_exact_argv_is_not_owned():
    hook = {
        "command": "python /third/runner.py --note run_aqg_codex_hook.py pretooluse_secret_scan.sh",
        "commandWindows": "python C:\\third\\runner.py --note run_aqg_codex_hook.py pretooluse_secret_scan.sh",
    }
    assert installer._owned_script(hook) is None


def test_legacy_windows_command_is_owned_independent_of_host_path_rules():
    hook = {
        "command": "python /old/run_aqg_codex_hook.py pretooluse_bash_skill_validator.sh",
        "commandWindows": (
            "python C:\\old\\run_aqg_codex_hook.py "
            "pretooluse_bash_skill_validator.sh"
        ),
    }
    assert installer._owned_script(hook) == "pretooluse_bash_skill_validator.sh"


def test_posix_literal_backslash_runner_is_not_owned():
    hook = {
        "command": (
            "python '/opt/custom\\run_aqg_codex_hook.py' "
            "pretooluse_bash_skill_validator.sh"
        ),
        "commandWindows": (
            "python C:\\third-party\\runner.py pretooluse_bash_skill_validator.sh"
        ),
    }
    assert installer._owned_script(hook) is None


def test_current_windows_runner_and_policy_paths_are_owned():
    script = "pretooluse_secret_scan.sh"
    digest = "0" * 64
    posix_argv = [
        "python", "-c", installer.VERIFY_CODE,
        "/old/run_aqg_codex_hook.py", f"/old/{script}",
        "--bundle-sha256", digest, script, "--managed-id", installer.MANAGED_ID,
    ]
    windows_argv = [
        "python", "-c", installer.VERIFY_CODE,
        "C:\\old\\run_aqg_codex_hook.py", f"C:\\old\\{script}",
        "--bundle-sha256", digest, script, "--managed-id", installer.MANAGED_ID,
    ]
    hook = {
        "command": " ".join(shlex.quote(value) for value in posix_argv),
        "commandWindows": subprocess.list2cmdline(windows_argv),
    }
    assert installer._owned_script(hook) == script


def test_apply_patch_secret_scan_receives_full_literal_content():
    payload = _apply_patch(("src/auth.py", "TOKEN_LITERAL"))
    translated = runner._translate_payloads("pretooluse_secret_scan.sh", payload)
    assert len(translated) == 1
    assert translated[0]["tool_name"] == "Write"
    assert "TOKEN_LITERAL" in translated[0]["tool_input"]["content"]


def test_apply_patch_parser_keeps_inserted_lines_starting_with_double_plus():
    payload = _apply_patch(("src/auth.py", "++SECRET_LITERAL"))
    translated = runner._translate_payloads("pretooluse_secret_scan.sh", payload)
    assert "++SECRET_LITERAL" in translated[0]["tool_input"]["content"]


def test_apply_patch_secret_scan_receives_changed_paths():
    payload = _apply_patch(("src/auth.py", "TOKEN_LITERAL"))
    translated = runner._translate_payloads("pretooluse_secret_scan.sh", payload)
    assert Path(translated[0]["tool_input"]["file_path"]) == REPO / "src" / "auth.py"


def test_apply_patch_file_reminders_receive_every_changed_path():
    payload = _apply_patch(("src/auth.py", "one"), ("tests/test_auth.py", "two"))
    translated = runner._translate_payloads(
        "posttooluse_code_construction_reminder.sh", payload
    )
    assert [Path(item["tool_input"]["file_path"]) for item in translated] == [
        REPO / "src" / "auth.py",
        REPO / "tests" / "test_auth.py",
    ]
    assert all(item["tool_name"] == "Edit" for item in translated)


def test_apply_patch_memory_guard_receives_per_file_new_content():
    payload = _apply_patch(
        ("C:/Users/test/.codex/memories/a.md", "```python"),
        ("src/app.py", "print('ok')"),
    )
    translated = runner._translate_payloads("pretooluse_memory_write_guard.sh", payload)
    assert len(translated) == 2
    first = translated[0]["tool_input"]
    assert Path(first["file_path"]).parts[-3:] == (".codex", "memories", "a.md")
    assert "```python" in first["content"]
    assert Path(translated[1]["tool_input"]["file_path"]) == REPO / "src" / "app.py"


def test_apply_patch_relative_memory_path_is_resolved_against_cwd(tmp_path):
    memory = tmp_path / ".codex" / "memories"
    payload = _apply_patch((".codex/memories/project.md", "```python"))
    payload["cwd"] = str(tmp_path)
    translated = runner._translate_payloads("pretooluse_memory_write_guard.sh", payload)
    assert translated[0]["tool_input"]["file_path"] == str(
        memory / "project.md"
    )


def test_apply_patch_move_uses_destination_path_for_memory_guard():
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(REPO),
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "\n".join(
                (
                    "*** Begin Patch",
                    "*** Update File: notes.md",
                    "*** Move to: C:/Users/test/.codex/memories/notes.md",
                    "@@",
                    "+```python",
                    "*** End Patch",
                )
            )
        },
    }
    translated = runner._translate_payloads("pretooluse_memory_write_guard.sh", payload)
    assert Path(translated[0]["tool_input"]["file_path"]).parts[-3:] == (
        ".codex",
        "memories",
        "notes.md",
    )


def test_apply_patch_move_without_added_content_is_marked_uninspectable():
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(REPO),
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "\n".join(
                (
                    "*** Begin Patch",
                    "*** Update File: notes.md",
                    "*** Move to: .codex/memories/notes.md",
                    "*** End Patch",
                )
            )
        },
    }
    translated = runner._translate_payloads("pretooluse_memory_write_guard.sh", payload)
    assert translated[0]["tool_input"]["aqg_content_unavailable"] is True


def test_apply_patch_move_with_added_content_is_still_marked_uninspectable():
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(REPO),
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "\n".join(
                (
                    "*** Begin Patch",
                    "*** Update File: notes.md",
                    "*** Move to: .codex/memories/notes.md",
                    "@@",
                    "+benign footer",
                    "*** End Patch",
                )
            )
        },
    }
    translated = runner._translate_payloads("pretooluse_memory_write_guard.sh", payload)
    assert translated[0]["tool_input"]["aqg_content_unavailable"] is True


def test_bash_failure_is_normalized_for_existing_debugging_hook():
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_response": {"exit_code": 7, "output": "failed"},
    }
    translated = runner._translate_payloads(
        "posttooluse_bash_error_debugging_reminder.sh", payload
    )
    assert translated[0]["tool_response"]["is_error"] is True


def test_bash_argv_array_is_normalized_for_blocking_scan():
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": ["printf", "%s", "literal-value"]},
    }
    translated = runner._translate_payloads("pretooluse_secret_scan.sh", payload)
    assert translated[0]["tool_input"]["command"] == "printf %s literal-value"


def test_bash_invalid_command_shape_fails_translation_closed():
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": {"unexpected": "shape"}},
    }
    assert runner._translate_payloads("pretooluse_secret_scan.sh", payload) == []


def test_runner_environment_uses_stable_repo_and_codex_memory_root(tmp_path):
    payload = {"cwd": str(tmp_path)}
    env = runner._hook_environment(REPO, payload, base_env={"HOME": str(tmp_path)})
    assert env["AQG_ROOT"] == str(REPO)
    assert env["AQG_CLIENT"] == "codex"
    assert env["CLAUDE_PROJECT_DIR"] == str(tmp_path)
    assert env["AQG_MEMORY_ROOT"] == str(tmp_path / ".codex" / "memories")


def test_runner_environment_honors_custom_codex_home(tmp_path):
    custom_home = tmp_path / "custom-codex"
    env = runner._hook_environment(
        REPO,
        {"cwd": str(tmp_path)},
        base_env={"HOME": str(tmp_path), "CODEX_HOME": str(custom_home)},
    )
    assert env["AQG_MEMORY_ROOT"] == str(custom_home / "memories")


def test_codex_memory_guard_blocks_code_shaped_memory_write(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    memory_file = tmp_path / ".codex" / "memories" / "project.md"
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(REPO),
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(memory_file),
            "content": "```python\ndef durable_bug_fix():\n    pass\n```",
        },
    }
    result = runner._run_hook("pretooluse_memory_write_guard.sh", payload, REPO)
    assert result.returncode == 2
    assert "memory-write-guard" in result.stderr


def test_codex_memory_guard_blocks_symlink_escape(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    memory_dir = tmp_path / ".codex" / "memories"
    memory_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("safe", encoding="utf-8")
    link = memory_dir / "project.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(tmp_path),
        "tool_name": "Write",
        "tool_input": {"file_path": str(link), "content": "plain prose"},
    }
    result = runner._run_hook("pretooluse_memory_write_guard.sh", payload, REPO)
    assert result.returncode == 2
    assert "escapes through a symlink" in result.stderr


def test_claude_memory_guard_blocks_symlink_escape(monkeypatch, tmp_path):
    bash = runner._find_bash()
    if not bash:
        pytest.skip("bash runtime is unavailable")
    memory_dir = tmp_path / ".claude" / "projects" / "project-hash" / "memory"
    memory_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("safe", encoding="utf-8")
    link = memory_dir / "project.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(link), "content": "plain prose"},
    }
    env = dict(os.environ)
    env.update({"AQG_ROOT": str(REPO), "HOME": str(tmp_path)})
    env.pop("AQG_MEMORY_ROOT", None)
    result = subprocess.run(
        [bash, (REPO / "agent-packs/claude-code/hooks/pretooluse_memory_write_guard.sh").as_posix()],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 2
    assert "escapes through a symlink" in result.stderr


def test_claude_memory_guard_blocks_code_shaped_write(tmp_path):
    bash = runner._find_bash()
    if not bash:
        pytest.skip("bash runtime is unavailable")
    target = tmp_path / ".claude" / "projects" / "project-hash" / "memory" / "note.md"
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "```python\nprint('x')\n```"},
    }
    env = dict(os.environ)
    env.update({"AQG_ROOT": str(REPO), "HOME": str(tmp_path)})
    env.pop("AQG_MEMORY_ROOT", None)
    result = subprocess.run(
        [bash, (REPO / "agent-packs/claude-code/hooks/pretooluse_memory_write_guard.sh").as_posix()],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 2


def test_codex_memory_guard_blocks_uninspectable_move(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    memory_file = tmp_path / ".codex" / "memories" / "project.md"
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(tmp_path),
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(memory_file),
            "content": "",
            "aqg_content_unavailable": True,
        },
    }
    result = runner._run_hook("pretooluse_memory_write_guard.sh", payload, REPO)
    assert result.returncode == 2
    assert "cannot inspect content moved into memory" in result.stderr


def test_pretool_block_is_rendered_as_codex_deny_without_secret_echo():
    result = runner.HookRun(2, "", "[aqg secret-scan] BLOCK: github_token x1")
    output = runner._render_output("PreToolUse", [result], {})
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "github_token" in specific["permissionDecisionReason"]


def test_pretool_policy_deny_takes_precedence_over_stdout_json():
    result = runner.HookRun(2, '{"systemMessage":"allow-looking output"}', "policy denied")
    output = runner._render_output("PreToolUse", [result], {})
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_infrastructure_failure_warns_without_policy_deny():
    result = runner.HookRun(3, "", "[aqg codex-hook] DEGRADED: bash runtime not found")
    output = runner._render_output("PreToolUse", [result], {}, "pretooluse_secret_scan.sh")
    assert output == {"systemMessage": "[aqg codex-hook] DEGRADED: bash runtime not found"}


def test_pretool_warn_only_output_becomes_additional_context():
    result = runner.HookRun(0, "warn before proceeding", "")
    output = runner._render_output("PreToolUse", [result], {}, "pretooluse_secret_scan.sh")
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "warn before proceeding",
        }
    }


def test_posttool_reminder_becomes_model_visible_additional_context():
    result = runner.HookRun(0, "", "run aqg-systematic-debugging")
    output = runner._render_output("PostToolUse", [result], {})
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "run aqg-systematic-debugging",
        }
    }


def test_stop_output_is_always_valid_json_and_does_not_loop():
    reminder = runner.HookRun(0, "", "complete evidence closeout")
    first = runner._render_output(
        "Stop", [reminder], {"stop_hook_active": False}, "precompact_closeout_reminder.sh"
    )
    repeated = runner._render_output(
        "Stop", [reminder], {"stop_hook_active": True}, "precompact_closeout_reminder.sh"
    )
    assert first == {
        "decision": "block",
        "reason": "complete evidence closeout",
    }
    assert repeated == {}
    assert runner._render_output("Stop", [], {}, "precompact_closeout_reminder.sh") == {}


def test_stop_wip_checkpoint_message_does_not_force_continuation():
    saved = runner.HookRun(0, "saved checkpoint", "")
    assert runner._render_output("Stop", [saved], {}, "wip_checkpoint_save.sh") == {
        "systemMessage": "saved checkpoint"
    }


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_context_events_wrap_plain_text_in_official_json_shape(event):
    result = runner.HookRun(0, "model-visible guidance", "")
    assert runner._render_output(event, [result], {}, "sessionstart_preflight.sh") == {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": "model-visible guidance",
        }
    }


def test_user_prompt_hook_json_is_forwarded_unchanged():
    expected = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "invoke aqg-session-handoff",
        }
    }
    result = runner.HookRun(0, json.dumps(expected), "")
    assert runner._render_output("UserPromptSubmit", [result], {}) == expected


def test_runner_rejects_unknown_script_identifier(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"read": lambda self: "{}"})())
    assert runner.main(["../../unmanaged.sh"]) == 2
    assert "not a managed AQG hook" in capsys.readouterr().err


def test_runner_fails_closed_on_malformed_pretool_payload(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"read": lambda self: "{"})())
    assert runner.main(["pretooluse_secret_scan.sh"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_runner_fails_closed_on_missing_pretool_fields(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"read": lambda self: "{}"})())
    assert runner.main(["pretooluse_memory_write_guard.sh"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_runner_fails_closed_when_apply_patch_cannot_be_parsed(monkeypatch, capsys):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": "not an apply_patch envelope"},
    }
    monkeypatch.setattr(
        sys, "stdin", type("Input", (), {"read": lambda self: json.dumps(payload)})()
    )
    assert runner.main(["pretooluse_secret_scan.sh"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_runner_digest_mismatch_warns_without_locking_tools(monkeypatch, capsys):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo safe"},
    }
    monkeypatch.setattr(
        sys, "stdin", type("Input", (), {"read": lambda self: json.dumps(payload)})()
    )
    assert runner.main(
        [
            "pretooluse_secret_scan.sh",
            "--bundle-sha256",
            "0" * 64,
            "--managed-id",
            runner.MANAGED_ID,
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert "digest mismatch" in output["systemMessage"]
    assert "permissionDecision" not in json.dumps(output)


def test_runner_unreadable_bundle_warns_without_traceback(monkeypatch, capsys):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo safe"},
    }
    monkeypatch.setattr(
        sys, "stdin", type("Input", (), {"read": lambda self: json.dumps(payload)})()
    )
    monkeypatch.setattr(runner, "_bundle_digest", lambda *_args: (_ for _ in ()).throw(OSError("missing")))
    assert runner.main(
        [
            "pretooluse_secret_scan.sh",
            "--bundle-sha256",
            "0" * 64,
            "--managed-id",
            runner.MANAGED_ID,
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert "cannot verify managed bundle" in output["systemMessage"]


def test_trusted_command_verifies_bundle_before_runner_executes(tmp_path):
    fake_runner = tmp_path / installer.RUNNER_NAME
    marker = tmp_path / "executed"
    fake_runner.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    policy = tmp_path / "pretooluse_secret_scan.sh"
    policy.write_text("exit 0\n", encoding="utf-8")
    digest = installer._bundle_digest(fake_runner, policy)
    command, _command_windows = installer._command(
        Path(sys.executable), fake_runner, policy, policy.name, digest
    )
    fake_runner.write_text("raise SystemExit('tampered')\n", encoding="utf-8")
    completed = subprocess.run(
        shlex.split(command),
        input="{}",
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 3
    assert not marker.exists()


def test_run_translated_shares_one_total_timeout_budget(monkeypatch):
    observed = []
    clock = iter((10.0, 10.0, 12.0, 49.5))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))

    def fake_run(_script, _payload, _root, *, timeout):
        observed.append(timeout)
        return runner.HookRun(0, "", "")

    monkeypatch.setattr(runner, "_run_hook", fake_run)
    results = runner._run_translated(
        "posttooluse_code_construction_reminder.sh", [{}, {}, {}], REPO, total_timeout=40
    )
    assert len(results) == 3
    assert observed == [40.0, 38.0, 0.5]


def test_verify_reports_missing_and_complete_install(tmp_path, capsys):
    target = tmp_path / "hooks.json"
    assert installer.cmd_verify(target, REPO, Path(sys.executable)) == installer.EXIT_GENERIC
    assert "NOT INSTALLED" in capsys.readouterr().out
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    assert installer.cmd_verify(target, REPO, Path(sys.executable)) == 0
    output = capsys.readouterr().out
    assert "on-disk" in output
    assert "runtime discovery" in output
    assert "/hooks" in output and "trust" in output


def test_default_target_honors_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert installer._default_target() == tmp_path / "codex-home" / "hooks.json"


def test_main_refuses_symlinked_target_without_touching_destination(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "hooks.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    before = real.read_bytes()
    assert installer.main(["--apply", "--target", str(link), "--aqg-root", str(REPO)]) == 1
    assert real.read_bytes() == before


def test_apply_reports_malformed_block_without_traceback(tmp_path, capsys):
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash"}]}}), encoding="utf-8")
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == installer.EXIT_GENERIC
    assert "hooks" in capsys.readouterr().err


def test_apply_preserves_empty_third_party_hook_block(tmp_path):
    target = tmp_path / "hooks.json"
    empty_block = {"matcher": "ThirdParty", "hooks": [], "note": "keep"}
    target.write_text(
        json.dumps({"hooks": {"PreToolUse": [empty_block]}}), encoding="utf-8"
    )
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert empty_block in data["hooks"]["PreToolUse"]


def test_verify_rejects_missing_installed_interpreter(tmp_path):
    target = tmp_path / "hooks.json"
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    hook = data["hooks"]["PreToolUse"][0]["hooks"][0]
    missing = str(tmp_path / "missing-python")
    for key, windows in (("command", False), ("commandWindows", True)):
        argv = installer._parse_command(hook[key], windows=windows)
        argv[0] = missing
        hook[key] = subprocess.list2cmdline(argv) if windows else " ".join(shlex.quote(x) for x in argv)
    target.write_text(json.dumps(data), encoding="utf-8")
    status, detail = installer.inspect_install(target, REPO, Path(sys.executable))
    assert status == "stale"
    assert "interpreter" in detail


def test_installer_and_runner_managed_script_sets_match():
    assert set(installer.CODEX_HOOK_SCRIPTS) == set(runner.MANAGED_SCRIPTS)


def test_doctor_warns_when_codex_hooks_are_not_installed(tmp_path):
    result = doctor.check_codex_hooks(tmp_path / "hooks.json", REPO)
    assert result.status == "WARN"
    assert result.name == "codex_hooks"


def test_doctor_default_omits_absent_opt_in_codex_hooks(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    args = doctor.parse_args(
        [
            "--no-cli",
            "--codex-skills-dir",
            str(codex_home / "skills"),
            "--claude-skills-dir",
            str(tmp_path / ".claude" / "skills"),
        ]
    )
    results, _aqg_root = doctor._run_install_mode(args)
    assert all(result.name != "codex_hooks" for result in results)


def test_doctor_checks_codex_hooks_when_codex_skills_are_installed(monkeypatch, tmp_path):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    (codex_home / "skills" / "aqg-startup-preflight").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    args = doctor.parse_args(
        [
            "--no-cli",
            "--codex-skills-dir",
            str(codex_home / "skills"),
            "--claude-skills-dir",
            str(home / ".claude" / "skills"),
        ]
    )

    results, _aqg_root = doctor._run_install_mode(args)

    codex_hooks = [result for result in results if result.name == "codex_hooks"]
    assert len(codex_hooks) == 1
    assert codex_hooks[0].status == "WARN"
    assert "no AQG Codex hooks" in codex_hooks[0].detail


def test_doctor_explicit_missing_codex_hooks_file_warns(tmp_path):
    target = tmp_path / "missing-hooks.json"
    args = doctor.parse_args(["--no-cli", "--codex-hooks-file", str(target)])
    results, _aqg_root = doctor._run_install_mode(args)
    codex_hooks = [result for result in results if result.name == "codex_hooks"]
    assert len(codex_hooks) == 1
    assert codex_hooks[0].status == "WARN"


def test_doctor_diagnoses_dangling_codex_hooks_symlink(monkeypatch, tmp_path):
    target = tmp_path / "hooks.json"
    try:
        target.symlink_to(tmp_path / "missing.json")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    results, _aqg_root = doctor._run_install_mode(doctor.parse_args(["--no-cli"]))
    codex_hooks = [result for result in results if result.name == "codex_hooks"]
    assert len(codex_hooks) == 1
    assert codex_hooks[0].status == "FAIL"


def test_doctor_passes_complete_codex_hook_install(tmp_path):
    target = tmp_path / "hooks.json"
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    result = doctor.check_codex_hooks(target, REPO)
    assert result.status == "PASS"
    assert "on-disk" in result.detail
    assert "runtime discovery" in result.detail
    assert "/hooks" in result.detail


def _doctor_managed_install(root, home, *, env_root):
    env = os.environ.copy()
    env.pop("AQG_ROOT", None)
    if env_root is not None:
        env["AQG_ROOT"] = env_root
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / ".codex"))
    proc = subprocess.run(
        [
            sys.executable, "-B", str(root / "scripts/aqg_doctor.py"),
            "--json", "--no-cli",
            "--codex-hooks-file", str(home / "hooks.json"),
            "--claude-settings-file", str(home / "settings.json"),
            "--codex-skills-dir", str(home / "codex-skills"),
            "--claude-skills-dir", str(home / "claude-skills"),
        ],
        cwd=home, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    return {item["name"]: item for item in json.loads(proc.stdout)["results"]}


@pytest.mark.parametrize("root_source", ["absolute", "relative", "home", "doctor-location"])
def test_doctor_and_installer_agree_on_managed_root(tmp_path, root_source):
    root = tmp_path / "managed root"
    root.symlink_to(REPO, target_is_directory=True)
    target = tmp_path / "hooks.json"
    assert installer.main(["--apply", "--aqg-root", str(root), "--target", str(target)]) == 0
    assert installer.main(["--verify", "--aqg-root", str(root), "--target", str(target)]) == 0

    env_root = {
        "absolute": str(root), "relative": root.name,
        "home": "~/" + root.name, "doctor-location": None,
    }[root_source]
    results = _doctor_managed_install(root, tmp_path, env_root=env_root)

    assert results["codex_hooks"]["status"] == "PASS", results["codex_hooks"]


def test_doctor_skill_containment_through_managed_root(tmp_path):
    root = tmp_path / "managed-root"
    root.symlink_to(REPO, target_is_directory=True)
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "inside").symlink_to(root / "skills/aqg-code-construction", target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("fixture", encoding="utf-8")
    (skills / "outside").symlink_to(outside, target_is_directory=True)
    missing = tmp_path / "empty-root"
    missing.mkdir()
    (skills / "missing").symlink_to(missing, target_is_directory=True)

    results = {item.name: item for item in doctor.check_skill_install(
        label="skill", target_dir=skills, expected_names=("inside", "outside"), aqg_root=root,
    )}
    assert results["skill:inside"].status == "PASS", results["skill:inside"]
    assert results["skill:outside"].status == "WARN"
    empty_root = tmp_path / "empty-root-link"
    empty_root.symlink_to(missing, target_is_directory=True)
    result = doctor.check_skill_install(
        label="skill", target_dir=skills, expected_names=("missing",), aqg_root=empty_root,
    )[-1]
    assert result.status == "FAIL"
    assert "missing SKILL.md" in result.detail


@pytest.mark.parametrize("error", [OSError("unavailable root"), RuntimeError("root link loop")])
def test_doctor_reports_root_resolution_error_without_blaming_skill(tmp_path, monkeypatch, error):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "inside").symlink_to(REPO / "skills/aqg-code-construction", target_is_directory=True)
    root = tmp_path / "managed-root"
    real_resolve = type(root).resolve

    def resolve(path, *args, **kwargs):
        if path == root:
            raise error
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(root), "resolve", resolve)
    results = doctor.check_skill_install(
        label="skill", target_dir=skills, expected_names=("inside",), aqg_root=root,
    )
    failures = [result for result in results if result.status == "FAIL"]
    assert len(failures) == 1
    assert failures[0].name == "skill_root"
    assert "AQG_ROOT" in failures[0].detail
    assert "AQG_ROOT" in failures[0].fix


def test_doctor_file_symlink_falls_back_to_checkout(tmp_path, monkeypatch):
    script = tmp_path / "bin/aqg-doctor"
    script.parent.mkdir()
    script.symlink_to(SCRIPTS / "aqg_doctor.py")
    monkeypatch.delenv("AQG_ROOT", raising=False)
    monkeypatch.setattr(doctor, "__file__", str(script))
    root, _source = doctor.resolve_aqg_root()
    assert root == REPO
    target = tmp_path / "hooks.json"
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    assert doctor.check_codex_hooks(target, root).status == "PASS"


def test_doctor_explicit_non_checkout_root_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv("AQG_ROOT", str(tmp_path))
    root, source = doctor.resolve_aqg_root()
    assert root == tmp_path
    results = doctor.check_aqg_root(root, source)
    assert any(item.name == "version" and item.status == "FAIL" for item in results)


def test_doctor_preserves_upgrade_following_and_detects_real_hook_drift(tmp_path, monkeypatch):
    versions = [tmp_path / "versions" / name for name in ("v1", "v2")]
    for version in versions:
        (version / "scripts").mkdir(parents=True)
        (version / "VERSION").write_text(version.name, encoding="utf-8")
        shutil.copy2(SCRIPTS / installer.RUNNER_NAME, version / "scripts")
        shutil.copytree(REPO / "agent-packs/claude-code/hooks", version / "agent-packs/claude-code/hooks")
    root = tmp_path / "managed-root"
    root.symlink_to(versions[0], target_is_directory=True)
    monkeypatch.setenv("AQG_ROOT", str(root))
    target = tmp_path / "hooks.json"
    python = Path(sys.executable)
    assert installer.cmd_apply(target, root, python) == 0
    original = target.read_bytes()

    # Moving the entrance with identical hook content needs no config rewrite.
    root.unlink()
    root.symlink_to(versions[1], target_is_directory=True)
    assert installer.cmd_verify(target, root, python) == 0
    assert doctor.check_codex_hooks(target, doctor.resolve_aqg_root()[0]).status == "PASS"
    assert target.read_bytes() == original

    # The physical-version workaround must remain stale, even at the same version.
    assert installer.cmd_apply(target, root.resolve(), python) == 0
    assert doctor.check_codex_hooks(target, doctor.resolve_aqg_root()[0]).status == "FAIL"
    assert installer.cmd_apply(target, root, python) == 0
    assert doctor.check_codex_hooks(target, doctor.resolve_aqg_root()[0]).status == "PASS"

    # Content changes still invalidate the reviewed bundle; path handling must
    # never turn an actual digest mismatch into PASS.
    policy = root / "agent-packs/claude-code/hooks" / installer.CODEX_HOOK_SCRIPTS[0]
    policy.write_bytes(policy.read_bytes() + b"\n# changed fixture policy\n")
    assert installer.cmd_verify(target, root, python) == 1
    result = doctor.check_codex_hooks(target, doctor.resolve_aqg_root()[0])
    assert result.status == "FAIL" and "stale" in result.detail
    assert installer.cmd_apply(target, root, python) == 0
    assert installer.cmd_verify(target, root, python) == 0
    assert doctor.check_codex_hooks(target, doctor.resolve_aqg_root()[0]).status == "PASS"


def test_doctor_warns_when_claude_skills_are_installed_without_hooks(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "aqg-startup-preflight").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    args = doctor.parse_args(
        [
            "--no-cli",
            "--claude-skills-dir",
            str(home / ".claude" / "skills"),
            "--codex-skills-dir",
            str(home / ".codex" / "skills"),
        ]
    )

    results, _aqg_root = doctor._run_install_mode(args)

    claude_hooks = [result for result in results if result.name == "claude_hooks"]
    assert len(claude_hooks) == 1
    assert claude_hooks[0].status == "WARN"
    assert "no AQG Claude Code hooks" in claude_hooks[0].detail


def test_doctor_passes_complete_claude_hook_install(tmp_path):
    target = tmp_path / "settings.json"
    assert claude_installer.cmd_apply(target, REPO) == 0
    result = doctor.check_claude_hooks(target, REPO)
    assert result.status == "PASS"
    assert "on-disk" in result.detail


def test_verify_ignores_interpreter_only_drift(tmp_path):
    target = tmp_path / "hooks.json"
    assert installer.cmd_apply(target, REPO, Path(sys.executable)) == 0
    alternate = tmp_path / "alternate-python"
    alternate.write_text("placeholder", encoding="utf-8")
    status, _detail = installer.inspect_install(target, REPO, alternate)
    assert status == "complete"


def test_doctor_fails_malformed_or_stale_codex_hook_install(tmp_path):
    malformed = tmp_path / "bad.json"
    malformed.write_text('{"hooks": []}', encoding="utf-8")
    assert doctor.check_codex_hooks(malformed, REPO).status == "FAIL"

    stale = tmp_path / "stale.json"
    assert installer.cmd_apply(stale, REPO, Path(sys.executable)) == 0
    data = json.loads(stale.read_text(encoding="utf-8"))
    data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 1
    stale.write_text(json.dumps(data), encoding="utf-8")
    assert doctor.check_codex_hooks(stale, REPO).status == "FAIL"
