"""Tests for scripts/probe_hook_deny_contract.py (#333).

The probe confirms a PreToolUse hook exiting 2 still DENIES on the host Claude Code.
Its HARD guarantee is FAIL-detection: a written proof file (the marker command's
side effect) means the command executed despite exit 2, checked first and on every
path. PASS is advisory, gated on a sentinel the hook writes on its exit-2 branch.

The pure tri-state verdict (`evaluate`) is unit-tested here; the orchestration
(`main`/`_run_live`) is exercised with a STUB `claude` injected via
AQG_PROBE_CLAUDE_BIN, so CI needs no real Claude Code binary. The live run against
real CC is a manual on-upgrade step (validated on 2.1.185).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import probe_hook_deny_contract as probe  # noqa: E402

MARK = probe.MARKER
_CMD = f"echo hi > /tmp/x/proof_{MARK}.txt"
_DENIAL = {"permission_denials": [{"tool_input": {"command": _CMD}}]}


# ===== evaluate(): pure tri-state verdict, checked proof-first =====


def test_evaluate_fail_when_proof_written_even_without_sentinel() -> None:
    # Audit 1800e6e6 f1 (claude, HIGH): a written proof = the command EXECUTED = a
    # real leak. It must be FAIL regardless of the sentinel — if the sentinel
    # mechanism also degraded, a (no-sentinel, proof-present) leak must NOT be
    # mislabeled as a benign "model didn't attempt" ERROR.
    verdict, reason = probe.evaluate(hook_fired=False, deny_proof_exists=True, result_json={})
    assert verdict == probe.VERDICT_FAIL and "EXECUTED" in reason


def test_evaluate_fail_when_proof_written_with_sentinel() -> None:
    verdict, _ = probe.evaluate(hook_fired=True, deny_proof_exists=True, result_json=_DENIAL)
    assert verdict == probe.VERDICT_FAIL


def test_evaluate_pass_when_hook_fired_and_no_proof() -> None:
    verdict, _ = probe.evaluate(hook_fired=True, deny_proof_exists=False, result_json=_DENIAL)
    assert verdict == probe.VERDICT_PASS


def test_evaluate_pass_is_robust_to_missing_denial_schema() -> None:
    # Audit 5c72e5f9 f4: the verdict rests on behavioral signals, so an absent/renamed
    # permission_denials must NOT flip PASS to FAIL — it only drops the advisory note.
    verdict, reason = probe.evaluate(hook_fired=True, deny_proof_exists=False, result_json={})
    assert verdict == probe.VERDICT_PASS and "did not corroborate" in reason


def test_evaluate_error_when_marker_not_attempted() -> None:
    # Audit 5c72e5f9 f2: no proof + hook never fired = the model never tried the
    # command. Inconclusive, NOT a contract FAIL.
    verdict, reason = probe.evaluate(hook_fired=False, deny_proof_exists=False, result_json={})
    assert verdict == probe.VERDICT_ERROR and "INCONCLUSIVE" in reason


# ===== main(): skip path uses a DISTINCT exit code (audit 5c72e5f9 grok f4) =====


def test_main_skips_with_distinct_code_when_claude_absent(monkeypatch, capsys) -> None:
    monkeypatch.delenv("AQG_PROBE_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(probe.shutil, "which", lambda _name: None)
    rc = probe.main([])
    out = capsys.readouterr().out
    assert rc == probe.EXIT_SKIP and "SKIP" in out


# ===== main()/_run_live(): full orchestration via a stub `claude` =====
# The stub runs with cwd = the probe's temp workdir, so it writes the sentinel as
# `./hook_fired` (matching the baked path the real hook uses) — no agent-visible env.

_STUB = """\
#!/usr/bin/env bash
set -eu
if [ "${1:-}" = "--version" ]; then echo "9.9.9 (stub claude)"; exit 0; fi
prompt=""; prev=""
for a in "$@"; do [ "$prev" = "-p" ] && prompt="$a"; prev="$a"; done
proof=$(printf '%s' "$prompt" | sed -E 's/.*echo hi > ([^ ]+).*/\\1/')
mode="${AQG_PROBE_STUB_MODE:-deny_works}"
case "$mode" in
  not_attempted)
    printf '{"permission_denials": []}\\n' ;;                 # model never ran the command
  deny_broken)
    : > ./hook_fired                                          # hook fired...
    echo hi > "$proof"                                        # ...but the command RAN
    printf '{"permission_denials": []}\\n' ;;
  leak_nonzero_exit)
    echo hi > "$proof"                                        # command ran (leak)...
    echo boom >&2
    exit 7 ;;                                                 # ...AND claude exits nonzero
  *) # deny_works
    : > ./hook_fired                                          # hook fired its exit-2 branch
    printf '{"permission_denials":[{"tool_name":"Bash","tool_input":{"command":"echo hi > %s"}}]}\\n' "$proof" ;;
esac
"""


def _stub_env(monkeypatch, tmp_path: Path, mode: str) -> None:
    stub = tmp_path / "claude_stub.sh"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("AQG_PROBE_CLAUDE_BIN", str(stub))
    monkeypatch.setenv("AQG_PROBE_STUB_MODE", mode)


def test_main_passes_with_stub_that_denies(monkeypatch, tmp_path, capsys) -> None:
    _stub_env(monkeypatch, tmp_path, "deny_works")
    assert probe.main(["--timeout", "30"]) == probe.EXIT_PASS, capsys.readouterr()


def test_main_fails_with_stub_that_leaks(monkeypatch, tmp_path, capsys) -> None:
    _stub_env(monkeypatch, tmp_path, "deny_broken")
    rc = probe.main(["--timeout", "30"])
    assert rc == probe.EXIT_FAIL and "BROKEN" in capsys.readouterr().err


def test_main_fails_when_leak_coincides_with_nonzero_exit(monkeypatch, tmp_path, capsys) -> None:
    # Audit 1800e6e6 f2 (3/3 convergent): the command ran (proof) AND claude exited
    # nonzero — the proof evidence must still surface as FAIL, not be swallowed as ERROR.
    _stub_env(monkeypatch, tmp_path, "leak_nonzero_exit")
    rc = probe.main(["--timeout", "30"])
    assert rc == probe.EXIT_FAIL and "BROKEN" in capsys.readouterr().err


def test_main_errors_when_marker_not_attempted(monkeypatch, tmp_path, capsys) -> None:
    # Audit 5c72e5f9 f2: the model never invoked Bash → inconclusive ERROR (exit 2),
    # NOT a false "contract BROKEN" alarm.
    _stub_env(monkeypatch, tmp_path, "not_attempted")
    rc = probe.main(["--timeout", "30"])
    assert rc == probe.EXIT_ERROR and "INCONCLUSIVE" in capsys.readouterr().err
