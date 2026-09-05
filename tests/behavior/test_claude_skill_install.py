from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "agent-packs" / "claude-code" / "install.sh"


def load_script_module(name: str):
    path = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_claude_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def bash_executable() -> str:
    if os.name != "nt":
        bash = shutil.which("bash")
        if bash:
            return bash
        pytest.skip("bash is unavailable")
    git = shutil.which("git")
    if git:
        git_bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    pytest.skip("Git Bash is unavailable")


def test_link_mode_creates_doctor_recognized_links_in_temporary_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "home" / ".claude" / "skills"
    env = os.environ.copy()
    env.update(
        {
            "AQG_SKIP_HOOK_PROMPT": "1",
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
        }
    )

    completed = subprocess.run(
        [bash_executable(), str(INSTALLER), "--dest", str(destination), "--mode", "link"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    skill_install = load_script_module("aqg_skill_install")
    expected_names = sorted(
        path.name
        for path in (REPO / "agent-packs" / "claude-code" / "skills").glob("aqg-*")
        if path.is_dir()
    )
    expected_types = {"symlink", "junction"} if os.name == "nt" else {"symlink"}
    actual_types = {
        skill_install.classify_install(destination / name) for name in expected_names
    }
    assert actual_types <= expected_types

    doctor = load_script_module("aqg_doctor")
    results = doctor.check_skill_install(
        label="claude_skill",
        target_dir=destination,
        expected_names=expected_names,
        aqg_root=REPO,
    )
    assert results
    assert all(result.status == "PASS" for result in results), results

    doctor_env = env.copy()
    doctor_env["AQG_ROOT"] = str(REPO)
    doctor_cli = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "aqg_doctor.py"),
            "--no-cli",
            "--json",
            "--claude-skills-dir",
            str(destination),
            "--codex-skills-dir",
            str(destination),
        ],
        cwd=REPO,
        env=doctor_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    payload = json.loads(doctor_cli.stdout)
    assert doctor_cli.returncode == 0, payload
    # Assert the skill-install checks by name, since those are what this test
    # installed. Everything else in the same run describes either the source
    # checkout or the ambient host, not this installation:
    #   script:*        — CRITICAL_SCRIPTS inside the AQG checkout
    #   hook_visible:*  — hooks read from AQG_ROOT, not from `destination`
    #   rules_block:*   — the throwaway HOME above, where installing creates a
    #                     bare .claude with no CLAUDE.md and doctor rightly WARNs
    #   pyyaml          — redirecting HOME also hides user-site packages
    # Asserting all of them made this test fail whenever doctor grew a check.
    # Enumerate rather than prefix-match: a rename that collapsed the selection
    # down to the two roots would otherwise still pass.
    expected_checks = {"claude_skill_root", "codex_skill_root"}
    expected_checks.update(f"claude_skill:{name}" for name in expected_names)
    expected_checks.update(f"codex_skill:{name}" for name in expected_names)
    by_name = {result["name"]: result for result in payload["results"]}
    missing = sorted(expected_checks - by_name.keys())
    assert not missing, (missing, payload)
    not_passing = [
        by_name[name] for name in sorted(expected_checks) if by_name[name]["status"] != "PASS"
    ]
    assert not not_passing, not_passing
