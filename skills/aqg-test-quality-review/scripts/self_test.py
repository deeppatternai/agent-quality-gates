#!/usr/bin/env python3
"""Minimal self-test for aqg_test_quality_review.py.

Verifies the helper imports cleanly, `analyze` emits a ledger with a coverage
candidate when source changes with no test, and `validate` round-trips a clean
ledger. Exercises the exit-code contract (0 happy path).
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout

import aqg_test_quality_review as tq

_DIFF = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " def foo():\n"
    "+    return 1\n"
)

_CLEAN_LEDGER = (
    "test_quality_review:\n"
    "  candidates: []\n"
    "  counts: { shape_ratio: 0.0 }\n"
    "  anti_horizontal: { fired: false }\n"
    "  decision: accept\n"
    "  decision_reason: tests are behavioral and complete\n"
)


def _run(argv: list[str], stdin: str = "") -> tuple[int, str]:
    out = io.StringIO()
    saved_argv, saved_in = sys.argv, sys.stdin
    try:
        sys.argv = ["aqg_test_quality_review.py", *argv]
        sys.stdin = io.StringIO(stdin)
        with redirect_stdout(out):
            rc = tq.main(argv)
    finally:
        sys.argv, sys.stdin = saved_argv, saved_in
    return rc, out.getvalue()


def test_analyze_emits_coverage_candidate() -> None:
    rc, text = _run(["analyze"], stdin=_DIFF)
    assert rc == 0, f"analyze returned {rc}; output: {text[:200]}"
    assert "coverage_gap" in text, "expected coverage_gap candidate for untested source change"


def test_validate_clean_ledger() -> None:
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "ledger.md"
    p.write_text(_CLEAN_LEDGER, encoding="utf-8")
    rc, text = _run(["validate", "--file", str(p)])
    assert rc == 0, f"validate returned {rc}; output: {text[:200]}"
    assert "VALID" in text


if __name__ == "__main__":
    test_analyze_emits_coverage_candidate()
    test_validate_clean_ledger()
    print("OK: aqg_test_quality_review self-test passed")
