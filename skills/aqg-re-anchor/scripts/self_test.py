#!/usr/bin/env python3
"""Self-test smoke for aqg_re_anchor (run by scripts/run_skill_self_tests.sh).

Real `test_` functions (not just a help smoke) per Finding B — a self_test with
0 test functions is a thin gate. Deep logic coverage lives in
tests/test_re_anchor.py (run by `pytest skills/*/tests/`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aqg_re_anchor import render_re_anchor  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "aqg_re_anchor.py"


def test_render_basic_block() -> None:
    out = render_re_anchor(
        goal="ship X",
        gates=["gate1"],
        progress=[{"label": "step1", "state": "current"}],
    )
    assert "🧭 RE-ANCHOR" in out
    assert "ship X" in out
    assert "▶ step1" in out
    assert "→ now: step1" in out


def test_render_degrades_on_empty() -> None:
    out = render_re_anchor()
    assert "(no goal set)" in out
    assert "(no active gates declared)" in out
    assert "(no progress tracked)" in out


def test_render_sanitizes_control_chars() -> None:
    out = render_re_anchor(goal="blocked\r done\x1b[2J", gates=[], progress=[])
    assert "\r" not in out
    assert "\x1b" not in out


def test_cli_stdin_exit_0() -> None:
    payload = json.dumps({"goal": "G", "gates": [], "progress": []})
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], input=payload, text=True, encoding="utf-8", capture_output=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "G" in proc.stdout


def test_cli_bad_json_exit_2() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], input="garbage{", text=True, encoding="utf-8", capture_output=True
    )
    assert proc.returncode == 2


def test_help_smoke() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], text=True, encoding="utf-8", capture_output=True
    )
    assert proc.returncode == 0
    assert "aqg_re_anchor" in proc.stdout


if __name__ == "__main__":
    test_render_basic_block()
    test_render_degrades_on_empty()
    test_render_sanitizes_control_chars()
    test_cli_stdin_exit_0()
    test_cli_bad_json_exit_2()
    test_help_smoke()
    print("aqg-re-anchor self_test: all passed")
