"""Behavior lock for scripts/install_aqg.sh — the one-shot AQG installer.

The risky new logic is the marker-delimited AQG_ROOT block written into the
shell rc: it must be idempotent (a re-run replaces, never stacks), back up the
rc, preserve unrelated content, honour --dry-run (write nothing), and be fully
removed by --uninstall. Orchestration of the already-tested per-client
installers + doctor is exercised via a real `--clients codex` run in a sandbox
HOME; assertions are on the rc file (robust to doctor's sandbox exit code).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "install_aqg.sh"
BEGIN = "# >>> agent-quality-gates (AQG_ROOT) >>>"


def _run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "SHELL": "/bin/zsh",
        "CODEX_HOME": str(home / ".codex"),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_dry_run_writes_nothing(tmp_path):
    r = _run(tmp_path, "--dry-run", "--yes", "--clients", "codex")
    assert "would write to" in r.stdout
    assert not (tmp_path / ".zshrc").exists()  # dry-run must not touch the rc


def test_real_write_creates_single_block_and_backup(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text("echo pre-existing\n", encoding="utf-8")
    _run(tmp_path, "--yes", "--no-hooks", "--clients", "codex")
    body = rc.read_text(encoding="utf-8")
    assert body.count(BEGIN) == 1                 # exactly one block
    assert "export AQG_ROOT=" in body
    assert str(REPO) in body                      # points at this checkout
    assert "echo pre-existing" in body            # unrelated content preserved
    assert list(tmp_path.glob(".zshrc.aqg-bak.*"))  # backup written


def test_idempotent_rerun_does_not_stack(tmp_path):
    _run(tmp_path, "--yes", "--no-hooks", "--clients", "codex")
    _run(tmp_path, "--yes", "--no-hooks", "--clients", "codex")
    body = (tmp_path / ".zshrc").read_text(encoding="utf-8")
    assert body.count(BEGIN) == 1                 # second run replaced, not stacked
    assert body.count("export AQG_ROOT=") == 1


def test_uninstall_removes_block_preserving_rest(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text("echo keep-me\n", encoding="utf-8")
    _run(tmp_path, "--yes", "--no-hooks", "--clients", "codex")
    r = _run(tmp_path, "--uninstall", "--yes", "--clients", "codex")
    assert r.returncode == 0
    body = rc.read_text(encoding="utf-8")
    assert BEGIN not in body                       # block gone
    assert "export AQG_ROOT=" not in body
    assert "echo keep-me" in body                  # unrelated content preserved


def test_one_shot_codex_install_adds_the_codex_hook_pack(tmp_path):
    """Default hooks apply to every selected supported client, not only Claude."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    r = _run(tmp_path, "--yes", "--clients", "codex")
    assert r.returncode == 0, r.stdout + r.stderr
    hooks = codex_home / "hooks.json"
    assert hooks.is_file()
    assert "pretooluse_secret_scan.sh" in hooks.read_text(encoding="utf-8")
    assert "AQG_ROOT env" in r.stdout


def test_no_hooks_skips_the_codex_hook_pack(tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    r = _run(tmp_path, "--yes", "--no-hooks", "--clients", "codex")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (codex_home / "hooks.json").exists()


def test_dual_client_install_writes_both_supported_hook_layers(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    r = _run(tmp_path, "--yes", "--clients", "claude,codex")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert (tmp_path / ".codex" / "hooks.json").is_file()


def test_warn_only_dual_client_install_skips_codex_hooks(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    r = _run(tmp_path, "--yes", "--warn-only-hooks", "--clients", "claude,codex")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "run_warn_only.sh" in (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert not (tmp_path / ".codex" / "hooks.json").exists()


def test_unknown_arg_exits_2(tmp_path):
    r = _run(tmp_path, "--bogus")
    assert r.returncode == 2


def test_help_only_prints_documented_usage(tmp_path):
    r = _run(tmp_path, "--help")
    assert r.returncode == 0
    assert "scripts/install_aqg.sh [options]" in r.stdout
    assert "set -euo pipefail" not in r.stdout


def test_no_client_selected_errors(tmp_path):
    # empty --clients and no ~/.claude / ~/.codex probed -> refuse, don't no-op
    r = _run(tmp_path, "--yes", "--clients", "")
    assert r.returncode != 0
    assert "no client selected" in r.stdout + r.stderr


# ---- audit 04545796 regression net (the bugs the first suite missed) --------


def test_malformed_block_aborts_without_truncating(tmp_path):
    # rc with a BEGIN marker but no END (partial / hand-corrupted block) must NOT
    # be truncated past the marker — the write aborts, rc content survives
    # (audit 04545796 f1: the critical data-loss case; convergent 4/4).
    rc = tmp_path / ".zshrc"
    rc.write_text(
        "keep-above\n"
        f"{BEGIN}\n"
        "export AQG_ROOT=/old\n"
        "keep-below-must-survive\n",
        encoding="utf-8",
    )
    _run(tmp_path, "--yes", "--no-hooks", "--clients", "codex")
    body = rc.read_text(encoding="utf-8")
    assert "keep-above" in body
    assert "keep-below-must-survive" in body   # not dropped past the stray BEGIN


def test_stale_block_path_gets_updated(tmp_path):
    # a well-formed block with an OLD path, with the NEW path appearing on an
    # unrelated line, must be rewritten — not read as "unchanged" (audit f2).
    rc = tmp_path / ".zshrc"
    rc.write_text(
        f"# ref {REPO}\n"
        f"{BEGIN}\n"
        "export AQG_ROOT=/old/stale\n"
        "# <<< agent-quality-gates (AQG_ROOT) <<<\n",
        encoding="utf-8",
    )
    _run(tmp_path, "--yes", "--no-hooks", "--clients", "codex")
    body = rc.read_text(encoding="utf-8")
    assert "/old/stale" not in body               # stale path replaced
    assert f"AQG_ROOT={REPO}" in body             # block now points at this checkout


def test_clients_missing_value_exits_2(tmp_path):
    # trailing --clients with no value -> clean usage exit 2, not a shift crash
    r = _run(tmp_path, "--clients")
    assert r.returncode == 2
    assert "requires a value" in r.stdout + r.stderr


def test_default_codex_path_does_not_claim_hooks_were_skipped(tmp_path):
    """The orchestrator owns the Codex hook decision, so it suppresses the skills
    installer's own hook step. The user never passed --no-hooks, so the run must
    not report hooks as skipped by a flag they did not use — deep audit
    aud_b_1HGp0d-yqFwDDI f3/f1: misleading consent output on a run that installs
    4 blocking preToolUse policies."""
    (tmp_path / ".codex").mkdir()
    r = _run(tmp_path, "--yes", "--clients", "codex")
    assert (tmp_path / ".codex" / "hooks.json").exists(), r.stdout + r.stderr
    assert "skipped by --no-hooks" not in r.stdout, r.stdout
