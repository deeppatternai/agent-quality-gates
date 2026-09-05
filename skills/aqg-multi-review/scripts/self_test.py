#!/usr/bin/env python3
"""Minimal self-test for aqg_multi_review.py.

Verifies the multi-review helper imports cleanly and `new` mode produces
a YAML ledger skeleton containing all 5 expected dimensions.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout

import aqg_multi_review as mreview


EXPECTED_DIMENSIONS = ("logic", "edge_cases", "security", "performance", "concurrency")


def _run_main(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    saved = sys.argv
    try:
        sys.argv = ["aqg_multi_review.py", *argv]
        with redirect_stdout(out):
            rc = mreview.main()
    finally:
        sys.argv = saved
    return rc, out.getvalue()


def test_new_mode_emits_all_five_dimensions() -> None:
    """Issue #114 sidecar smoke: `new` returns ledger skeleton with 5 dims."""
    rc, text = _run_main(["new"])
    assert rc == 0, f"new mode returned {rc} (expected 0); output: {text[:200]}"
    for dim in EXPECTED_DIMENSIONS:
        assert dim in text, f"missing dimension '{dim}' in skeleton output"


if __name__ == "__main__":
    test_new_mode_emits_all_five_dimensions()
    print("OK: aqg_multi_review self-test passed")
