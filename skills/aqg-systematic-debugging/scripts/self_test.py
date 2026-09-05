#!/usr/bin/env python3
"""Self-test for debug_case.py.

Locks the Batch-1 dual-audit fixes (audit b19a20d9):
- C1 section parser: only known required headings outside fenced blocks act as
  delimiters; duplicate required headings fail; a placeholder under a custom
  sub-heading is no longer hidden (false-PASS) and a required heading inside a
  fence does not satisfy validation.
- C2 root-cause/fix coupling: whole-value pending markers, not loose substring
  (no false-FAIL when a confirmed root cause quotes the phrase).
- C3 placeholder marker: only a bare standalone marker line counts, not the
  word inside legitimate prose (no false-FAIL).
- P1 validate() path guard: missing / directory / unreadable path returns the
  failed code, not a raw traceback.
"""

from __future__ import annotations

import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import debug_case


FILLED = """# Debug Case: sample

## Symptom
Command failed on 2026-05-01.

## Fresh Reproduction
`pytest tests/sample.py` exited 1 with assertion failure.

## Evidence
- `tests/sample.py` line 10 asserts `x < 10`.

## Hypotheses
| hypothesis | evidence for | evidence against | test | result |
|---|---|---|---|---|
| assertion is wrong | failing line | none | `pytest tests/sample.py` | fail |

## Root Cause
The expected value in the test is stale.

## Fix
Update the expected value after confirming the contract.

## Verification
- original symptom: `pytest tests/sample.py` -> pass
- regression scope (stakes-scaled): trivial (one stale assertion, single file) -> one check
- regression check(s): `pytest tests/sample.py` -> pass

## Boundaries
- production writes/deploys/restarts: none
- secrets/raw/private data: none
- Owner/user decision needed: none
"""


def _validate_text(tmp: Path, text: str, name: str = "case.md") -> int:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    with redirect_stdout(StringIO()):
        return debug_case.validate(p)


# --------------------------------------------------------------------------
# create() + skeleton + slug
# --------------------------------------------------------------------------
def test_create_skeleton_is_unfilled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        assert debug_case.create("Sample Failure", out) == 0
        created = next(out.glob("*.md"))
        with redirect_stdout(StringIO()):
            assert debug_case.validate(created) == 1  # skeleton has fill markers


def test_skeleton_prompts_stakes_scaled_regression_scope() -> None:
    # step-7 stakes-aware: the Verification skeleton must prompt a regression SCOPE
    # scaled to blast radius (not just "one check"), keep it FOCUSED — explicitly NOT
    # the full suite (CI's / the project gate's job) — AND carry a __FILL_SCOPE__
    # marker (the forcing function). Scope to the Verification section so decoy text in
    # another section can't satisfy it (audit ea851f63 claude-f1 / grok-f1).
    verif = (
        debug_case.TEMPLATE.split("## Verification", 1)[1].split("## Boundaries", 1)[0].lower()
    )
    assert "regression scope" in verif, debug_case.TEMPLATE
    assert "blast-radius" in verif or "blast radius" in verif, debug_case.TEMPLATE
    assert "not the full suite" in verif, debug_case.TEMPLATE
    assert "__fill_scope__" in verif, debug_case.TEMPLATE  # forcing function


def test_unfilled_scope_line_fails_validate() -> None:
    # Forcing function has TEETH (audit ea851f63 claude-f1): an otherwise-complete case
    # whose regression-scope line is left unfilled (still __FILL_SCOPE__) must FAIL
    # validate — validate flags any "__FILL_" left in a required section (debug_case.py
    # line ~207, generic substring check), so the stakes prompt is enforced, not prose.
    assert "-> one check" in FILLED  # guard: FILLED still carries the fill-able scope value
    unfilled = FILLED.replace(
        "trivial (one stale assertion, single file) -> one check", "__FILL_SCOPE__"
    )
    assert unfilled != FILLED  # the replace actually fired (FILLED did not drift)
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), unfilled) == 1


def test_skill_md_step7_is_stakes_aware() -> None:
    # Couple SKILL.md step 7 to the skeleton (audit ea851f63 grok-f2): the prose and the
    # skeleton must not drift — step 7 must carry the same stakes-aware markers, else a
    # revert to "at least one regression check" on one side passes while the other holds.
    skill_md = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(
        encoding="utf-8"
    ).lower()
    assert "blast radius" in skill_md, "SKILL.md step 7 lost the blast-radius scaling"
    assert "not the full suite" in skill_md, "SKILL.md step 7 lost the not-full-suite carve-out"


def test_create_collision_suffix() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        assert debug_case.create("Same Title", out) == 0
        assert debug_case.create("Same Title", out) == 0
        assert len(list(out.glob("*.md"))) == 2  # second got a -2 suffix


def test_slugify_unicode_and_empty() -> None:
    assert debug_case.slugify("") == "debug-case"
    assert debug_case.slugify("   ") == "debug-case"
    assert debug_case.slugify("Hello World!") == "hello-world"


def test_filled_case_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), FILLED) == 0


# --------------------------------------------------------------------------
# C1 — section parser robustness
# --------------------------------------------------------------------------
def test_custom_subheading_does_not_hide_placeholder() -> None:
    # OLD bug: a custom `## Notes` heading truncated Symptom, so the marker
    # after it escaped into a non-required "Notes" span (false-PASS).
    truncation = FILLED.replace(
        "## Symptom\nCommand failed on 2026-05-01.\n",
        "## Symptom\nCommand failed on 2026-05-01.\n\n## Notes\n__FILL_DETAIL__\n",
    )
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), truncation) == 1


def test_required_heading_in_fence_does_not_satisfy() -> None:
    fenced = FILLED.replace(
        "## Boundaries\n- production writes/deploys/restarts: none\n"
        "- secrets/raw/private data: none\n- Owner/user decision needed: none\n",
        "```\n## Boundaries\nnone none none\n```\n",
    )
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), fenced) == 1  # real Boundaries missing


def test_duplicate_required_section_fails() -> None:
    dup = FILLED + "\n## Symptom\nduplicate symptom body\n"
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), dup) == 1


# --------------------------------------------------------------------------
# C2 — root-cause / fix coupling (whole-value markers)
# --------------------------------------------------------------------------
def test_confirmed_root_cause_quoting_phrase_passes() -> None:
    confirmed = FILLED.replace(
        "The expected value in the test is stale.",
        "The failure happened because downstream state was not confirmed yet at call time.",
    )
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), confirmed) == 0


def test_pending_root_cause_with_concrete_fix_fails() -> None:
    pending = FILLED.replace("The expected value in the test is stale.", "unknown")
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), pending) == 1


def test_pending_root_cause_with_pending_fix_passes() -> None:
    both = FILLED.replace(
        "The expected value in the test is stale.", "not confirmed yet"
    ).replace(
        "Update the expected value after confirming the contract.", "pending root cause"
    )
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), both) == 0


# --------------------------------------------------------------------------
# C3 — placeholder marker only as a standalone line
# --------------------------------------------------------------------------
def test_marker_word_in_prose_passes() -> None:
    prose = FILLED.replace(
        "- `tests/sample.py` line 10 asserts `x < 10`.",
        "- removed the outdated TODO comment in `tests/sample.py` line 10.",
    )
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), prose) == 0


def test_bare_marker_line_fails() -> None:
    bare = FILLED.replace("- `tests/sample.py` line 10 asserts `x < 10`.", "TODO")
    with tempfile.TemporaryDirectory() as tmp:
        assert _validate_text(Path(tmp), bare) == 1


# --------------------------------------------------------------------------
# P1 — validate() path guard (no traceback)
# --------------------------------------------------------------------------
def test_missing_path_returns_failed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with redirect_stdout(StringIO()):
            assert debug_case.validate(Path(tmp) / "nope.md") == 1


def test_directory_path_returns_failed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "adir"
        d.mkdir()
        with redirect_stdout(StringIO()):
            assert debug_case.validate(d) == 1


TESTS = [
    test_create_skeleton_is_unfilled,
    test_skeleton_prompts_stakes_scaled_regression_scope,
    test_unfilled_scope_line_fails_validate,
    test_skill_md_step7_is_stakes_aware,
    test_create_collision_suffix,
    test_slugify_unicode_and_empty,
    test_filled_case_passes,
    test_custom_subheading_does_not_hide_placeholder,
    test_required_heading_in_fence_does_not_satisfy,
    test_duplicate_required_section_fails,
    test_confirmed_root_cause_quoting_phrase_passes,
    test_pending_root_cause_with_concrete_fix_fails,
    test_pending_root_cause_with_pending_fix_passes,
    test_marker_word_in_prose_passes,
    test_bare_marker_line_fails,
    test_missing_path_returns_failed,
    test_directory_path_returns_failed,
]


if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"OK: debug_case self-test passed ({len(TESTS)} tests)")
