"""CI-gated guard: scripts/upgrade.sh installs Claude Code hooks BY DEFAULT, with
two carve-outs that preserve an explicit user choice.

Default ("auto", neither --hooks nor --no-hooks) on a machine WITH ~/.claude or
CODEX_HOME:
  - already on the managed set        -> refresh (pick up newly-shipped hooks)
  - explicit warn-only opt-in present -> leave byte-for-byte untouched (never
    promote warn-only -> the full blocking set; audit cf5adc7f f1)
  - otherwise (fresh)                 -> install the managed set
`--hooks` forces install/refresh; `--no-hooks` skips entirely. (Owner 2026-06-10:
flipped from opt-in to default-on so "installed AQG = enforcement resident".)

Throwaway $HOME so the real ~/.claude is never touched; --clean-only --no-codex
--no-claude isolates the hook step; the trailing doctor fails on the empty fake
HOME, so we read output and ignore the exit code on purpose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPGRADE = REPO / "scripts" / "upgrade.sh"
WARN_ONLY_EXAMPLE = (
    REPO / "agent-packs" / "claude-code" / "hooks" / "settings.warn-only.example.json"
)


def _bash_executable() -> str:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            root = Path(git).resolve().parent.parent
            for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash") or "bash"


def _msys_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) >= 3 and value[1:3] == ":/":
        return f"/{value[0].lower()}/{value[3:]}"
    return value


def _test_env(home: Path, *, codex_home: Path | None = None) -> dict[str, str]:
    bin_dir = home / "test-bin"
    bin_dir.mkdir(exist_ok=True)
    python3 = bin_dir / "python3"
    python3.write_bytes(
        f"#!/usr/bin/env bash\nexec '{Path(sys.executable).as_posix()}' \"$@\"\n".encode(
            "utf-8"
        )
    )
    python3.chmod(0o755)
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive,
        "HOMEPATH": str(home)[len(home.drive):],
        "PATH": f"{_msys_path(bin_dir)}:/usr/bin:/bin",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env


def _run(home: Path, *args: str) -> str:
    env = _test_env(home)
    proc = subprocess.run(
        [_bash_executable(), str(UPGRADE), "--clean-only", "--no-codex", "--no-claude", *args],
        text=True, encoding="utf-8", errors="replace", capture_output=True, cwd=str(REPO), env=env,
    )
    return proc.stdout + proc.stderr  # exit code ignored (doctor fails on empty HOME)


def _run_codex(home: Path, *args: str) -> str:
    codex_home = home / ".codex"
    codex_home.mkdir(exist_ok=True)
    env = _test_env(home, codex_home=codex_home)
    proc = subprocess.run(
        [_bash_executable(), str(UPGRADE), "--clean-only", "--no-claude", *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        cwd=str(REPO),
        env=env,
    )
    return proc.stdout + proc.stderr


def test_default_leaves_warn_only_settings_byte_for_byte(tmp_path):
    """A machine that opted into ONLY the warn-only example must be untouched by a
    default upgrade — never promoted to the full hook set (audit cf5adc7f f1)."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    settings = claude / "settings.json"
    settings.write_bytes(WARN_ONLY_EXAMPLE.read_bytes())
    before = settings.read_bytes()

    out = _run(tmp_path)  # default: no flag

    assert "warn-only opt-in detected" in out, out
    assert settings.read_bytes() == before, "default upgrade must not modify a warn-only opt-in"
    assert "pretooluse_bash_skill_validator.sh" not in settings.read_text(encoding="utf-8"), \
        "default upgrade must not promote a warn-only opt-in to the full (blocking) set"


def test_default_installs_managed_set_on_fresh_claude(tmp_path):
    """A fresh machine WITH Claude Code but no AQG hooks gets the managed set installed
    by default (Owner 2026-06-10: 'installed AQG = enforcement resident')."""
    (tmp_path / ".claude").mkdir()
    out = _run(tmp_path)
    assert "installing managed set (default" in out, out
    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists(), "default upgrade must install the hook set on a fresh machine"
    text = settings.read_text(encoding="utf-8")
    assert "pretooluse_bash_skill_validator.sh" in text, "managed (blocking) set not installed"
    assert "userpromptsubmit_handoff_mandate.sh" in text, "handoff-mandate hook not installed"
    # Invariant the warn-only-respect branch relies on (audit 2ab8499e claude f2): the
    # managed installer must NEVER emit the warn-only wrapper, else a managed machine
    # would be misread as warn-only on a later upgrade.
    assert "run_warn_only.sh" not in text, "managed install must not contain the warn-only wrapper"


def test_no_hooks_flag_skips(tmp_path):
    (tmp_path / ".claude").mkdir()
    out = _run(tmp_path, "--no-hooks")
    assert "skipped (--no-hooks)" in out, out
    assert not (tmp_path / ".claude" / "settings.json").exists(), \
        "--no-hooks must not create/modify settings.json"


def test_hooks_flag_force_installs(tmp_path):
    (tmp_path / ".claude").mkdir()
    out = _run(tmp_path, "--hooks")
    assert "force install/refresh" in out, out


def test_default_refreshes_when_managed_set_already_installed(tmp_path):
    """(b) A machine already on the managed (full) hook set gets NEW hooks
    auto-added on a default upgrade (no --hooks) — so e.g. security-review lands
    without re-running --hooks. Opt-in is preserved: only machines already on the
    managed set are refreshed; warn-only-only and never-installed stay untouched
    (the two guards above + the cf5adc7f regression)."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    settings = claude / "settings.json"
    # Simulate an older managed install: one canonical managed hook present,
    # the newer security-review hook absent.
    settings.write_text(
        json.dumps({
            "hooks": {
                "SessionStart": [{
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": (
                            'if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi; '
                            'bash "$AQG_ROOT/agent-packs/claude-code/hooks/'
                            'sessionstart_preflight.sh" "${CLAUDE_PROJECT_DIR:-}" || true'
                        ),
                    }],
                }],
            }
        }),
        encoding="utf-8",
    )

    out = _run(tmp_path)  # default: no --hooks

    assert ("already opted in" in out) or ("refreshing" in out), out
    after = settings.read_text(encoding="utf-8")
    assert "posttooluse_security_review_reminder.sh" in after, (
        "default upgrade on a managed-set machine must auto-add the new "
        "security-review hook"
    )


def test_codex_default_installs_managed_set(tmp_path):
    out = _run_codex(tmp_path)
    assert "Codex hooks: installing managed set (default" in out, out
    hooks = (tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8")
    assert "run_aqg_codex_hook.py" in hooks
    assert "pretooluse_secret_scan.sh" in hooks


def test_codex_hooks_flag_installs_managed_set(tmp_path):
    out = _run_codex(tmp_path, "--hooks")
    assert "Codex hooks (force install/refresh)" in out, out
    hooks = (tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8")
    assert "run_aqg_codex_hook.py" in hooks
    assert "pretooluse_secret_scan.sh" in hooks


def test_codex_default_refreshes_existing_managed_set(tmp_path):
    hooks_file = tmp_path / ".codex" / "hooks.json"
    hooks_file.parent.mkdir(exist_ok=True)
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python /old/run_aqg_codex_hook.py pretooluse_bash_skill_validator.sh",
                                    "commandWindows": "python C:\\old\\run_aqg_codex_hook.py pretooluse_bash_skill_validator.sh",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    out = _run_codex(tmp_path)
    assert "refreshing existing managed set" in out, out
    refreshed = hooks_file.read_text(encoding="utf-8")
    assert "posttooluse_security_review_reminder.sh" in refreshed
    assert "userpromptsubmit_handoff_mandate.sh" in refreshed
    assert "/old/" not in refreshed


def test_codex_default_fails_loudly_for_malformed_existing_config(tmp_path):
    hooks_file = tmp_path / ".codex" / "hooks.json"
    hooks_file.parent.mkdir(exist_ok=True)
    hooks_file.write_text("{not-json", encoding="utf-8")
    out = _run_codex(tmp_path)
    assert "invalid Codex hooks config" in out
    assert "installing managed set (default" not in out
    assert hooks_file.read_text(encoding="utf-8") == "{not-json"


def test_codex_no_hooks_flag_never_creates_hook_file(tmp_path):
    out = _run_codex(tmp_path, "--no-hooks")
    assert "Codex hooks: skipped (--no-hooks)" in out, out
    assert not (tmp_path / ".codex" / "hooks.json").exists()
