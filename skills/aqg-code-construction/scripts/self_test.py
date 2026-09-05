#!/usr/bin/env python3
"""Minimal self-test for aqg_construction_check.py.

Verifies the construction checker imports cleanly, exits silently when
AQG_AGENT is unset (the gating contract), and rejects a missing ledger
with EXIT_LEDGER_MALFORMED when AQG_AGENT is set.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import aqg_construction_check as cc


def _run_main(argv: list[str], *, env_aqg_agent: str | None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    saved_env = os.environ.get("AQG_AGENT")
    if env_aqg_agent is None:
        os.environ.pop("AQG_AGENT", None)
    else:
        os.environ["AQG_AGENT"] = env_aqg_agent
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cc.main(argv)
    finally:
        if saved_env is None:
            os.environ.pop("AQG_AGENT", None)
        else:
            os.environ["AQG_AGENT"] = saved_env
    return rc, out.getvalue(), err.getvalue()


def test_silent_pass_when_aqg_agent_unset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rc, _, err = _run_main(["--cwd", tmp, "--quiet"], env_aqg_agent=None)
    assert rc == cc.EXIT_OK, f"expected EXIT_OK silent pass, got {rc}"
    assert err == "", f"--quiet + AQG_AGENT unset should produce no stderr, got {err!r}"


def test_missing_ledger_returns_ledger_malformed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rc, _, err = _run_main(
            ["--ledger", str(Path(tmp) / "does-not-exist.md"), "--cwd", tmp, "--quiet"],
            env_aqg_agent="human-opt-in",
        )
    assert rc == cc.EXIT_LEDGER_MALFORMED, \
        f"expected EXIT_LEDGER_MALFORMED ({cc.EXIT_LEDGER_MALFORMED}), got {rc}"
    assert "ledger not found" in err.lower()


if __name__ == "__main__":
    test_silent_pass_when_aqg_agent_unset()
    test_missing_ledger_returns_ledger_malformed()
    print("OK: aqg_construction_check self-test passed")
