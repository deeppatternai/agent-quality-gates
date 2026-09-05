"""L3-6c — gate-checker LOW robustness (manual-only DX validators, non-security).

Three asymmetry / bypass gaps in the manual adjudication + closeout validators:

- GP-01 (validate_audit_adjudication.OBJECT_MARKERS): `什么` ("what") was an
  object marker. needs_user_detail() requires a needs-user-decision row to name
  BOTH an actor AND a concrete object. `什么` is an interrogative/vague token
  (= "unspecified object"), so a vague CJK row ("用户要想清楚要什么": actor +
  `什么` only, no concrete object) passed who-and-what, while the English
  equivalent ("user must figure out what they want": no object marker) failed.
  Asymmetric and contrary to the "name the concrete what" intent. Fix: drop
  `什么` (restore symmetry, keep the gate strict) — NOT add "what" (which would
  loosen a deliberately strict gate so the English vague row passes too).

- GP-03 (validate_audit_adjudication.validate_text): validated only tables[0].
  A clean all-accepted decoy first table masked a broken real table after it.
  Fix: validate ALL adjudication tables; any bad row in any table fails.

- GP-07 (check_evidence_closeout.is_placeholder): a single short token ("x", "1",
  "ok") passed as substantive evidence. Fix: a single token under 3 chars is a
  placeholder. Must NOT regress legitimate short values ("none" for remaining
  blockers — note this file's EMPTY_VALUES intentionally omits "none").

Probe-proven RED on HEAD baseline (49bd007): /tmp/probe_l3_6c.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import validate_audit_adjudication as adj  # noqa: E402
import check_evidence_closeout as evc  # noqa: E402


# ----- GP-01: CJK `什么` who-and-what asymmetry --------------------------


class TestNeedsUserDetailSymmetry:
    def test_what_is_not_an_object_marker(self) -> None:
        assert "什么" not in adj.OBJECT_MARKERS

    def test_vague_cjk_row_fails_like_english_equivalent(self) -> None:
        # Both rows are equally vague: an actor + an interrogative "what",
        # naming no concrete object. Both must fail who-and-what symmetrically.
        cjk = adj.needs_user_detail("用户要想清楚要什么", "记录结果")
        eng = adj.needs_user_detail("user must figure out what they want", "record the outcome")
        assert cjk is False, "vague CJK row must not pass who-and-what (was True via `什么`)"
        assert eng is False, "vague English row must not pass who-and-what"

    def test_concrete_cjk_needs_user_decision_still_passes(self) -> None:
        # Naming a concrete object (授权) + actor (用户/负责人) must still pass.
        assert adj.needs_user_detail("等用户授权后再改", "负责人审批记录") is True

    def test_concrete_english_needs_user_decision_still_passes(self) -> None:
        assert (
            adj.needs_user_detail(
                "wait for Owner branch protection approval",
                "Owner approval recorded before change",
            )
            is True
        )


# ----- GP-03: validate_text must check ALL tables ------------------------

_DECOY_THEN_PLACEHOLDER = """# Adjudication

| finding | decision | action | verification |
|---|---|---|---|
| harmless | accepted | did the safe thing | `pytest` exits 0 |

Prose between two tables.

| finding | decision | action | verification |
|---|---|---|---|
| real problem | accepted | TODO | - |
"""

_DECOY_THEN_ILLEGAL_DECISION = """# Adjudication

| finding | decision | action | verification |
|---|---|---|---|
| harmless | accepted | did the safe thing | `pytest` exits 0 |

| finding | decision | action | verification |
|---|---|---|---|
| real problem | approved | shipped it | observed in prod |
"""

_SINGLE_CLEAN_TABLE = """# Adjudication

| finding | decision | action | verification |
|---|---|---|---|
| stale gate | accepted | add strict script | `python3 scripts/check.py` exits 0 |
"""

_TWO_CLEAN_TABLES = """# Adjudication

| finding | decision | action | verification |
|---|---|---|---|
| stale gate | accepted | add strict script | `python3 scripts/check.py` exits 0 |

| finding | decision | action | verification |
|---|---|---|---|
| broad rewrite | rejected | no action; scope too broad | diff remains unchanged |
"""


class TestValidateAllTables:
    def test_decoy_first_table_does_not_mask_placeholder_in_second(self) -> None:
        ok, issues, _ = adj.validate_text(_DECOY_THEN_PLACEHOLDER)
        assert ok is False, "a broken 2nd table must fail despite a clean 1st"
        joined = " ".join(issues).lower()
        assert "action" in joined or "verification" in joined, issues

    def test_decoy_first_table_does_not_mask_illegal_decision_in_second(self) -> None:
        ok, issues, _ = adj.validate_text(_DECOY_THEN_ILLEGAL_DECISION)
        assert ok is False, "illegal decision in a later table must fail"
        assert any("decision must be" in i for i in issues), issues

    def test_single_clean_table_still_passes(self) -> None:
        ok, issues, counts = adj.validate_text(_SINGLE_CLEAN_TABLE)
        assert ok is True, issues
        assert counts["accepted"] == 1, counts

    def test_single_table_issue_message_format_unchanged(self) -> None:
        # Backward compat: single-table issues keep the bare "row N:" prefix.
        bad = _SINGLE_CLEAN_TABLE.replace("add strict script", "TODO")
        ok, issues, _ = adj.validate_text(bad)
        assert ok is False
        assert any(i.startswith("row ") for i in issues), issues

    def test_counts_accumulate_across_all_tables(self) -> None:
        ok, issues, counts = adj.validate_text(_TWO_CLEAN_TABLES)
        assert ok is True, issues
        assert counts["accepted"] == 1 and counts["rejected"] == 1, counts


# ----- GP-07: a single short token is a placeholder ----------------------


class TestIsPlaceholderShortToken:
    @pytest.mark.parametrize("value", ["x", "1", "ok", "no", "a"])
    def test_single_short_token_is_placeholder(self, value: str) -> None:
        assert evc.is_placeholder(value) is True, f"{value!r} should be a placeholder"

    @pytest.mark.parametrize("value", ["x/y", "o-k", "1/1", "n/a"])
    def test_punctuation_delimited_short_fragment_is_placeholder(self, value: str) -> None:
        # f2 (audit 6012a42d): normalize() turns `/` and `-` into spaces, so a
        # single-token check missed these; counting compact alphanumerics fixes it.
        # `n/a` is correctly caught too (it is an empty-intent value).
        assert evc.is_placeholder(value) is True, f"{value!r} should be a placeholder"

    @pytest.mark.parametrize(
        "value", ["none", "done", "yes", "scripts and docs updated", "PR body updated"]
    )
    def test_substantive_value_not_placeholder(self, value: str) -> None:
        assert evc.is_placeholder(value) is False, f"{value!r} should NOT be a placeholder"

    def test_remaining_blockers_none_still_valid_end_to_end(self) -> None:
        # 'none' is the canonical legitimate remaining-blockers value.
        doc = """# PR

## Evidence Block

| item | evidence |
|---|---|
| scope completed | scripts and docs updated |
| verification run | `python3 scripts/check_evidence_closeout.py --self-test` -> exit 0 |
| audit adjudicated | local review completed; no external audit |
| durable state updated | PR body updated with evidence |
| production boundary | no production, secrets, or raw data touched |
| remaining blockers | none |
"""
        ok, issues, _ = evc.validate_text(doc)
        assert ok is True, issues

    def test_all_single_char_evidence_fails_completeness(self) -> None:
        doc = """# PR

## Evidence Block

| item | evidence |
|---|---|
| scope completed | x |
| verification run | x |
| audit adjudicated | x |
| durable state updated | x |
| production boundary | x |
| remaining blockers | x |
"""
        ok, issues, _ = evc.validate_text(doc)
        assert ok is False, "all-single-char evidence must fail completeness"
        assert all("placeholder" in i for i in issues), issues


class TestEvidenceHeadingRequired:
    """WS-8 P2-5c: the evidence gate must require a genuine evidence heading. A
    headingless doc that merely scatters the six required labels as colon-items
    must not clear the gate (the old fallback treated the whole doc as evidence)."""

    _HEADINGLESS_BUT_LABELLED = """# Random Notes

Some intro prose unrelated to closeout.

scope completed: refactored the widget
verification run: pytest passed locally
audit adjudicated: no external audit; local review
durable state updated: PR body updated
production boundary: no production, secrets, or raw data touched
remaining blockers: none

More unrelated prose.
"""

    _WITH_HEADING = """# PR

## Evidence Block

| item | evidence |
|---|---|
| scope completed | scripts and docs updated |
| verification run | `python3 scripts/check_evidence_closeout.py --self-test` -> exit 0 |
| audit adjudicated | local review completed; no external audit |
| durable state updated | PR body updated with evidence |
| production boundary | no production, secrets, or raw data touched |
| remaining blockers | none |
"""

    def test_headingless_doc_with_all_labels_fails(self) -> None:
        ok, issues, _ = evc.validate_text(self._HEADINGLESS_BUT_LABELLED)
        assert ok is False, "headingless doc must not clear the evidence gate"
        assert any("missing evidence block heading" in i.lower() for i in issues), issues

    def test_proper_heading_still_passes(self) -> None:
        ok, issues, _ = evc.validate_text(self._WITH_HEADING)
        assert ok is True, issues

    def test_has_evidence_heading_predicate(self) -> None:
        assert evc.has_evidence_heading(self._WITH_HEADING) is True
        assert evc.has_evidence_heading(self._HEADINGLESS_BUT_LABELLED) is False

    @pytest.mark.parametrize("heading", ["## Evidence Block", "## Closeout", "## 证据", "### Evidence"])
    def test_synonym_headings_recognized(self, heading: str) -> None:
        # 'evidence' / 'closeout' / '证据' branches must all satisfy the requirement.
        assert evc.has_evidence_heading(f"# PR\n\n{heading}\n\nbody\n") is True

    def test_fallback_extraction_survives_without_heading(self) -> None:
        # The fallback is deliberately kept for lenient item *extraction* even though
        # it can no longer produce a PASS. Lock that contract so a maintainer who
        # treats the fallback as dead code cannot silently break boundary/diagnostic
        # extraction with the suite still green.
        blocks = evc.evidence_blocks(self._HEADINGLESS_BUT_LABELLED)
        assert blocks, "fallback must still yield a block for headingless extraction"
        _, _, found = evc.validate_text(self._HEADINGLESS_BUT_LABELLED)
        assert found.get("scope completed"), found
        assert found.get("production boundary"), found
