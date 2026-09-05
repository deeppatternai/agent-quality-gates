#!/usr/bin/env python3
"""Self-test smoke for aqg_session_handoff (run by scripts/run_skill_self_tests.sh).

Real `test_` functions (not just a help smoke). Deep behavior-lock coverage of the
a3 §4 fixture matrix lives in tests/test_session_handoff.py (run by
`pytest skills/*/tests/`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aqg_session_handoff import render_skeleton, validate_handoff  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "aqg_session_handoff.py"

_VALID = (
    "## 1. Goal\nShip skill.\n\n"
    "## 2. Environment\nrepo ~/aqg, branch x, read-only.\n\n"
    "## 3. Done so far\nWrote code; audit_id abc123 adjudicated.\n\n"
    "## 4. Current Working State\nworktree clean\n\n"
    "## 5. Next\nWrite the sidecar.\n\n"
    "## 6. Discipline traps\nnone\n\n"
    "## 7. First step\nRun preflight then read SKILL.md.\n\n"
    "## 8. Open decisions\n- Owner: ship now? (yes).\n"
)


def test_render_skeleton_has_8_headers() -> None:
    out = render_skeleton(Path("."))
    for n in range(1, 9):
        assert f"## {n}." in out, f"missing header {n}"


def test_validate_happy_passes() -> None:
    res = validate_handoff(_VALID)
    assert res["valid"], res["violations"]


def test_validate_blacklist_first_step_fails() -> None:
    bad = _VALID.replace("Run preflight then read SKILL.md.", "继续之前的活")
    res = validate_handoff(bad)
    assert not res["valid"]
    assert any(v.startswith("R6") for v in res["violations"])


def test_validate_secret_redacted_not_releaked() -> None:
    bad = _VALID.replace("read-only.", "key AKIAIOSFODNN7EXAMPLE here")
    res = validate_handoff(bad)
    assert not res["valid"]
    assert all("AKIAIOSFODNN7EXAMPLE" not in v for v in res["violations"])


def test_round_trip_skeleton_fails_gate() -> None:
    res = validate_handoff(render_skeleton(Path(".")))
    rules = {v.split()[0] for v in res["violations"]}
    assert "R2" in rules and "R1" not in rules


def test_cli_new_exit_0() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "new"], text=True, encoding="utf-8", capture_output=True
    )
    assert proc.returncode == 0
    assert "## 1. Goal" in proc.stdout


def test_cli_validate_invalid_exit_1() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "validate"],
        input="## 1. Goal\n<unfilled>\n", text=True, encoding="utf-8", capture_output=True,
    )
    assert proc.returncode == 1


if __name__ == "__main__":
    test_render_skeleton_has_8_headers()
    test_validate_happy_passes()
    test_validate_blacklist_first_step_fails()
    test_validate_secret_redacted_not_releaked()
    test_round_trip_skeleton_fails_gate()
    test_cli_new_exit_0()
    test_cli_validate_invalid_exit_1()
    print("aqg-session-handoff self_test: all passed")
