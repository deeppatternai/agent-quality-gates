#!/usr/bin/env python3
"""Self-test for the merged adjudication validator.

The entry script lives at repo-level `scripts/validate_audit_adjudication.py`
(see skill.template.json entry_script); this smoke test imports it from there.
Locks the shape invariants carried over from the skill validator (audit
ae917256 / 469e66f5) AND the content invariants merged in from the gate
validator (filled cells, needs-user-decision detail), all through the unified
`validate_text` entry point.

Run with: python3 skills/aqg-audit-adjudication/scripts/self_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import validate_audit_adjudication as v  # noqa: E402


VALID = """\
| finding | decision | action | verification |
|---|---|---|---|
| stale state | accepted | rerun preflight | preflight exit 0 |
| broad rewrite | rejected | outside scope, keep as-is | diff unchanged |
| prod deploy | needs-user-decision | ask Owner for deploy authorization | Owner approval recorded |
"""

# P2: header + data with no separator row is not a table.
NO_SEPARATOR = (
    "| finding | decision | action | verification |\n"
    "| stale | accepted | fix it | check passes |\n"
)

# C1: bare pipes inside backtick code spans (a shell pipeline) must not split.
PIPED_CODE = (
    "| finding | decision | action | verification |\n"
    "|---|---|---|---|\n"
    "| flaky grep | accepted | run `cat x | grep y` then assert | `a | b` exits 0 |\n"
)

# C1: a backslash-escaped pipe is literal content, not a delimiter.
ESCAPED_PIPE = (
    "| finding | decision | action | verification |\n"
    "|---|---|---|---|\n"
    "| handles a\\|b | accepted | normalize input | exit 0 verified |\n"
)

# C2 (now strict): a row-misaligned table whose header IS the required set is a
# hard violation, NOT silently skipped — the merged engine validates ALL
# adjudication-header tables (gate GP-03), so a malformed one can't hide behind
# a valid one after it.
MALFORMED_THEN_VALID = (
    "| finding | decision | action | verification |\n"
    "|---|---|---|---|\n"
    "| only three cells | accepted | x |\n"
    "\nsome prose between tables\n\n" + VALID
)

# P1: a bold/emphasis-wrapped decision value still validates.
BOLD_DECISION = (
    "| finding | decision | action | verification |\n"
    "|---|---|---|---|\n"
    "| x | **accepted** | fix the thing | check passes |\n"
)

# content: empty cells are rejected (merged from the gate validator).
EMPTY_ROW = (
    "| finding | decision | action | verification |\n"
    "|---|---|---|---|\n"
    "| | | | |\n"
)

# Chinese decision aliases are recognized.
CHINESE = VALID.replace("accepted", "采纳", 1)


def _ok(text: str) -> bool:
    return v.validate_text(text)[0]


def test_valid_passes() -> None:
    ok, issues, counts = v.validate_text(VALID)
    assert ok, issues
    assert counts == {"accepted": 1, "rejected": 1, "needs-user-decision": 1}, counts


def test_missing_separator_fails() -> None:
    ok, issues, _ = v.validate_text(NO_SEPARATOR)
    assert not ok and any("separator" in i for i in issues), issues


def test_inline_code_pipe_does_not_fracture() -> None:
    assert _ok(PIPED_CODE)


def test_escaped_pipe_does_not_fracture() -> None:
    assert _ok(ESCAPED_PIPE)


def test_malformed_header_table_now_fails() -> None:
    # was the skill validator's "skip malformed and use the valid one"; the
    # merged engine hard-fails the row-misaligned table (gate strictness).
    assert not _ok(MALFORMED_THEN_VALID)


def test_emphasis_decision_validates() -> None:
    assert _ok(BOLD_DECISION)
    assert v.decision_class("**accepted**") == "accepted"


def test_empty_cells_fail() -> None:
    assert not _ok(EMPTY_ROW)


def test_chinese_decision_recognized() -> None:
    assert _ok(CHINESE)


def test_split_cells_counts() -> None:
    assert len(v.split_cells("| a | b | c | d |")) == 4
    assert len(v.split_cells("| a | `x | y` | c | d |")) == 4
    assert len(v.split_cells("| a\\|b | c | d | e |")) == 4


TESTS = [
    test_valid_passes,
    test_missing_separator_fails,
    test_inline_code_pipe_does_not_fracture,
    test_escaped_pipe_does_not_fracture,
    test_malformed_header_table_now_fails,
    test_emphasis_decision_validates,
    test_empty_cells_fail,
    test_chinese_decision_recognized,
    test_split_cells_counts,
]


if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"OK: validate_audit_adjudication self-test passed ({len(TESTS)} tests)")
