"""Behavior contracts for WorkBuddy/CodeBuddy/Trae/Kimi/Qoder work-client support."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import install_aqg_work_clients as work_installer


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install_aqg_work_clients.py"
MANAGED_MARKER = ".aqg-work-client-managed.json"
LINK_MARKER_DIR = "managed-links"
RULE_MARKER = "AQG WORK CLIENT MANAGED RULE"
REPORT_MARKER = "AQG WORK CLIENT SUPPORT REPORT"
LEGACY_MCP_DESCRIPTION = "AQG diagnostic connector placeholder; lifecycle gates remain hook/rule driven."
LEGACY_MCP_PROFILES = (
    ("codebuddy", ".codebuddy"),
    ("workbuddy-ai", ".workbuddy-ai"),
    ("kimi-code", ".kimi-code"),
    ("qoderwork", ".qoderwork"),
    ("qoderwake", ".qoderwake"),
)


def _expected_skills() -> list[str]:
    return sorted(
        path.name for path in (REPO / "skills").glob("aqg-*") if (path / "SKILL.md").is_file()
    )


@pytest.fixture(autouse=True)
def _isolate_central_backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Route the central backup store into this test's tmp tree for both in-process
    # main() calls and subprocess installer runs (children inherit os.environ).
    monkeypatch.setenv("AQG_BACKUP_DIR", str(tmp_path / ".aqg-central"))
    monkeypatch.delenv("AQG_ROOT", raising=False)
    monkeypatch.delenv("AQG_BACKUP_KEEP", raising=False)


def _run_installer(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )


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


def _legacy_mcp_entry(script: str) -> dict[str, object]:
    return {
        "command": "python3",
        "args": [script, "--no-cli"],
        "description": LEGACY_MCP_DESCRIPTION,
    }


def test_codebuddy_full_profile_installs_skills_rules_and_hooks_without_pseudo_mcp(tmp_path: Path) -> None:
    home = tmp_path / "home"

    proc = _run_installer(
        "--client",
        "codebuddy",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "support_level: full" in proc.stdout
    root = home / ".codebuddy"
    expected_skills = _expected_skills()
    assert sorted(path.name for path in (root / "skills").glob("aqg-*")) == expected_skills
    assert all(_is_link_install(root / "skills" / name) for name in expected_skills)
    assert sorted(path.name for path in (root / LINK_MARKER_DIR).glob("*.json")) == [
        f"{name}.json" for name in expected_skills
    ]
    assert RULE_MARKER in (root / "rules" / "aqg.md").read_text(encoding="utf-8")
    assert not (root / "mcp.json").exists()
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert {
        "SessionStart",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PreCompact",
        "Stop",
        "UserPromptSubmit",
    } <= set(settings["hooks"])
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Shell|Bash|Write|Edit|MultiEdit"
    assert "hooks" in settings["hooks"]["PreToolUse"][0]

    assert _run_installer(
        "--client",
        "codebuddy",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--verify",
    ).returncode == 0
    assert _run_installer(
        "--client",
        "codebuddy",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--is-installed",
    ).returncode == 0


def test_kimi_code_partial_profile_reports_fail_open_hook_degradation(tmp_path: Path) -> None:
    home = tmp_path / "home"

    proc = _run_installer(
        "--client",
        "kimi-code",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "support_level: partial" in proc.stdout
    assert "fail-open" in proc.stdout
    root = home / ".kimi-code"
    assert (root / "config.toml").is_file()
    assert "PreToolUse" in (root / "config.toml").read_text(encoding="utf-8")
    assert not (root / "mcp.json").exists()


@pytest.mark.parametrize(
    ("client", "root_dir", "expect_skills", "expect_mcp", "expect_hooks"),
    [
        ("workbuddy", ".workbuddy", True, False, False),
        ("qoderwork", ".qoderwork", True, False, True),
        ("qoderwake", ".qoderwake", True, False, False),
    ],
)
def test_degraded_work_profiles_install_only_documented_surfaces(
    tmp_path: Path,
    client: str,
    root_dir: str,
    expect_skills: bool,
    expect_mcp: bool,
    expect_hooks: bool,
) -> None:
    home = tmp_path / "home"

    proc = _run_installer(
        "--client",
        client,
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    root = home / root_dir
    assert REPORT_MARKER in (root / "aqg-support-report.md").read_text(encoding="utf-8")
    assert (root / "skills").exists() is expect_skills
    assert (root / "mcp.json").exists() is expect_mcp
    assert (root / "settings.json").exists() is expect_hooks
    assert not (root / "config.toml").exists()
    if client == "qoderwork":
        settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        assert set(settings["hooks"]) == {"PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit"}


def test_retired_trae_work_profile_redirects_and_writes_nothing(tmp_path: Path) -> None:
    # R24-06/R24-07: `trae-work` moved to the agent-client adapter because it
    # shares `~/.trae/skills`. The old entry point must refuse and point at the
    # new one instead of silently writing a `.trae-work` root nothing reads.
    home = tmp_path / "home"

    proc = _run_installer(
        "--client",
        "trae-work",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 2
    assert "install_aqg_agent_clients.py" in proc.stderr
    assert "--client trae-work" in proc.stderr
    assert not home.exists() or not list(home.glob(".trae-work*"))


def test_kimi_work_installs_aqg_skills_to_explicit_daimon_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skills_root = tmp_path / "KimiData" / "daimon-share" / "daimon" / "skills"

    proc = _run_installer(
        "--client",
        "kimi-work",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--skills-root",
        str(skills_root),
        "--apply",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "client_id=kimi-work" in proc.stdout
    assert f"resolved_skills_root={skills_root.resolve()}" in proc.stdout
    assert "discovery_source=explicit --skills-root" in proc.stdout
    assert "requested_mode=link" in proc.stdout
    assert "effective_mode=link" in proc.stdout
    assert "support_level: partial" in proc.stdout
    root = home / ".kimi-work"
    assert REPORT_MARKER in (root / "aqg-support-report.md").read_text(encoding="utf-8")
    assert "product=Kimi Work Desktop / Kimi Desktop" in (root / "aqg-support-report.md").read_text(encoding="utf-8")
    expected_skills = _expected_skills()
    assert sorted(path.name for path in skills_root.glob("aqg-*")) == expected_skills
    assert all((skills_root / name / "SKILL.md").is_file() for name in expected_skills)
    marker = json.loads(
        (skills_root.parent / LINK_MARKER_DIR / f"{expected_skills[0]}.json").read_text(encoding="utf-8")
    )
    assert marker["manager"] == "AQG"
    assert marker["target_client"] == "kimi-work"
    assert marker["requested_mode"] == "link"
    assert marker["effective_mode"] == "link"
    assert marker["resolved_skills_root"] == str(skills_root.resolve())
    assert not (root / "mcp.json").exists()


def test_kimi_work_skills_root_env_overrides(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "env-root"
    root.mkdir()
    monkeypatch.setenv("KIMI_WORK_SKILLS_ROOT", str(root))

    resolved = work_installer.resolve_kimi_work_skills_root(home=tmp_path, include_fallback=False)

    assert resolved is not None
    assert resolved.skills_root == root.resolve()
    assert resolved.discovery_source == "env KIMI_WORK_SKILLS_ROOT"


def test_kimi_work_skills_root_compat_env_alias(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "desktop-env-root"
    root.mkdir()
    monkeypatch.delenv("KIMI_WORK_SKILLS_ROOT", raising=False)
    monkeypatch.setenv("KIMI_DESKTOP_SKILLS_ROOT", str(root))

    resolved = work_installer.resolve_kimi_work_skills_root(home=tmp_path, include_fallback=False)

    assert resolved is not None
    assert resolved.skills_root == root.resolve()
    assert resolved.discovery_source == "env KIMI_DESKTOP_SKILLS_ROOT"


# Coverage gap, stated so it is not silently green: CI runs Linux only, so this
# branch is currently exercised nowhere. Closing it means teaching
# _skills_root_from_kimi_command_line to split with PureWindowsPath instead of
# Path, which is a product change and deliberately out of scope here.
@pytest.mark.skipif(os.name != "nt", reason="Windows process command-line path parsing")
def test_kimi_work_skills_root_from_node_command_line(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "KimiData"
    skills_root = base / "daimon-share" / "daimon" / "skills"
    skills_root.parent.mkdir(parents=True)
    command_line = (
        f'node.exe "{base}\\daimon-bundle\\app\\daimon\\dist\\src\\runner\\cli.js" '
        f'start --config "{base}\\daimon-share\\daimon\\runtime\\openclaw-empty.json" --control'
    )
    monkeypatch.setattr(work_installer, "_kimi_process_command_lines", lambda: (command_line,))

    resolved = work_installer.resolve_kimi_work_skills_root(home=tmp_path, include_fallback=False)

    assert resolved is not None
    assert resolved.skills_root == skills_root.resolve()
    assert resolved.discovery_source == "running Kimi/Daimon process --config"


def test_kimi_work_skills_root_from_main_log_share_dir(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    share = tmp_path / "KimiData" / "daimon-share"
    skills_root = share / "daimon" / "skills"
    skills_root.parent.mkdir(parents=True)
    log = appdata / "kimi-desktop" / "logs" / "main.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            [
                "install-drive seed: new user on d: - shareDir seeded to C:\\old\\daimon-share",
                f"install-drive seed: new user on d: - shareDir seeded to {share}",
                f"released 1 built-in skill(s) ok -> {skills_root}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))

    resolved = work_installer.resolve_kimi_work_skills_root(home=home, include_fallback=False)

    assert resolved is not None
    assert resolved.skills_root == skills_root.resolve()
    assert resolved.discovery_source == "Kimi Desktop log released skills"


def test_kimi_work_skills_root_from_rehome_marker(tmp_path: Path, monkeypatch) -> None:
    share = tmp_path / "KimiData" / "daimon-share"
    marker = share / "daimon" / ".rehomed"
    marker.parent.mkdir(parents=True)
    marker.write_text(str(share), encoding="utf-8")
    monkeypatch.setattr(work_installer, "_kimi_rehome_marker_paths", lambda home: (marker,))

    resolved = work_installer.resolve_kimi_work_skills_root(home=tmp_path, include_fallback=False)

    assert resolved is not None
    assert resolved.skills_root == (share / "daimon" / "skills").resolve()
    assert resolved.discovery_source == "Kimi Daimon .rehomed marker"


def test_kimi_work_skills_root_from_install_drive_candidate(tmp_path: Path, monkeypatch) -> None:
    exe = tmp_path / "Kimi" / "Kimi.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    share = tmp_path / "KimiData" / "daimon-share"
    share.mkdir(parents=True)
    monkeypatch.setattr(work_installer, "_kimi_exe_paths_from_registry_and_shortcuts", lambda home: (exe,))
    monkeypatch.setattr(work_installer, "_kimi_install_drive_share_candidates", lambda paths: (share,))

    resolved = work_installer.resolve_kimi_work_skills_root(home=tmp_path, include_fallback=False)

    assert resolved is not None
    assert resolved.skills_root == (share / "daimon" / "skills").resolve()
    assert resolved.discovery_source == "Kimi.exe install drive candidate"


def test_kimi_work_copy_mode_apply_verify_uninstall(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skills_root = tmp_path / "skills"
    common = (
        "--client",
        "kimi-work",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--skills-root",
        str(skills_root),
        "--mode",
        "copy",
    )

    apply = _run_installer(*common, "--apply")
    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert "requested_mode=copy" in apply.stdout
    assert "effective_mode=copy" in apply.stdout
    assert all(not _is_link_install(path) for path in skills_root.glob("aqg-*"))

    verify = _run_installer(*common, "--verify")
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert "effective_mode_summary=copy:" in verify.stdout

    uninstall = _run_installer(*common, "--uninstall")
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert not any(skills_root.glob("aqg-*"))


def test_kimi_work_falls_back_to_copy_when_symlink_creation_fails(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    skills_root = tmp_path / "skills"
    common = (
        "--client",
        "kimi-work",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--skills-root",
        str(skills_root),
    )
    monkeypatch.setattr(work_installer, "_create_link", lambda source, target: (_ for _ in ()).throw(OSError("link denied")))

    rc = work_installer.main([*common, "--apply"])

    assert rc == 0
    markers = [json.loads((path / MANAGED_MARKER).read_text(encoding="utf-8")) for path in skills_root.glob("aqg-*")]
    assert markers
    assert {marker["requested_mode"] for marker in markers} == {"link"}
    assert {marker["effective_mode"] for marker in markers} == {"copy"}
    assert all("link denied" in marker["fallback_reason"] for marker in markers)


def test_kimi_work_strict_link_fails_closed_when_symlink_creation_fails(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    skills_root = tmp_path / "skills"
    monkeypatch.setattr(work_installer, "_create_link", lambda source, target: (_ for _ in ()).throw(OSError("link denied")))

    rc = work_installer.main(
        [
            "--client",
            "kimi-work",
            "--scope",
            "user",
            "--home",
            str(home),
            "--aqg-root",
            str(REPO),
            "--skills-root",
            str(skills_root),
            "--mode",
            "link",
            "--strict-link",
            "--apply",
        ]
    )

    assert rc == 1
    assert not any(skills_root.glob("aqg-*"))


def test_kimi_work_verify_accepts_copy_install_when_requested_mode_is_link(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    skills_root = tmp_path / "skills"
    monkeypatch.setattr(work_installer, "_create_link", lambda source, target: (_ for _ in ()).throw(OSError("link denied")))
    common = [
        "--client",
        "kimi-work",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--skills-root",
        str(skills_root),
    ]

    assert work_installer.main([*common, "--apply"]) == 0

    verify = _run_installer(*common, "--verify")
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert "effective_mode_summary=copy:" in verify.stdout


def test_kimi_work_refuses_unmanaged_aqg_skill_collision(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    collision = skills_root / "aqg-code-construction"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("user-owned\n", encoding="utf-8")

    proc = _run_installer(
        "--client",
        "kimi-work",
        "--scope",
        "user",
        "--home",
        str(tmp_path / "home"),
        "--aqg-root",
        str(REPO),
        "--skills-root",
        str(skills_root),
        "--apply",
    )

    assert proc.returncode == 1
    assert "refusing to overwrite existing skill" in proc.stderr


def test_kimi_work_output_redacts_secret_shaped_values(tmp_path: Path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("KIMI_WORK_SKILLS_ROOT", str(skills_root))
    monkeypatch.setenv("api_key", "SHOULD_NOT_PRINT")
    monkeypatch.setenv("accessToken", "SHOULD_NOT_PRINT")
    monkeypatch.setenv("refreshToken", "SHOULD_NOT_PRINT")
    monkeypatch.setenv("Authorization", "SHOULD_NOT_PRINT")

    proc = _run_installer(
        "--client",
        "kimi-work",
        "--scope",
        "user",
        "--home",
        str(tmp_path / "home"),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "SHOULD_NOT_PRINT" not in output
    assert "api_key" not in output
    assert "accessToken" not in output
    assert "refreshToken" not in output
    assert "Authorization" not in output


def test_uninstall_preserves_user_files_and_removes_managed_assets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".codebuddy"
    root.mkdir(parents=True)
    user_file = root / "team.txt"
    user_file.write_text("keep\n", encoding="utf-8")

    common = ("--client", "codebuddy", "--scope", "user", "--home", str(home), "--aqg-root", str(REPO))
    assert _run_installer("--apply", *common).returncode == 0
    uninstall = _run_installer("--uninstall", *common)

    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert user_file.read_text(encoding="utf-8") == "keep\n"
    assert not (root / "rules" / "aqg.md").exists()
    assert not any((root / "skills").glob("aqg-*"))
    assert _run_installer("--is-installed", *common).returncode == 1


def test_uninstall_preserves_user_settings_and_mcp_entries(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".codebuddy"
    root.mkdir(parents=True)
    (root / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "User", "hooks": [{"type": "command", "command": "echo user"}]}]}}),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps({"mcpServers": {"user-server": {"command": "node", "args": ["server.js"]}}}),
        encoding="utf-8",
    )

    common = ("--client", "codebuddy", "--scope", "user", "--home", str(home), "--aqg-root", str(REPO))
    assert _run_installer("--apply", *common).returncode == 0
    assert _run_installer("--uninstall", *common).returncode == 0

    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert settings == {
        "hooks": {
            "PreToolUse": [
                {"matcher": "User", "hooks": [{"type": "command", "command": "echo user"}]}
            ]
        }
    }
    mcp = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    assert mcp == {"mcpServers": {"user-server": {"command": "node", "args": ["server.js"]}}}


@pytest.mark.parametrize(("client", "root_dir"), LEGACY_MCP_PROFILES)
def test_fresh_apply_never_registers_aqg_support_mcp(
    tmp_path: Path, client: str, root_dir: str
) -> None:
    home = tmp_path / "home"
    common = ("--client", client, "--scope", "user", "--home", str(home), "--aqg-root", str(REPO))

    apply = _run_installer("--apply", *common)

    assert apply.returncode == 0, apply.stdout + apply.stderr
    root = home / root_dir
    assert not (root / "mcp.json").exists()
    report = (root / "aqg-support-report.md").read_text(encoding="utf-8")
    assert "MCP/connectors: unsupported" in report
    assert _run_installer("--verify", *common).returncode == 0


@pytest.mark.parametrize(("client", "root_dir"), LEGACY_MCP_PROFILES)
@pytest.mark.parametrize(
    "doctor_path",
    (
        "/Users/example/.deeppattern/agent-quality-gates/scripts/aqg_doctor.py",
        str(REPO / "scripts" / "aqg_doctor.py"),
        r"C:\Users\example\.deeppattern\agent-quality-gates\scripts\aqg_doctor.py",
    ),
)
def test_apply_removes_only_exact_historical_aqg_support_entry(
    tmp_path: Path, client: str, root_dir: str, doctor_path: str
) -> None:
    home = tmp_path / "home"
    root = home / root_dir
    root.mkdir(parents=True)
    expected = {
        "schemaVersion": 7,
        "mcpServers": {
            "decision-engine": {"command": "de", "args": ["serve"]},
            "user-server": {"command": "node", "args": ["server.js"]},
        },
    }
    config = json.loads(json.dumps(expected))
    config["mcpServers"]["aqg-support"] = _legacy_mcp_entry(doctor_path)
    (root / "mcp.json").write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
    common = ("--client", client, "--scope", "user", "--home", str(home), "--aqg-root", str(REPO))

    apply = _run_installer("--apply", *common)

    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert json.loads((root / "mcp.json").read_text(encoding="utf-8")) == expected
    assert _run_installer("--verify", *common).returncode == 0


def test_apply_removes_managed_only_mcp_file_and_repeat_apply_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".workbuddy-ai"
    root.mkdir(parents=True)
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "aqg-support": _legacy_mcp_entry(
                        "/old/agent-quality-gates/scripts/aqg_doctor.py"
                    )
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    common = (
        "--client",
        "workbuddy-ai",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
    )

    assert _run_installer("--apply", *common).returncode == 0
    assert not (root / "mcp.json").exists()
    repeat = _run_installer("--apply", *common)
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert "already current" in repeat.stdout


def test_verify_rejects_exact_historical_aqg_support_residue(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".workbuddy-ai"
    common = (
        "--client",
        "workbuddy-ai",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
    )
    assert _run_installer("--apply", *common).returncode == 0
    mcp_path = root / "mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "aqg-support": _legacy_mcp_entry(
                        str(REPO / "scripts" / "aqg_doctor.py")
                    )
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    verify = _run_installer("--verify", *common)

    assert verify.returncode == 1
    assert "MCP drift: historical aqg-support residue" in verify.stderr


def test_uninstall_removes_historical_aqg_support_and_preserves_other_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".workbuddy-ai"
    root.mkdir(parents=True)
    mcp_path = root / "mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "schemaVersion": 7,
                "mcpServers": {
                    "aqg-support": _legacy_mcp_entry(
                        "/old/agent-quality-gates/scripts/aqg_doctor.py"
                    ),
                    "decision-engine": {"command": "de", "args": ["serve"]},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    common = (
        "--client",
        "workbuddy-ai",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
    )

    uninstall = _run_installer("--uninstall", *common)

    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert json.loads(mcp_path.read_text(encoding="utf-8")) == {
        "schemaVersion": 7,
        "mcpServers": {"decision-engine": {"command": "de", "args": ["serve"]}},
    }
    repeat = _run_installer("--uninstall", *common)
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert "not installed" in repeat.stdout
    assert json.loads(mcp_path.read_text(encoding="utf-8")) == {
        "schemaVersion": 7,
        "mcpServers": {"decision-engine": {"command": "de", "args": ["serve"]}},
    }


@pytest.mark.parametrize(("client", "root_dir"), LEGACY_MCP_PROFILES)
def test_foreign_same_name_mcp_is_byte_preserved_and_never_claims_installation(
    tmp_path: Path, client: str, root_dir: str
) -> None:
    home = tmp_path / "home"
    root = home / root_dir
    root.mkdir(parents=True)
    mcp_path = root / "mcp.json"
    foreign_bytes = (
        b'{\n  "owner": "user",\n  "mcpServers": {\n'
        b'    "aqg-support": {"command": "custom-aqg", "args": ["serve"], "env": {"KEEP": "yes"}},\n'
        b'    "decision-engine": {"command": "de", "args": ["serve"]}\n  }\n}\n'
    )
    mcp_path.write_bytes(foreign_bytes)
    common = ("--client", client, "--scope", "user", "--home", str(home), "--aqg-root", str(REPO))

    assert _run_installer("--is-installed", *common).returncode == 1
    apply = _run_installer("--apply", *common)
    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert mcp_path.read_bytes() == foreign_bytes
    assert _run_installer("--verify", *common).returncode == 0
    assert mcp_path.read_bytes() == foreign_bytes
    assert _run_installer("--is-installed", *common).returncode == 0
    uninstall = _run_installer("--uninstall", *common)
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert mcp_path.read_bytes() == foreign_bytes
    assert _run_installer("--is-installed", *common).returncode == 1


@pytest.mark.parametrize(
    "foreign_entry",
    (
        {
            "command": "python",
            "args": ["/old/scripts/aqg_doctor.py", "--no-cli"],
            "description": LEGACY_MCP_DESCRIPTION,
        },
        {
            "command": "python3",
            "args": ["scripts/aqg_doctor.py", "--no-cli"],
            "description": LEGACY_MCP_DESCRIPTION,
        },
        {
            "command": "python3",
            "args": ["/old/scripts/other.py", "--no-cli"],
            "description": LEGACY_MCP_DESCRIPTION,
        },
        {
            "command": "python3",
            "args": ["/old/scripts/aqg_doctor.py", "--no-cli"],
            "description": "user-managed diagnostic server",
        },
        {
            "command": "python3",
            "args": ["/old/scripts/aqg_doctor.py", "--no-cli"],
            "description": LEGACY_MCP_DESCRIPTION,
            "disabled": False,
        },
    ),
)
def test_uninstall_preserves_near_match_foreign_aqg_support_bytes(
    tmp_path: Path, foreign_entry: dict[str, object]
) -> None:
    home = tmp_path / "home"
    root = home / ".workbuddy-ai"
    root.mkdir(parents=True)
    mcp_path = root / "mcp.json"
    original = (json.dumps({"mcpServers": {"aqg-support": foreign_entry}}, indent=3) + "\n").encode()
    mcp_path.write_bytes(original)
    common = (
        "--client",
        "workbuddy-ai",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
    )

    uninstall = _run_installer("--uninstall", *common)

    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert mcp_path.read_bytes() == original
    assert _run_installer("--is-installed", *common).returncode == 1


@pytest.mark.parametrize("malformed_servers", ([], None, "not-an-object"))
def test_apply_rejects_malformed_mcp_servers_before_other_writes(
    tmp_path: Path, malformed_servers: object
) -> None:
    home = tmp_path / "home"
    root = home / ".workbuddy-ai"
    root.mkdir(parents=True)
    mcp_path = root / "mcp.json"
    original = (json.dumps({"mcpServers": malformed_servers}) + "\n").encode()
    mcp_path.write_bytes(original)
    adjacent = root / "settings.json.aqg-work-client.bak"
    adjacent.write_bytes(b"legacy backup\n")

    proc = _run_installer(
        "--client",
        "workbuddy-ai",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 1
    assert "mcpServers must be a JSON object" in proc.stderr
    assert mcp_path.read_bytes() == original
    assert adjacent.read_bytes() == b"legacy backup\n"
    assert not (tmp_path / ".aqg-central").exists()
    assert not (root / "aqg-support-report.md").exists()
    assert not (root / "rules" / "aqg.md").exists()
    assert not list((root / "skills").glob("aqg-*"))


@pytest.mark.parametrize("malformed_servers", ([], None, "not-an-object"))
def test_uninstall_rejects_malformed_mcp_servers_before_removing_managed_assets(
    tmp_path: Path, malformed_servers: object
) -> None:
    home = tmp_path / "home"
    root = home / ".workbuddy-ai"
    common = (
        "--client",
        "workbuddy-ai",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
    )
    assert _run_installer("--apply", *common).returncode == 0
    report = root / "aqg-support-report.md"
    skill_names = sorted(path.name for path in (root / "skills").glob("aqg-*"))
    (root / "mcp.json").write_text(
        json.dumps({"mcpServers": malformed_servers}) + "\n", encoding="utf-8"
    )
    adjacent = root / "settings.json.aqg-work-client.bak"
    adjacent.write_bytes(b"legacy backup\n")
    central = tmp_path / ".aqg-central"
    central_before = {
        path.relative_to(central): path.read_bytes()
        for path in central.rglob("*")
        if path.is_file()
    }

    uninstall = _run_installer("--uninstall", *common)

    assert uninstall.returncode == 1
    assert "mcpServers must be a JSON object" in uninstall.stderr
    assert adjacent.read_bytes() == b"legacy backup\n"
    assert {
        path.relative_to(central): path.read_bytes()
        for path in central.rglob("*")
        if path.is_file()
    } == central_before
    assert report.is_file()
    assert sorted(path.name for path in (root / "skills").glob("aqg-*")) == skill_names


def test_apply_refuses_unmanaged_skill_collision_before_writing_other_assets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".codebuddy"
    collision = root / "skills" / "aqg-code-construction"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("user-owned\n", encoding="utf-8")

    proc = _run_installer(
        "--client",
        "codebuddy",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 1
    assert "refusing to overwrite" in proc.stderr
    assert not (root / "rules" / "aqg.md").exists()
    assert not (root / "settings.json").exists()


def test_apply_refuses_symlinked_settings_without_writing_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".codebuddy"
    root.mkdir(parents=True)
    external = tmp_path / "external-settings.json"
    external.write_text('{"hooks": {}}\n', encoding="utf-8")
    try:
        (root / "settings.json").symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    proc = _run_installer(
        "--client",
        "codebuddy",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 1
    assert "symlink" in proc.stderr.lower()
    assert external.read_text(encoding="utf-8") == '{"hooks": {}}\n'
    assert not (root / "rules" / "aqg.md").exists()


def test_work_client_migrates_and_removes_legacy_inplace_backups(tmp_path: Path) -> None:
    profile = work_installer.PROFILES["codebuddy"]
    home = tmp_path / "home"
    client_root = home / profile.user_dir
    legacy = client_root / "aqg-backups"
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text("old owned backup\n", encoding="utf-8")
    adjacent = client_root / "settings.json.aqg-work-client.bak"
    adjacent.write_text("old adjacent backup\n", encoding="utf-8")

    common = ["--client", "codebuddy", "--scope", "user", "--home", str(home), "--aqg-root", str(REPO)]
    assert work_installer.main([*common, "--apply"]) == 0

    # Legacy in-place backups are migrated into the central store, then deleted.
    assert not legacy.exists()
    assert not adjacent.exists()
    central = tmp_path / ".aqg-central"
    assert list(central.rglob("settings.json")), "legacy owned backup should be preserved centrally"
    assert list(
        central.rglob("settings.json.aqg-work-client.bak")
    ), "legacy adjacent backup should be preserved centrally"


# --------------------------------------------------------------------------
# AQG-026 WorkBuddy AI independent adaptation (R5-R7)
# --------------------------------------------------------------------------


def test_workbuddy_ai_full_profile_installs_skills_rules_and_hooks_independently(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    proc = _run_installer(
        "--client",
        "workbuddy-ai",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "support_level: full" in proc.stdout
    root = home / ".workbuddy-ai"
    expected_skills = _expected_skills()
    assert sorted(path.name for path in (root / "skills").glob("aqg-*")) == expected_skills
    assert RULE_MARKER in (root / "rules" / "aqg.md").read_text(encoding="utf-8")
    assert not (root / "mcp.json").exists()
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert {
        "SessionStart",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PreCompact",
        "Stop",
        "UserPromptSubmit",
    } <= set(settings["hooks"])

    # R5/R6: independent identity -- full capability lives only under its own
    # root, never merged into the sibling workbuddy/codebuddy config roots.
    assert not (home / ".workbuddy").exists()
    assert not (home / ".codebuddy").exists()

    assert _run_installer(
        "--client",
        "workbuddy-ai",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--verify",
    ).returncode == 0
    assert _run_installer(
        "--client",
        "workbuddy-ai",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--is-installed",
    ).returncode == 0


def test_workbuddy_ai_repeat_apply_is_idempotent_and_uninstall_preserves_third_party_and_siblings(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    sibling_workbuddy = home / ".workbuddy"
    sibling_workbuddy.mkdir(parents=True)
    (sibling_workbuddy / "keep.txt").write_text("legacy workbuddy content\n", encoding="utf-8")
    sibling_codebuddy = home / ".codebuddy"
    sibling_codebuddy.mkdir(parents=True)
    (sibling_codebuddy / "keep.txt").write_text("codebuddy content\n", encoding="utf-8")

    common = ("--client", "workbuddy-ai", "--scope", "user", "--home", str(home), "--aqg-root", str(REPO))
    first = _run_installer("--apply", *common)
    assert first.returncode == 0, first.stdout + first.stderr
    root = home / ".workbuddy-ai"
    (root / "team.txt").write_text("keep\n", encoding="utf-8")

    second = _run_installer("--apply", *common)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already current" in second.stdout

    uninstall = _run_installer("--uninstall", *common)
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert (root / "team.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (root / "rules" / "aqg.md").exists()
    assert not any((root / "skills").glob("aqg-*"))
    assert _run_installer("--is-installed", *common).returncode == 1

    # R7: sibling roots must remain completely untouched by workbuddy-ai's
    # apply -> repeat-apply -> uninstall cycle.
    assert (sibling_workbuddy / "keep.txt").read_text(encoding="utf-8") == "legacy workbuddy content\n"
    assert (sibling_codebuddy / "keep.txt").read_text(encoding="utf-8") == "codebuddy content\n"


def test_kimi_work_uninstall_backs_up_external_skills_to_central_store(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    skills_root = tmp_path / "skills"  # deliberately OUTSIDE client_root (home/.kimi-work)
    # Force copy install so uninstall backs up real directories (not links).
    monkeypatch.setattr(
        work_installer,
        "_create_link",
        lambda source, target: (_ for _ in ()).throw(OSError("link denied")),
    )
    common = [
        "--client",
        "kimi-work",
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--skills-root",
        str(skills_root),
    ]
    assert work_installer.main([*common, "--apply"]) == 0
    assert any(skills_root.glob("aqg-*")), "copy-mode skills should be installed under the external root"

    assert work_installer.main([*common, "--uninstall"]) == 0
    assert not any(skills_root.glob("aqg-*"))

    central = tmp_path / ".aqg-central"
    manifests = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in central.glob("*/*/*/manifest.json")
    ]
    assert manifests, "central store should record a manifest"
    source_roots = {manifest["source_root"] for manifest in manifests}
    # The external skills root gets its own session so relpath mirroring stays valid.
    assert str(skills_root.resolve()) in source_roots, source_roots
    assert list((central / "kimi-work").rglob("SKILL.md")), "external skill dirs captured centrally"


def _fake_aqg_root(tmp_path: Path, *, roster: tuple[str, ...], present: tuple[str, ...]) -> Path:
    """A minimal stand-in for REPO_ROOT: a skills.list roster manifest plus the
    subset of skills/<name>/SKILL.md dirs that are actually packaged."""
    root = tmp_path / "fake-aqg-root"
    (root / "skills").mkdir(parents=True)
    # Real rule template: the roster is then the ONLY thing wrong with this root,
    # so a fail-closed apply cannot pass for an unrelated missing-file reason.
    rule_template = REPO / "examples" / "aqg-codex-agents.example.md"
    (root / "examples").mkdir()
    shutil.copy2(rule_template, root / "examples" / rule_template.name)
    (root / "skills.list").write_text(
        "# roster manifest fixture\n\n" + "".join(f"{name}\n" for name in roster),
        encoding="utf-8",
    )
    for name in present:
        skill = root / "skills" / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return root


def test_partially_missing_required_skill_sources_fail_closed_before_any_client_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # AQG-026: _skill_sources() returned whatever it happened to find, so absent
    # packaged skills made _install_skills() a silent no-op and apply/verify
    # could claim success with no AQG skills installed. A PARTIAL roster (not
    # just an empty skills/ dir) must fail closed before any surface is written.
    fake_root = _fake_aqg_root(
        tmp_path,
        roster=("aqg-code-construction", "aqg-security-review", "aqg-startup-preflight"),
        present=("aqg-code-construction", "aqg-extra-local"),
    )
    monkeypatch.setattr(work_installer, "REPO_ROOT", fake_root)
    home = tmp_path / "home"
    common = ["--client", "workbuddy-ai", "--scope", "user", "--home", str(home), "--aqg-root", str(REPO)]

    assert work_installer.main([*common, "--apply"]) == 1
    apply_err = capsys.readouterr().err
    assert "aqg-security-review" in apply_err, apply_err
    assert "aqg-startup-preflight" in apply_err, apply_err
    assert "aqg-code-construction" not in apply_err, apply_err

    root = home / ".workbuddy-ai"
    assert not list((root / "skills").glob("aqg-*"))
    assert not (root / "rules" / "aqg.md").exists()
    assert not (root / "aqg-support-report.md").exists()
    assert not (root / "settings.json").exists()
    assert not (root / "mcp.json").exists()
    # fail-closed must not smear across the sibling CodeBuddy / legacy WorkBuddy roots
    assert not (home / ".codebuddy").exists()
    assert not (home / ".workbuddy").exists()

    # verify must fail closed through the same source validation, not report OK.
    assert work_installer.main([*common, "--verify"]) == 1
    verify_err = capsys.readouterr().err
    assert "aqg-security-review" in verify_err, verify_err


def test_skill_sources_allows_extra_aqg_skills_beyond_the_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # skills.list is a deletion-guard FLOOR: adding a skill needs no manifest
    # edit, so a packaged aqg-* skill absent from the roster stays installable.
    fake_root = _fake_aqg_root(
        tmp_path,
        roster=("aqg-code-construction",),
        present=("aqg-code-construction", "aqg-extra-local"),
    )
    monkeypatch.setattr(work_installer, "REPO_ROOT", fake_root)
    assert [path.name for path in work_installer._skill_sources()] == [
        "aqg-code-construction",
        "aqg-extra-local",
    ]


@pytest.mark.parametrize("manifest", (None, "", "# only a comment\n"))
def test_absent_or_empty_roster_manifest_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: str | None
) -> None:
    # Nothing anchors the required set -> refuse to install rather than trust an
    # unverifiable source tree (same fail-closed stance as check_fixture_mix I6).
    fake_root = _fake_aqg_root(
        tmp_path, roster=("aqg-code-construction",), present=("aqg-code-construction",)
    )
    if manifest is None:
        (fake_root / "skills.list").unlink()
    else:
        (fake_root / "skills.list").write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(work_installer, "REPO_ROOT", fake_root)
    with pytest.raises(work_installer.InstallError):
        work_installer._skill_sources()


def test_skill_sources_accepts_the_real_packaged_roster() -> None:
    # GREEN guard: the shipped tree satisfies its own roster, so the new
    # validation cannot regress a normal install.
    assert [path.name for path in work_installer._skill_sources()] == _expected_skills()
