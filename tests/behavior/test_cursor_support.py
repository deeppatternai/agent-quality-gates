"""Behavior contracts for the Cursor client-support installer."""

from __future__ import annotations

import re
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install_cursor_support.py"
ADAPTER = REPO / "scripts" / "cursor_aqg_hook.py"

sys.path.insert(0, str(REPO / "scripts"))
from aqg_policy_markers import gate_a_tokens, missing_gate_a_tokens  # noqa: E402

MANAGED_MARKER = ".aqg-cursor-managed.json"
LINK_MARKER_DIR = "managed-links"
LEGACY_LINK_MARKER_DIR = ".aqg-cursor-managed-links"
RULE_MARKER = "AQG CURSOR MANAGED RULE"


def _expected_skills() -> list[str]:
    return sorted(
        path.name for path in (REPO / "skills").glob("aqg-*") if (path / "SKILL.md").is_file()
    )


def _run_cursor_installer(*args: str) -> subprocess.CompletedProcess[str]:
    # Route the central backup store into the per-test tmp tree so backups never
    # leak to the real store or to dirname(REPO)/aqg-backups.
    env = os.environ.copy()
    for flag in ("--project-root", "--home"):
        if flag in args:
            env["AQG_BACKUP_DIR"] = str(Path(args[args.index(flag) + 1]) / ".aqg-central")
            break
    return subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _run_adapter(event: str, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ADAPTER),
            event,
            "--aqg-root",
            str(REPO),
        ],
        cwd=REPO,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def _run_adapter_bytes(event: str, payload: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(ADAPTER), event, "--aqg-root", str(REPO)],
        cwd=REPO,
        input=payload,
        capture_output=True,
        check=False,
    )


def _link_marker(skill: Path) -> Path:
    return skill.parent.parent / LINK_MARKER_DIR / f"{skill.name}.json"


def _legacy_link_marker(skill: Path) -> Path:
    return skill.parent / LEGACY_LINK_MARKER_DIR / f"{skill.name}.json"


def _legacy_marker_for(skill_name: str) -> dict[str, object]:
    source = REPO / "skills" / skill_name
    return {
        "manager": "aqg-cursor-support",
        "schema_version": 2,
        "skill": skill_name,
        "install_mode": "link",
        "source_path": str(source.resolve()),
    }


def _is_link_install(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(isjunction is not None and isjunction(path))


def _create_link(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlinks unavailable: {exc}")
    cmd = shutil.which("cmd.exe") or shutil.which("cmd")
    if cmd is None:
        pytest.skip("cmd unavailable for junction fixture")
    proc = subprocess.run(
        [cmd, "/c", "mklink", "/J", str(target), str(source)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0 or not _is_link_install(target):
        pytest.skip("directory link fixtures unavailable")


def test_project_apply_installs_skills_and_rule_without_clobbering(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    rules_dir = project / ".cursor" / "rules"
    rules_dir.mkdir(parents=True)
    existing_rule = rules_dir / "team.mdc"
    existing_rule.write_text("team-owned\n", encoding="utf-8")

    proc = _run_cursor_installer(
        "--apply",
        "--scope",
        "project",
        "--project-root",
        str(project),
        "--home",
        str(home),
        "--no-hooks",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert existing_rule.read_text(encoding="utf-8") == "team-owned\n"

    expected_skills = _expected_skills()
    installed_root = project / ".cursor" / "skills"
    assert sorted(path.name for path in installed_root.glob("aqg-*")) == expected_skills
    assert all(_is_link_install(installed_root / name) for name in expected_skills)
    assert all(
        (installed_root / name).resolve(strict=True) == (REPO / "skills" / name).resolve(strict=True)
        for name in expected_skills
    )
    assert sorted(path.name for path in (project / ".cursor" / LINK_MARKER_DIR).glob("*.json")) == [
        f"{name}.json" for name in expected_skills
    ]
    assert not (installed_root / LEGACY_LINK_MARKER_DIR).exists()
    assert not any(path.name.startswith(".aqg-") for path in installed_root.iterdir())

    aqg_rule = rules_dir / "aqg.mdc"
    rule_text = aqg_rule.read_text(encoding="utf-8")
    assert "alwaysApply: true" in rule_text
    assert "AQG CURSOR MANAGED RULE" in rule_text
    assert "Agent Quality Gates (AQG) engineering discipline" in rule_text


def test_project_apply_supports_explicit_copy_mode(tmp_path: Path) -> None:
    project = tmp_path / "project"

    proc = _run_cursor_installer(
        "--apply",
        "--mode",
        "copy",
        "--scope",
        "project",
        "--project-root",
        str(project),
        "--no-hooks",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    installed_root = project / ".cursor" / "skills"
    for name in _expected_skills():
        installed = installed_root / name
        assert installed.is_dir()
        assert not installed.is_symlink()
        assert (installed / MANAGED_MARKER).is_file()
    assert (
        _run_cursor_installer(
            "--verify",
            "--mode",
            "copy",
            "--scope",
            "project",
            "--project-root",
            str(project),
            "--no-hooks",
        ).returncode
        == 0
    )


def test_project_apply_merges_native_hooks_and_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    cursor_root = project / ".cursor"
    cursor_root.mkdir(parents=True)
    hooks_path = cursor_root / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "afterFileEdit": [
                        {"command": ".cursor/hooks/team-format.sh", "timeout": 7}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    args = (
        "--apply",
        "--scope",
        "project",
        "--project-root",
        str(project),
        "--home",
        str(tmp_path / "home"),
    )
    first = _run_cursor_installer(*args)
    second = _run_cursor_installer(*args)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    config = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert config["hooks"]["afterFileEdit"] == [
        {"command": ".cursor/hooks/team-format.sh", "timeout": 7}
    ]
    expected_events = {"sessionStart", "preToolUse", "postToolUse", "preCompact", "stop"}
    assert expected_events <= config["hooks"].keys()
    for event in expected_events:
        managed = [
            item
            for item in config["hooks"][event]
            if "cursor_aqg_hook.py" in item.get("command", "")
        ]
        assert len(managed) == 1, (event, config["hooks"][event])
        command = managed[0]["command"]
        assert "cursor_aqg_hook.py" in command
        assert "bash" not in command.lower()
        assert "sessionstart_preflight.sh" not in command
        assert "agent-packs/claude-code/hooks" not in command.replace("\\", "/")
    assert config["hooks"]["preToolUse"][-1]["failClosed"] is True
    assert config["hooks"]["stop"][-1]["loop_limit"] == 1
    # The pre-existing hooks.json is backed up in the central store, not adjacent.
    assert not (cursor_root / "hooks.json.aqg-cursor.bak").exists()
    central = project / ".aqg-central"
    backed = list(central.rglob("hooks.json"))
    assert backed, "hooks.json should be captured in the central backup store"
    manifest = json.loads((backed[0].parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["client"] == "cursor"
    assert manifest["scope"].startswith("project-")
    assert Path(manifest["source_root"]) == cursor_root


def test_cursor_migrates_and_removes_legacy_inplace_backups(tmp_path: Path) -> None:
    project = tmp_path / "project"
    cursor_root = project / ".cursor"
    (cursor_root / "rules").mkdir(parents=True)
    # Both legacy formats present before apply: owned dir + adjacent .bak.
    legacy_owned = cursor_root / "aqg-backups"
    legacy_owned.mkdir()
    (legacy_owned / "aqg.mdc").write_text("old rule backup\n", encoding="utf-8")
    adjacent = cursor_root / "hooks.json.aqg-cursor.bak"
    adjacent.write_text("old hooks backup\n", encoding="utf-8")

    args = (
        "--apply",
        "--scope",
        "project",
        "--project-root",
        str(project),
        "--home",
        str(tmp_path / "home"),
        "--no-hooks",
    )
    proc = _run_cursor_installer(*args)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Legacy in-place backups are migrated into the central store, then deleted.
    assert not legacy_owned.exists()
    assert not adjacent.exists()
    central = project / ".aqg-central"
    assert list(central.rglob("aqg.mdc")), "legacy owned backup should be preserved centrally"
    assert list(
        central.rglob("hooks.json.aqg-cursor.bak")
    ), "legacy adjacent backup should be preserved centrally"


def test_project_apply_removes_legacy_aqg_claude_hooks_but_preserves_user_hooks(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    cursor_root = project / ".cursor"
    cursor_root.mkdir(parents=True)
    hooks_path = cursor_root / "hooks.json"
    user_hook = {"command": ".cursor/hooks/team-session-start.py", "timeout": 9}
    hooks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "sessionStart": [
                        user_hook,
                        {
                            "command": (
                                'if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi; '
                                'CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-}" bash '
                                '"$AQG_ROOT/agent-packs/claude-code/hooks/sessionstart_preflight.sh" '
                                '"${CLAUDE_PROJECT_DIR:-}" || true'
                            ),
                            "timeout": 45,
                        },
                    ],
                    "postToolUse": [
                        {
                            "command": (
                                'bash "$AQG_ROOT/agent-packs/claude-code/hooks/run_warn_only.sh"'
                            )
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    proc = _run_cursor_installer(
        "--apply",
        "--scope",
        "project",
        "--project-root",
        str(project),
        "--home",
        str(tmp_path / "home"),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    config = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert user_hook in config["hooks"]["sessionStart"]
    all_commands = [
        hook.get("command", "")
        for hooks in config["hooks"].values()
        for hook in hooks
        if isinstance(hook, dict)
    ]
    assert not any("sessionstart_preflight.sh" in command for command in all_commands)
    assert not any("agent-packs/claude-code/hooks" in command.replace("\\", "/") for command in all_commands)
    assert not any("install_aqg_hooks.py" in command for command in all_commands)
    assert any("cursor_aqg_hook.py" in command for command in all_commands)


def test_user_apply_installs_global_skills_and_hooks_without_inventing_user_rule_file(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    proc = _run_cursor_installer("--apply", "--scope", "user", "--home", str(home))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    skill = home / ".cursor" / "skills" / "aqg-code-construction"
    assert _is_link_install(skill)
    assert skill.resolve(strict=True) == (REPO / "skills" / "aqg-code-construction").resolve(strict=True)
    assert (home / ".cursor" / "hooks.json").is_file()
    assert not (home / ".cursor" / "rules").exists()
    assert not (home / ".cursor" / "AGENTS.md").exists()


def test_verify_is_installed_and_uninstall_round_trip_preserve_user_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    cursor_root = project / ".cursor"
    rules_root = cursor_root / "rules"
    rules_root.mkdir(parents=True)
    user_rule = rules_root / "team.mdc"
    user_rule.write_text("team-owned\n", encoding="utf-8")

    common = ("--scope", "project", "--project-root", str(project), "--home", str(tmp_path / "home"))
    assert _run_cursor_installer("--apply", *common).returncode == 0
    assert _run_cursor_installer("--verify", *common).returncode == 0
    assert _run_cursor_installer("--is-installed", *common).returncode == 0

    uninstall = _run_cursor_installer("--uninstall", *common)

    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert user_rule.read_text(encoding="utf-8") == "team-owned\n"
    assert not (rules_root / "aqg.mdc").exists()
    assert not any((cursor_root / "skills").glob("aqg-*"))
    assert not any((cursor_root / LINK_MARKER_DIR).glob("aqg-*.json"))
    assert not (cursor_root / "skills" / LEGACY_LINK_MARKER_DIR).exists()
    config = json.loads((cursor_root / "hooks.json").read_text(encoding="utf-8"))
    assert all(
        "cursor_aqg_hook.py" not in hook.get("command", "")
        for hooks in config.get("hooks", {}).values()
        for hook in hooks
    )
    assert _run_cursor_installer("--is-installed", *common).returncode == 1


def test_verify_accepts_legacy_link_markers_and_apply_migrates_them(tmp_path: Path) -> None:
    project = tmp_path / "project"
    cursor_root = project / ".cursor"
    skills_root = cursor_root / "skills"
    legacy_root = skills_root / LEGACY_LINK_MARKER_DIR
    legacy_root.mkdir(parents=True)
    expected_skills = _expected_skills()
    for skill_name in expected_skills:
        skill = skills_root / skill_name
        _create_link(REPO / "skills" / skill_name, skill)
        (legacy_root / f"{skill_name}.json").write_text(
            json.dumps(_legacy_marker_for(skill_name), indent=2) + "\n",
            encoding="utf-8",
        )

    common = ("--scope", "user", "--home", str(project), "--no-hooks")
    verify = _run_cursor_installer("--verify", *common)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert _run_cursor_installer("--is-installed", *common).returncode == 0

    apply = _run_cursor_installer("--apply", *common)

    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert sorted(path.name for path in (cursor_root / LINK_MARKER_DIR).glob("*.json")) == [
        f"{name}.json" for name in expected_skills
    ]
    assert not legacy_root.exists()
    assert not any(path.name.startswith(".aqg-") for path in skills_root.iterdir())
    assert _run_cursor_installer("--verify", *common).returncode == 0


def test_apply_warns_and_preserves_non_aqg_legacy_marker_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    legacy_root = project / ".cursor" / "skills" / LEGACY_LINK_MARKER_DIR
    legacy_root.mkdir(parents=True)
    foreign = legacy_root / "foreign.json"
    foreign.write_text('{"manager":"someone-else"}\n', encoding="utf-8")

    proc = _run_cursor_installer(
        "--apply", "--scope", "project", "--project-root", str(project), "--no-hooks"
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert foreign.is_file()
    assert legacy_root.is_dir()
    assert "WARNING" in proc.stderr


def test_uninstall_cleans_new_and_legacy_cursor_link_markers(tmp_path: Path) -> None:
    project = tmp_path / "project"
    common = ("--scope", "project", "--project-root", str(project), "--no-hooks")
    assert _run_cursor_installer("--apply", *common).returncode == 0
    cursor_root = project / ".cursor"
    skills_root = cursor_root / "skills"
    legacy_root = skills_root / LEGACY_LINK_MARKER_DIR
    legacy_root.mkdir(parents=True)
    legacy_marker = legacy_root / "aqg-retired.json"
    legacy_marker.write_text(
        json.dumps({**_legacy_marker_for("aqg-startup-preflight"), "skill": "aqg-retired"}),
        encoding="utf-8",
    )

    uninstall = _run_cursor_installer("--uninstall", *common)

    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert not any((cursor_root / LINK_MARKER_DIR).glob("aqg-*.json"))
    assert not legacy_root.exists()


def test_uninstall_preserves_unmanaged_aqg_directory_and_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    common = ("--scope", "project", "--project-root", str(project), "--no-hooks")
    assert _run_cursor_installer("--apply", *common).returncode == 0

    skills_root = project / ".cursor" / "skills"
    unmanaged_dir = skills_root / "aqg-user-owned"
    unmanaged_dir.mkdir()
    (unmanaged_dir / "SKILL.md").write_text("user-owned\n", encoding="utf-8")
    external = tmp_path / "external-skill"
    external.mkdir()
    unmanaged_link = skills_root / "aqg-linked-owned"
    _create_link(external, unmanaged_link)

    uninstall = _run_cursor_installer("--uninstall", *common)

    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert unmanaged_dir.is_dir()
    assert (unmanaged_dir / "SKILL.md").read_text(encoding="utf-8") == "user-owned\n"
    assert _is_link_install(unmanaged_link)
    assert unmanaged_link.resolve(strict=True) == external.resolve(strict=True)
    assert not (skills_root / "aqg-code-construction").exists()


def test_apply_refuses_unowned_aqg_collision_before_writing_other_items(tmp_path: Path) -> None:
    project = tmp_path / "project"
    collision = project / ".cursor" / "skills" / "aqg-code-construction"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("user-owned\n", encoding="utf-8")

    proc = _run_cursor_installer(
        "--apply", "--scope", "project", "--project-root", str(project), "--no-hooks"
    )

    assert proc.returncode == 1
    assert "refusing to overwrite" in proc.stderr
    assert (collision / "SKILL.md").read_text(encoding="utf-8") == "user-owned\n"
    assert not (project / ".cursor" / "rules" / "aqg.mdc").exists()


def test_apply_refuses_unknown_aqg_symlink_before_writing_other_items(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external-skill"
    external.mkdir()
    collision = project / ".cursor" / "skills" / "aqg-code-construction"
    collision.parent.mkdir(parents=True)
    _create_link(external, collision)

    proc = _run_cursor_installer(
        "--apply", "--scope", "project", "--project-root", str(project), "--no-hooks"
    )

    assert proc.returncode == 1
    assert "refusing to overwrite" in proc.stderr
    assert _is_link_install(collision)
    assert collision.resolve(strict=True) == external.resolve(strict=True)
    assert not (project / ".cursor" / "rules" / "aqg.mdc").exists()


def test_apply_refuses_symlinked_rules_directory_without_writing_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external_rules = tmp_path / "external-rules"
    external_rules.mkdir()
    cursor_root = project / ".cursor"
    cursor_root.mkdir(parents=True)
    try:
        (cursor_root / "rules").symlink_to(external_rules, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    proc = _run_cursor_installer(
        "--apply", "--scope", "project", "--project-root", str(project), "--no-hooks"
    )

    assert proc.returncode == 1
    assert "symlink" in proc.stderr.lower()
    assert not (external_rules / "aqg.mdc").exists()
    assert not (cursor_root / "skills").exists()


def test_apply_refuses_symlinked_hooks_without_writing_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external_hooks = tmp_path / "external-hooks.json"
    external_hooks.write_text('{"version": 1, "hooks": {}}\n', encoding="utf-8")
    cursor_root = project / ".cursor"
    cursor_root.mkdir(parents=True)
    try:
        (cursor_root / "hooks.json").symlink_to(external_hooks)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    proc = _run_cursor_installer(
        "--apply", "--scope", "project", "--project-root", str(project)
    )

    assert proc.returncode == 1
    assert "symlink" in proc.stderr.lower()
    assert external_hooks.read_text(encoding="utf-8") == '{"version": 1, "hooks": {}}\n'
    assert not (cursor_root / "skills").exists()
    assert not (cursor_root / "rules").exists()


def test_verify_detects_managed_rule_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    common = ("--scope", "project", "--project-root", str(project), "--no-hooks")
    assert _run_cursor_installer("--apply", *common).returncode == 0
    rule = project / ".cursor" / "rules" / "aqg.mdc"
    rule.write_text(rule.read_text(encoding="utf-8").replace(RULE_MARKER, "tampered"), encoding="utf-8")

    proc = _run_cursor_installer("--verify", *common)

    assert proc.returncode == 1
    assert "rule" in (proc.stdout + proc.stderr).lower()


def test_pretooluse_secret_scan_blocks_without_echoing_value() -> None:
    synthetic = "gh" + "p_" + ("a" * 32)
    proc = _run_adapter(
        "preToolUse",
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "example.py", "content": synthetic},
            "cwd": str(REPO),
        },
    )

    assert proc.returncode == 2
    response = json.loads(proc.stdout)
    assert response["permission"] == "deny"
    assert synthetic not in proc.stdout + proc.stderr
    assert "secret" in response["agent_message"].lower()


def test_posttooluse_file_edit_injects_relevant_aqg_reminders() -> None:
    proc = _run_adapter(
        "postToolUse",
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(REPO / "scripts" / "service.py"),
                "content": "def authenticate(user_input):\n    return user_input\n",
            },
            "cwd": str(REPO),
        },
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    context = json.loads(proc.stdout)["additional_context"]
    assert "aqg-code-construction" in context
    assert "aqg-security-review" in context


def test_posttooluse_audit_gate_matches_the_shared_policy() -> None:
    """Cursor must carry the same audit-trigger policy content as the other hosts.

    Cursor's adapter builds its own reminder text in Python rather than running
    the shell hook, so its wording is a second hand-maintained copy. Before this
    test it said only "and the audit-before-commit gate" — no exemption, no
    sensitivity list, no pointer — which is a third variant of the policy, the
    exact drift docs/policies/audit-trigger.md exists to end.
    """
    proc = _run_adapter(
        "postToolUse",
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(REPO / "scripts" / "service.py"),
                "content": "def add(a, b):\n    return a + b\n",
            },
            "cwd": str(REPO),
        },
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    context = json.loads(proc.stdout)["additional_context"]
    assert "SKIP if trivial" in context
    assert "ONCE before committing" in context
    assert "at most one per change" in context
    assert "audit-trigger.md" in context


def _policy_gate_a_tokens() -> tuple[str, ...]:
    """The ONE source for the sensitivity abbreviations: the policy document.

    Parsed rather than restated, so a hardcoded copy in this file cannot become a
    third hand-maintained list (which is what the first version of this test was,
    and why it was one-directional: adding a category to the policy left both
    adapters and the test in silent agreement about the old set).

    The regex itself moved to `scripts/aqg_policy_markers.py` once a third caller
    appeared; this stays as the local name the tests below already read by.
    """
    return gate_a_tokens(REPO)


def _emitted_shell_gate() -> str:
    """What the shell hook ACTUALLY prints, not what its source file contains."""
    proc = subprocess.run(
        [
            "bash",
            str(REPO / "agent-packs" / "claude-code" / "hooks"
                / "posttooluse_code_construction_reminder.sh"),
        ],
        input=json.dumps({"tool_input": {"file_path": "/repo/src/lib.py"}}),
        text=True,
        capture_output=True,
        env={**os.environ, "AQG_ROOT": str(REPO)},
        check=False,
    )
    return proc.stderr.split("[aqg audit-before-commit gate]", 1)[-1]


def _emitted_cursor_gate() -> str:
    proc = _run_adapter(
        "postToolUse",
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(REPO / "scripts" / "service.py"),
                "content": "def add(a, b):\n    return a + b\n",
            },
            "cwd": str(REPO),
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)["additional_context"]


def test_audit_gate_sensitivity_list_does_not_drift_between_adapters() -> None:
    """Every policy category must survive into the text each adapter EMITS.

    The first version of this test grepped the two SOURCE FILES against a
    hardcoded tuple, and gave false confidence in three separate ways, all
    demonstrated: deleting `auth` from Cursor's emitted string still passed,
    because the word also appears in this file's SECURITY_SIGNAL regex; adding a
    category to the policy was invisible, because the tuple was a third copy; and
    nothing checked the emitted output at all.

    Residual risk, stated rather than implied: this asserts the CATEGORY TOKENS
    only. The skip clause, frequency guard and pointer semantics are asserted
    per-adapter, so neither can lose them, but a wording difference between the
    two adapters would still pass.
    """
    tokens = _policy_gate_a_tokens()
    assert len(tokens) >= 8, f"suspiciously short token list: {tokens}"
    # The token-bounded check moved to scripts/aqg_policy_markers.py so the
    # negative fixture that proves it can fail runs this exact function rather
    # than a lookalike (aud_2PXuFzj3CUnZMS21 opus-f2).
    for carrier, emitted in (
        ("shell hook", _emitted_shell_gate()),
        ("cursor hook", _emitted_cursor_gate()),
    ):
        absent = missing_gate_a_tokens(emitted, REPO)
        assert not absent, f"{carrier} does not emit policy categories: {absent}"


def test_audit_gate_pointer_resolves_and_is_not_the_unresolved_marker() -> None:
    """In a normal checkout the pointer must resolve — no UNRESOLVED fallback."""
    context = _emitted_cursor_gate()
    match = re.search(r"Ladder \+ full list: (\S+)", context)
    assert match, f"no pointer emitted:\n{context}"
    emitted = Path(match.group(1))
    assert "UNRESOLVED" not in context
    assert emitted.is_absolute(), f"pointer must be host-resolvable, got {emitted}"
    assert emitted.is_file(), f"pointer does not resolve: {emitted}"


def test_audit_gate_pointer_degrades_to_marker_when_policy_is_missing() -> None:
    """The one branch the docstring calls safety-critical, previously untested.

    A fabricated absolute path is the same dangling-pointer failure this work
    exists to remove, only harder to notice.
    """
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), "postToolUse", "--aqg-root", "/nonexistent-aqg-root"],
        cwd=REPO,
        input=json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": str(REPO / "scripts" / "service.py"),
                    "content": "def add(a, b):\n    return a + b\n",
                },
                "cwd": str(REPO),
            }
        ),
        text=True,
        capture_output=True,
        env={**os.environ, "AQG_ROOT": "/nonexistent-aqg-root"},
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    context = json.loads(proc.stdout)["additional_context"]
    assert "UNRESOLVED" in context
    assert "/nonexistent-aqg-root/docs" not in context, "fabricated a path that does not exist"


def test_pretooluse_clean_call_defers_to_cursor_approval_policy() -> None:
    proc = _run_adapter(
        "preToolUse",
        {"tool_name": "Shell", "tool_input": {"command": "git status"}, "cwd": str(REPO)},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == {}


def test_pretooluse_accepts_utf8_bom() -> None:
    payload = {"tool_name": "Shell", "tool_input": {"command": "git status"}, "cwd": str(REPO)}
    proc = _run_adapter_bytes("preToolUse", b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))

    assert proc.returncode == 0, (proc.stdout + proc.stderr).decode("utf-8")
    assert json.loads(proc.stdout.decode("utf-8")) == {}


@pytest.mark.parametrize("payload", [b"\xef\xbb\xbf", b"\xef\xbb\xbf \r\n\t"])
def test_pretooluse_treats_bom_only_input_as_empty(payload: bytes) -> None:
    proc = _run_adapter_bytes("preToolUse", payload)

    assert proc.returncode == 0, (proc.stdout + proc.stderr).decode("utf-8")
    assert json.loads(proc.stdout.decode("utf-8")) == {}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\xef\xbb\xbf{not json", id="malformed-json"),
        pytest.param(b"\xef\xbb\xbf[]", id="non-object"),
        pytest.param(b"\xef\xbb\xbf" + b" " * 1_000_001, id="oversized"),
    ],
)
def test_pretooluse_bom_invalid_payloads_fail_closed(payload: bytes) -> None:
    proc = _run_adapter_bytes("preToolUse", payload)

    assert proc.returncode == 2
    assert json.loads(proc.stdout.decode("utf-8"))["permission"] == "deny"


def test_pretooluse_scanner_failure_is_fail_closed(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), "preToolUse", "--aqg-root", str(tmp_path)],
        cwd=REPO,
        input=json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": "x.py", "content": "value"}}
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 2
    assert json.loads(proc.stdout)["permission"] == "deny"
    assert "fail" in json.loads(proc.stdout)["agent_message"].lower()


def test_skill_validator_blocks_git_commit_with_global_option(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "skills" / "aqg-not-registered"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: aqg-not-registered\n---\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

    proc = _run_adapter(
        "preToolUse",
        {
            "tool_name": "Shell",
            "tool_input": {"command": "git -c user.name=CI commit -m test"},
            "cwd": str(repo),
        },
    )

    assert proc.returncode == 2
    assert json.loads(proc.stdout)["permission"] == "deny"
    assert "validator" in json.loads(proc.stdout)["agent_message"].lower()


def test_lifecycle_hooks_emit_documented_cursor_outputs_without_git_writes(tmp_path: Path) -> None:
    project = tmp_path / "plain-project"
    project.mkdir()
    common = {
        "conversation_id": "cursor-test-session",
        "workspace_roots": [str(project)],
    }

    start = _run_adapter("sessionStart", common)
    compact = _run_adapter("preCompact", common)
    first_stop = _run_adapter("stop", common)
    repeated_stop = _run_adapter("stop", {**common, "loop_count": 1})

    assert start.returncode == 0
    assert "additional_context" in json.loads(start.stdout)
    assert compact.returncode == 0
    assert "user_message" in json.loads(compact.stdout)
    assert first_stop.returncode == 0
    assert "followup_message" in json.loads(first_stop.stdout)
    assert json.loads(repeated_stop.stdout) == {}
    assert not (project / ".git").exists()


def test_sessionstart_empty_state_draft_exits_zero_without_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)
    proc = _run_adapter(
        "sessionStart",
        {
            "conversation_id": "empty-state-draft",
            "session_id": "empty-state-draft",
            "hook_event_name": "sessionStart",
            "cursor_version": "3.13.10",
            "workspace_roots": [],
            "transcript_path": None,
        },
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == {}
    assert "preflight" not in proc.stdout.lower()
    assert proc.stderr == ""


def test_malformed_hooks_fail_before_any_managed_file_is_written(tmp_path: Path) -> None:
    project = tmp_path / "project"
    cursor_root = project / ".cursor"
    cursor_root.mkdir(parents=True)
    (cursor_root / "hooks.json").write_text("{broken", encoding="utf-8")

    proc = _run_cursor_installer(
        "--apply", "--scope", "project", "--project-root", str(project)
    )

    assert proc.returncode == 1
    assert "invalid Cursor hooks JSON" in proc.stderr
    assert not (cursor_root / "skills").exists()
    assert not (cursor_root / "rules").exists()
