"""CI-gated guard: the Claude pack installer installs supported hooks by default
so an unfamiliar user does not install skills and silently miss resident gates.

Key invariants:
- A normal user-scope install writes managed hooks and never blocks on an
  interactive prompt.
- A custom --dest install defaults to skills-only so tests and nonstandard
  destinations do not silently edit the real ~/.claude/settings.json.
- AQG_SKIP_HOOK_PROMPT=1 suppresses hook management entirely; upgrade.sh sets it
  so it can own the hook decision without double-running the installer.

Installs to a throwaway --dest so nothing real is touched; stdin is /dev/null so
the `[ -t 0 ]` interactive branch is never taken.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "agent-packs" / "claude-code" / "install.sh"


def _run(dest: Path, skip_prompt: bool = False) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "AQG_SKIP_HOOK_PROMPT"}
    if skip_prompt:
        env["AQG_SKIP_HOOK_PROMPT"] = "1"
    return subprocess.run(
        ["bash", str(INSTALLER), "--scope", "user", "--mode", "link", "--force",
         "--dest", str(dest)],
        text=True, capture_output=True, cwd=str(REPO), env=env,
        stdin=subprocess.DEVNULL,
    )


def test_recommends_hooks_without_prompting(tmp_path):
    dest = tmp_path / "skills"
    dest.mkdir()
    proc = _run(dest)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    assert "lifecycle hooks skipped for custom --dest" in out, out
    assert "Install the AQG hooks now?" not in out, \
        "must NOT prompt when stdin is not a TTY (would hang scripted installs)"


def test_user_scope_installs_hooks_by_default_and_stays_idempotent(tmp_path):
    home = tmp_path / "home"
    env = {k: v for k, v in os.environ.items() if k != "AQG_SKIP_HOOK_PROMPT"}
    env["HOME"] = str(home)
    env["AQG_BACKUP_DIR"] = str(tmp_path / ".aqg-central")
    args = ["bash", str(INSTALLER), "--scope", "user", "--mode", "link", "--force"]

    proc = subprocess.run(
        args,
        text=True,
        capture_output=True,
        cwd=str(REPO),
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr

    settings = home / ".claude" / "settings.json"
    assert settings.is_file()
    verify = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "install_aqg_hooks.py"),
            "--verify",
            "--target",
            str(settings),
            "--aqg-root",
            str(REPO),
        ],
        text=True,
        capture_output=True,
        cwd=str(REPO),
        env=env,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr

    before = settings.read_text(encoding="utf-8")
    rerun = subprocess.run(
        args,
        text=True,
        capture_output=True,
        cwd=str(REPO),
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert rerun.returncode == 0, rerun.stderr
    assert settings.read_text(encoding="utf-8") == before


def test_skip_env_suppresses_recommendation(tmp_path):
    dest = tmp_path / "skills"
    dest.mkdir()
    proc = _run(dest, skip_prompt=True)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    assert "install_aqg_hooks.py" not in out, \
        "AQG_SKIP_HOOK_PROMPT=1 must suppress hook management (upgrade.sh owns it)"
