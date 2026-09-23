#!/usr/bin/env python3
"""Minimal self-test for aqg_phase_emit.py.

Verifies the phase-emit helper imports cleanly and emits a phase signal
to a tmp .aqg/ workspace returning a valid `recommended_audit_mode`.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import aqg_phase_emit as emit


def _run_main(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    saved = sys.argv
    try:
        sys.argv = ["aqg_phase_emit.py", *argv]
        with redirect_stdout(out):
            rc = emit.main()
    finally:
        sys.argv = saved
    return rc, out.getvalue()


def test_emit_returns_valid_audit_mode() -> None:
    """Issue #114 sidecar smoke: emit a PLAN_DONE phase + parse recommended_audit_mode.

    State persists to <repo>/.aqg/phase-state-<safe-task>-<short-hash>.json; we
    use a tmp dir as the repo so no real .aqg/ is polluted.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        artifact = repo / "plan.md"
        artifact.write_text("# Test plan\n\nMechanical bug fix.\n")
        rc, text = _run_main(
            [
                "--repo", str(repo),
                "--json",
                "emit",
                "--phase", "plan_done",
                "--stakes", "trivial",
                "--task", "selftest-114",
                "--artifact-file", str(artifact),
            ]
        )
    assert rc == 0, f"emit returned {rc} (expected 0); output: {text[:200]}"
    payload = json.loads(text)
    assert payload.get("recommended_audit_mode") in ("fast", "standard", "deep", "skip"), (
        f"unexpected recommended_audit_mode: {payload.get('recommended_audit_mode')}"
    )


if __name__ == "__main__":
    test_emit_returns_valid_audit_mode()
    print("OK: aqg_phase_emit self-test passed")
