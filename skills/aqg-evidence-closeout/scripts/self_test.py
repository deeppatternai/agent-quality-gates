#!/usr/bin/env python3
"""Self-test for aqg_closeout.py.

Locks the Batch-2 dual-audit fixes (audit 6529b5d4):
- C1 file:line cell redacted in the construction section.
- C2 Transfer Test Pack row fields redacted + table-escaped.
- C3 threshold_strict range-validated (out-of-range never PASSES).
- C4 redact_secrets is fail-CLOSED even when the construction parser is absent.
"""

from __future__ import annotations

import io
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aqg_closeout as closeout


def _run_main(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    saved = sys.argv
    try:
        sys.argv = ["aqg_closeout.py", *argv]
        with redirect_stdout(out):
            rc = closeout.main()
    finally:
        sys.argv = saved
    return rc, out.getvalue()


# A GitHub-token-shaped canary: gh[posur]_ + 36 chars (matches the secret patterns).
def _canary(ch: str) -> str:
    return "ghp_" + ch * 36


# --------------------------------------------------------------------------
# Existing coverage
# --------------------------------------------------------------------------
def test_skeleton_prints_for_simple_task() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / ".git").mkdir()
        rc, text = _run_main(
            ["--task", "self-test", "--repo", str(repo), "--no-construction-import"]
        )
    assert rc == 0, f"closeout main returned {rc}, expected 0"
    assert "self-test" in text, "task slug missing from skeleton output"


def test_no_construction_import_is_silent_when_ledger_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / ".git").mkdir()
        rc, text = _run_main(["--task", "no-ledger", "--repo", str(repo)])
    assert rc == 0
    assert "Code Construction Evidence" not in text, \
        "should not render construction section when ledger missing"


# --------------------------------------------------------------------------
# C4 — redaction fail-closed without the construction parser
# --------------------------------------------------------------------------
def test_redact_fail_closed_without_construction_parser() -> None:
    token = _canary("C")
    saved = closeout._CONSTRUCTION
    try:
        closeout._CONSTRUCTION = None  # simulate construction skill absent
        redacted = closeout.redact_secrets(f"prefix {token} suffix")
    finally:
        closeout._CONSTRUCTION = saved
    assert token not in redacted, redacted


# --------------------------------------------------------------------------
# C1 — construction file:line cell is redacted
# --------------------------------------------------------------------------
def test_construction_file_line_cell_redacted() -> None:
    token = _canary("A")
    header = types.SimpleNamespace(
        task_slug="t",
        path="full",
        created_at="2026-05-26T00:00:00+00:00",
        session_agent="claude",
        skipped_checks=[],
        objections_diff_coverage_exception=None,
    )
    step = types.SimpleNamespace(
        step="1. Pattern Mining",
        evidence="neighbor code read",
        file_line=f"src/x.py:{token}",  # token planted in the file:line cell
        command_result="ok",
    )
    ledger_data = {
        "header": header,
        "steps": [step],
        "warning_section": "",
        "objection_section": "",
        "ledger_path": "/tmp/x",
    }
    out = io.StringIO()
    with redirect_stdout(out):
        closeout.print_construction_section(ledger_data)
    text = out.getvalue()
    assert token not in text, text


# --------------------------------------------------------------------------
# C2 — Transfer row fields redacted + pipe-escaped
# --------------------------------------------------------------------------
def test_transfer_fields_redacted_and_escaped() -> None:
    token = _canary("B")
    result = {
        "status": closeout.TransferImportStatus.OK_PASSED,
        "data": {
            "run_id": f"run-{token}",
            "date": "2026-05-26",
            "overall_pass_rate": 1.0,
            "threshold_strict": 1.0,
            "task5_advisory": "note | with pipe",
            "summary_path": "/tmp/s.yaml",
        },
        "detail": "ok",
    }
    row = closeout.render_transfer_pack_evidence_row(result)
    assert token not in row, row
    assert "note \\| with pipe" in row, row  # pipe escaped, not raw


# --------------------------------------------------------------------------
# C3 — threshold_strict range-validated (out-of-range never PASSES)
# --------------------------------------------------------------------------
def _write_summary(tmp: Path, body: str) -> Path:
    p = tmp / "summary.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_threshold_strict_out_of_range_is_malformed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_summary(
            Path(tmp),
            "run_id: r1\ndate: 2026-05-26\noverall_pass_rate: 0.0\n"
            "threshold_strict: -1.0\nthreshold_met: true\n",
        )
        result = closeout.import_transfer_summary(p, was_explicit=True)
    assert result["status"] == closeout.TransferImportStatus.MALFORMED, result


def test_valid_transfer_summary_still_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_summary(
            Path(tmp),
            "run_id: r1\ndate: 2026-05-26\noverall_pass_rate: 1.0\n"
            "threshold_strict: 1.0\nthreshold_met: true\ntask5_advisory: ok\n",
        )
        result = closeout.import_transfer_summary(p, was_explicit=True)
    assert result["status"] == closeout.TransferImportStatus.OK_PASSED, result


TESTS = [
    test_skeleton_prints_for_simple_task,
    test_no_construction_import_is_silent_when_ledger_missing,
    test_redact_fail_closed_without_construction_parser,
    test_construction_file_line_cell_redacted,
    test_transfer_fields_redacted_and_escaped,
    test_threshold_strict_out_of_range_is_malformed,
    test_valid_transfer_summary_still_passes,
]


if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"OK: aqg_closeout self-test passed ({len(TESTS)} tests)")
