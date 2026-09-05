"""Tests for scripts/validate_audit_adjudication.py — the merged adjudication
validator (gate content strictness + skill shape strictness, unified).

Locks:
- 469e66f5 #6 shape: extra / duplicate columns and row-cell mismatch hard-fail.
- ae917256: pipe-aware cell split, emphasis-wrapped decision, separator required.
- gate content: empty / placeholder cells rejected; needs-user-decision must
  name who and what is awaited.
- GP-03 validate-ALL adjudication-header tables — a clean decoy cannot mask a
  broken table after it; the former skill single-table "locate first / skip lax"
  behavior is now strict. Tables whose header is NOT the required set are ignored.
- real source line numbers in violation messages (15a637f2 #2).
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import validate_audit_adjudication as vaa  # noqa: E402


def _run_main(text: str) -> tuple[int, str]:
    """Pipe `text` through main() via stdin and capture stdout."""
    saved_argv, saved_stdin = sys.argv, sys.stdin
    out = io.StringIO()
    try:
        sys.argv = ["validate_audit_adjudication.py"]
        sys.stdin = io.StringIO(text)
        with redirect_stdout(out):
            rc = vaa.main()
    finally:
        sys.argv, sys.stdin = saved_argv, saved_stdin
    return rc, out.getvalue()


# ===== valid tables pass =====


def test_valid_minimal_passes() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n"
        "|---|---|---|---|\n| f1 | accepted | fix it | rerun tests |\n"
    )
    assert rc == 0, out
    assert "OK:" in out and "1 rows" in out


def test_valid_each_decision_counts() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| a | accepted | fix x | rerun |\n"
        "| b | rejected | out of scope, leave as-is | diff unchanged |\n"
        "| c | needs-user-decision | ask owner for deploy authorization | owner approval recorded |\n"
    )
    assert rc == 0, out
    assert "accepted=1" in out and "rejected=1" in out and "needs-user-decision=1" in out


# ===== 469e66f5 #6 shape strictness =====


def test_extra_column_rejected() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification | severity |\n"
        "|---|---|---|---|---|\n| f1 | accepted | fix | rerun | high |\n"
    )
    assert rc == 1 and "extra column" in out.lower(), out


def test_duplicate_header_rejected() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification | finding |\n"
        "|---|---|---|---|---|\n| f1 | accepted | fix | rerun | dup |\n"
    )
    assert rc == 1 and ("duplicate column" in out.lower() or "extra column" in out.lower()), out


def test_row_cell_mismatch_rejected() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | accepted | fix | rerun |\n| f2 | rejected | only-three-cells |\n"
    )
    assert rc == 1 and "cell count" in out.lower(), out


def test_missing_required_column_rejected() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action |\n|---|---|---|\n| f1 | accepted | fix |\n"
    )
    assert rc == 1, out


def test_unrecognized_decision_rejected() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | maybe | fix | rerun |\n"
    )
    assert rc == 1 and "decision" in out.lower(), out


# ===== ae917256: pipe-aware / emphasis / separator =====


def test_missing_separator_rejected() -> None:
    rc, out = _run_main(
        "| finding | decision | action | verification |\n| s | accepted | fix | check |\n"
    )
    assert rc == 1 and "separator" in out.lower(), out


def test_inline_code_pipe_does_not_fracture() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| flaky | accepted | run `cat x | grep y` | `a | b` exits 0 |\n"
    )
    assert rc == 0, out


def test_escaped_pipe_does_not_fracture() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| handles a\\|b | accepted | normalize input | exit 0 verified |\n"
    )
    assert rc == 0, out


def test_emphasis_decision_validates() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| x | **accepted** | fix the thing | check passes |\n"
    )
    assert rc == 0, out


# ===== gate content strictness (merged in) =====


def test_empty_or_placeholder_cell_rejected() -> None:
    # none / n/a are placeholders — a rejected row must still state a real
    # action and verification.
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | rejected | none | n/a |\n"
    )
    assert rc == 1, out


def test_needs_user_decision_without_detail_rejected() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | needs-user-decision | ask someone | record it |\n"
    )
    assert rc == 1 and "who and what" in out.lower(), out


def test_needs_user_decision_substring_false_accept_rejected() -> None:
    # CF-1 (#306): a needs-user-decision row that names no real actor must be
    # rejected even when its text embeds an actor marker inside a longer word —
    # "production" embeds "product", "username" embeds "user". Bare substring
    # matching false-accepted these; word-boundary matching does not.
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | needs-user-decision | wait for production deploy | production logs updated |\n"
    )
    assert rc == 1 and "who and what" in out.lower(), out


def test_needs_user_detail_word_boundary() -> None:
    # False-accepts removed: an actor/object marker embedded in a longer word
    # must not satisfy the check.
    assert vaa.needs_user_detail("wait for production deploy", "logs updated") is False
    assert vaa.needs_user_detail("update username setting", "config saved") is False
    # True-positives preserved (guards against over-correction into false-reject).
    assert vaa.needs_user_detail("ask Owner", "branch protection approval") is True
    assert vaa.needs_user_detail("escalate to the admin", "secret rotation approved") is True
    # CJK markers keep substring matching (no reliable Han word boundary).
    assert vaa.needs_user_detail("等负责人确认", "授权后记录") is True


def test_needs_user_detail_inflected_forms_still_match() -> None:
    # CF-1 fix must not over-correct (audit 2dd5bce5 f1/f2): plural/derived
    # actor and object forms the substring matcher accepted must still pass.
    assert vaa.needs_user_detail("ask the maintainers", "rotate credentials") is True
    assert vaa.needs_user_detail("ask Owner", "update permissions") is True
    assert vaa.needs_user_detail("owner approves deployment", "deployment logged") is True
    # An ASCII actor marker glued to CJK still matches (boundary is ASCII-only).
    assert vaa.needs_user_detail("等owner确认", "授权后记录") is True
    # The CF-1 false-accept stays closed (regression guard).
    assert vaa.needs_user_detail("wait for production deploy", "logs updated") is False


def test_needs_user_detail_cjk_false_friend() -> None:
    # CJK token-awareness (#306, residual of the 6/6 CF-1 finding): the actor
    # marker 人工 ("human/manual") is a misleading substring of 人工智能 ("AI"),
    # which names no human actor. Disambiguating the known false-friend compound
    # rejects the AI phrasing while preserving 人工 as a real human actor.
    assert vaa.needs_user_detail("等人工智能处理", "授权后记录") is False
    assert vaa.needs_user_detail("等人工确认", "授权后记录") is True
    # A standalone 人工 alongside the compound still names the actor.
    assert vaa.needs_user_detail("人工智能跑完后等人工复核", "授权后记录") is True
    # Unrelated CJK actors are unaffected (regression guard).
    assert vaa.needs_user_detail("等负责人确认", "授权后记录") is True
    # Boundary-join guard (audit 22e3e55d, 2/3 convergent): masking the compound
    # must NOT fuse its flanking chars into a spurious 人工 (人 + 人工智能 + 工).
    assert vaa.needs_user_detail("人人工智能工", "授权后记录") is False
    assert vaa.needs_user_detail("等人人工智能工作", "授权后记录") is False


def test_needs_user_detail_administrator_inflection() -> None:
    # Closed-lexicon false-reject (#306): #308's whole-word actor matching is too
    # strict for a legit inflection of an existing marker — "administrator" is the
    # formal form of the same actor as `admin`, but whole-word `admin` does not
    # fire inside it (vs the ASCII boundary). `administrator` is added to the
    # lexicon; the `s?` in the matcher covers the plural too.
    assert vaa.needs_user_detail("ask the administrator", "branch protection approval recorded") is True
    assert vaa.needs_user_detail("escalate to administrators", "secret rotation approved") is True
    # The CF-1 false-accept must stay closed: an object word must NOT satisfy the
    # actor test (production embeds no whole-word actor).
    assert vaa.needs_user_detail("wait for production deploy", "logs updated") is False
    # Dual-requirement still gates (audit 5d82757c c1): `administrator` present but
    # NO object marker is rejected — adding the actor marker does not bypass the
    # need to also name a concrete object. (Adjectival uses like "administrator
    # credential" stay True, identical to the pre-existing `admin` — a heuristic
    # property of every actor marker, not new to this slice.)
    assert vaa.needs_user_detail("update the administrator runbook", "runbook published") is False


def test_chinese_decision_accepted() -> None:
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | 采纳 | fix it | rerun |\n"
    )
    assert rc == 0, out


# ===== blank-line block-split silent row loss (#306, 790410fc follow-up) =====
# Decision-value-anchored orphan detection: a pipe row carrying a recognized
# decision value that sits OUTSIDE any header+separator table region is a row the
# collector drops silently. Robust against the heuristic edges two Standard audits
# (86f48f7b, bde372f9) found — see each test for the case it locks.

_TABLE = (
    "| finding | decision | action | verification |\n|---|---|---|---|\n"
    "| f1 | accepted | fix the thing | check passes |\n"
)


def test_blank_line_split_orphan_decision_row_rejected() -> None:
    # The reproduced bug: a stray blank line orphans a needs-user-decision row.
    rc, out = _run_main(
        _TABLE + "\n| f2 | needs-user-decision | wait for owner authorization | recorded |\n"
    )
    assert rc == 1 and "not inside a table" in out.lower(), out


def test_separator_first_orphan_block_rejected() -> None:
    # Audit bde372f9 / 86f48f7b f1: a headerless block that STARTS with a separator
    # then has a decision row — a naive "any separator excludes the block" rule
    # skipped it; the decision anchor still catches it.
    rc, out = _run_main(
        _TABLE + "\n|---|---|---|---|\n"
        "| f2 | needs-user-decision | wait for owner authorization | recorded |\n"
    )
    assert rc == 1 and "not inside a table" in out.lower(), out


def test_row_before_header_rejected() -> None:
    # Audit 86f48f7b f1 (claude): a decision row glued directly ABOVE the header
    # (no blank line) is dropped by the collector (rest = lines after the header).
    rc, out = _run_main(
        "| f0 | accepted | sneaked in | nowhere |\n" + _TABLE
    )
    assert rc == 1 and "not inside a table" in out.lower(), out


def test_all_dash_placeholder_does_not_mask_orphan() -> None:
    # Audit bde372f9 f1 (THE collision): `-` is a cell placeholder AND a row of
    # `| - | - | - | - |` is syntactically a separator. A naive structure check
    # would read it as a separator and treat the orphan decision row above it as a
    # header → silent drop. The decision anchor + "a header carries no decision
    # value" rule keep the orphan flagged.
    rc, out = _run_main(
        _TABLE + "\n| f2 | needs-user-decision | x | y |\n| - | - | - | - |\n"
    )
    assert rc == 1 and "not inside a table" in out.lower(), out


def test_caption_row_above_header_not_flagged() -> None:
    # Audit bde372f9 f3: a benign caption pipe row above the header carries no
    # decision value, so it is NOT a dropped adjudication row.
    rc, out = _run_main("| Adjudication Table |\n" + _TABLE)
    assert rc == 0, out


def test_metadata_table_with_decision_word_cell_not_flagged() -> None:
    # A real metadata table whose data cell happens to equal a decision value is a
    # data row INSIDE its own header+separator region — not a dropped row.
    rc, out = _run_main(
        _TABLE + "\n| feature | owner | status | date |\n|---|---|---|---|\n"
        "| login | bob | accepted | today |\n"
    )
    assert rc == 0, out


def test_closed_fenced_example_not_flagged() -> None:
    # Audit 86f48f7b/bde372f9 f2: decision rows inside a CLOSED fenced example are
    # literal content, not dropped rows — must not be flagged.
    rc, out = _run_main(
        _TABLE + "\nFormat example:\n\n```\n"
        "| f9 | accepted | demo | demo |\n| f8 | rejected | demo | demo |\n```\n"
    )
    assert rc == 0, out


def test_unclosed_fence_does_not_hide_orphan() -> None:
    # Audit bde372f9 f2 (claude/grok): an UNCLOSED fence must not blank the rest of
    # the document and hide a dropped decision row from the gate (gate-safe).
    rc, out = _run_main(
        _TABLE + "\n```\n"
        "| f2 | needs-user-decision | wait for owner authorization | recorded |\n"
    )
    assert rc == 1 and "not inside a table" in out.lower(), out


def test_blank_separated_real_tables_still_pass() -> None:
    # False-positive guard: two real adjudication tables separated by a blank line.
    rc, out = _run_main(
        _TABLE + "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f2 | rejected | out of scope, leave as-is | diff unchanged |\n"
    )
    assert rc == 0, out


def test_decision_row_under_wrong_width_table_rejected() -> None:
    # Deep audit 19728422 gpt-5.5 f1: a 4-col decision row glued under a 2-col
    # metadata table must NOT be absorbed as that table's data — the width match
    # keeps it loose, so the dropped needs-user-decision row is flagged.
    rc, out = _run_main(
        _TABLE + "\n| key | value |\n|---|---|\n"
        "| f2 | needs-user-decision | wait for owner authorization | verify |\n"
    )
    assert rc == 1 and "not inside a table" in out.lower(), out


def test_mismatched_separator_width_does_not_form_region() -> None:
    # Deep audit 19728422 claude f1a: a pseudo-header followed by a separator of a
    # DIFFERENT width is not a table (mirrors the collector), so a decision row
    # after it is not silently absorbed.
    rc, out = _run_main(
        _TABLE + "\n| f3 | x | a | v |\n|---|---|\n"
        "| f4 | needs-user-decision | wait for owner authorization | verify |\n"
    )
    assert rc == 1 and "not inside a table" in out.lower(), out


def test_over_indented_fence_does_not_hide_orphan() -> None:
    # Deep audit 19728422 deepseek f1 / gpt-5.5 f2: a 4-space-indented ``` is NOT a
    # GFM fence (only 0-3 spaces of indent open one), so it must not blank — and
    # thereby hide — a dropped decision row.
    rc, out = _run_main(
        _TABLE + "\n    ```\n"
        "    | f5 | needs-user-decision | wait for owner authorization | verify |\n"
        "    ```\n"
    )
    assert rc == 1 and "not inside a table" in out.lower(), out


# ----- no-leading-pipe GFM tables: fail-safe boundary (#306, Owner 2026-06-22) -----
# `_pipe_blocks` (shared by the collector AND this orphan check) only sees lines that
# START with `|`. A GFM table written WITHOUT a leading pipe is unrecognized.
# Confirmed intended-no-change: the pure case fails LOUD (locked below); the contrived
# mixed case is a documented latent limitation, pinned below so a future hardening
# flips it consciously rather than drifting silently.


def test_pure_no_leading_pipe_table_fails_loud() -> None:
    # The realistic loose-authoring case: a whole adjudication written as a GFM table
    # with no leading pipe is not recognized → fails loud ("no table"), never silently
    # passes. This is the fail-safe property the #306 confirm rests on.
    rc, out = _run_main(
        "finding | decision | action | verification\n"
        "--- | --- | --- | ---\n"
        "sql injection risk | accepted | parameterize query | grep no f-string\n"
    )
    assert rc == 1 and "no table" in out.lower(), out


def test_trailing_pipe_only_table_fails_loud() -> None:
    # Trailing-pipe-only GFM style (still no LEADING pipe) is likewise unrecognized
    # and fails loud — not silently accepted.
    rc, out = _run_main(
        "finding | decision | action | verification |\n"
        "--- | --- | --- | --- |\n"
        "sql injection risk | accepted | parameterize query | grep no f-string |\n"
    )
    assert rc == 1 and "no table" in out.lower(), out


def test_no_leading_pipe_table_mixed_known_limitation() -> None:
    # KNOWN LIMITATION, confirmed intended-no-change (#306, Owner 2026-06-22): a
    # no-leading-pipe GFM table mixed alongside a valid leading-pipe table is NOT
    # detected by the collector — its rows are silently dropped while the gate
    # passes. Contrived (real producers always emit leading pipes; the pure case
    # fails loud); hardening would widen the FP surface to any prose line with a
    # pipe.
    #
    # The dropped row carries an INVALID decision ("maybe later") so this test
    # genuinely DISCRIMINATES the boundary (audit 4b82cd39 f1): today the row is
    # dropped → rc==0; if the collector were ever hardened to recognize
    # no-leading-pipe tables, that invalid row would be flagged → rc==1 and this
    # assertion flips, forcing a conscious update. A *valid* row would pass under
    # both behaviors and pin nothing. The rc==0 documents the latent fail-open;
    # NOT an endorsement.
    rc, out = _run_main(
        _TABLE + "\nfinding | decision | action | verification\n"
        "--- | --- | --- | ---\n"
        "prod migration | maybe later | Owner must approve schema | signs off\n"
    )
    assert rc == 0, out


# ===== GP-03 validate-ALL (gate strictness supersedes skill single-table) =====


def test_decoy_first_table_does_not_mask_broken_second() -> None:
    text = (
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| ok | accepted | fix | rerun |\n\nprose\n\n"
        "| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| broken | accepted |  | done |\n"  # empty action in second table
    )
    rc, out = _run_main(text)
    assert rc == 1, out  # second table is validated too (GP-03)


def test_lax_header_table_now_fails() -> None:
    # was the skill validator's "skip the extra-column table, use the later
    # strict one"; the merged engine hard-fails the extra-column table.
    text = (
        "\n| finding | decision | action | verification | extra |\n|--|--|--|--|--|\n"
        "| x | accepted | fix | rerun | y |\n\nprose\n\n"
        "| finding | decision | action | verification |\n|--|--|--|--|\n"
        "| a | accepted | fix | rerun |\n"
    )
    rc, out = _run_main(text)
    assert rc == 1 and "extra column" in out.lower(), out


def test_non_required_header_table_ignored() -> None:
    # a later table whose header is NOT the required set is not an adjudication
    # table — ignored entirely, not a violation.
    text = (
        "\n| finding | decision | action | verification |\n|--|--|--|--|\n"
        "| a | accepted | fix | rerun |\n\nprose\n\n"
        "| col1 | col2 | col3 |\n|--|--|--|\n| x | y | z |\n"
    )
    rc, out = _run_main(text)
    assert rc == 0, out
    assert "1 rows" in out


# ===== module API (audit_to_adjudication_table converter depends on these) =====


def test_parse_tables_returns_all_tables() -> None:
    text = (
        "\n| finding | decision | action | verification |\n|--|--|--|--|\n"
        "| a | accepted | fix | rerun |\n\nprose\n\n"
        "| finding | decision | action | verification |\n|--|--|--|--|\n"
        "| b | rejected | out of scope | unchanged |\n"
    )
    tables = vaa.parse_tables(text)
    assert len(tables) == 2, tables
    assert vaa.REQUIRED_HEADERS == ("finding", "decision", "action", "verification")


def test_validate_text_signature() -> None:
    ok, issues, counts = vaa.validate_text(
        "| finding | decision | action | verification |\n|--|--|--|--|\n"
        "| a | accepted | fix | rerun |\n"
    )
    assert ok and issues == [] and counts["accepted"] == 1


def test_is_required_header_is_the_shared_recognition_predicate() -> None:
    # The single header-recognition predicate shared by _collect_tables,
    # _parse_tables_verbose, and _lax_lost_decision_rows (#306, audit 698fba2c):
    # the four REQUIRED columns in ANY order are a header; a superset (extra
    # columns) is still a header (the strict collector flags extras downstream);
    # a row missing a required column or a data row is NOT a header.
    assert vaa._is_required_header("| finding | decision | action | verification |")
    assert vaa._is_required_header("| decision | finding | verification | action |")
    assert vaa._is_required_header("| finding | decision | action | verification | note |")
    assert not vaa._is_required_header("| finding | decision | action |")
    assert not vaa._is_required_header("| f1 | accepted | did it | verified |")


# _lax_lost_decision_rows: the converter's orphan check (#306 converter sibling).
# Returns the gate orphan set restricted to HEADER-LESS blocks, so the LAX converter
# (which collects a required-superset header block even without a separator) does not
# false-fail a separator-less table (audit 82b1979f f2) while still catching a
# blank-line / headerless orphan the LAX parse silently discards.


def test_lax_lost_flags_headerless_blank_line_orphan() -> None:
    text = (
        "| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | accepted | x | y |\n"
        "\n"
        "| f2 | needs-user-decision | wait owner | recorded |\n"
    )
    lost = vaa._lax_lost_decision_rows(text)
    assert [lineno for lineno, _ in lost] == [5], lost


def test_lax_lost_ignores_separator_less_table() -> None:
    # Audit 82b1979f f2 (claude+grok convergent): a separator-less adjudication
    # table is collected by the LAX `_parse_tables_verbose` (no separator required),
    # so its header-bearing block must NOT be treated as a lost orphan — else the
    # converter false-fails an input it used to normalize.
    text = (
        "| finding | decision | action | verification |\n"
        "| f1 | accepted | did it | verified |\n"
    )
    assert vaa._lax_lost_decision_rows(text) == []


def test_lax_lost_ignores_metadata_table_decision_cell() -> None:
    # False-positive guard: a metadata table whose data cell equals a decision value
    # is inside its OWN header+separator region (already excluded by
    # _orphan_decision_rows), so the header-less intersection must not resurface it.
    text = (
        "| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | accepted | x | y |\n"
        "\n"
        "| feature | owner | status | date |\n|---|---|---|---|\n"
        "| login | bob | accepted | today |\n"
    )
    assert vaa._lax_lost_decision_rows(text) == []


def test_lax_lost_inherits_unrecognized_decision_residual() -> None:
    # Documented residual (inherited from _orphan_decision_rows's decision-value
    # anchor, #339): a header-less orphan whose decision is NOT a recognized token
    # (here "pending") is not flagged — it is not a valid adjudication decision
    # anyway, and broadening to it would re-open the structural-heuristic surface
    # #339 deliberately avoided. Pins the boundary so a future change flips it
    # consciously.
    text = (
        "| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | accepted | x | y |\n"
        "\n"
        "| f2 | pending | wait | recorded |\n"
    )
    assert vaa._lax_lost_decision_rows(text) == []


def test_lax_lost_flags_pre_header_stray_in_same_block() -> None:
    # Audit 698fba2c f1 (claude+gpt-5.5+grok convergent): a recognized decision row
    # glued ABOVE the header in the SAME contiguous pipe block is dropped by the LAX
    # parse (it collects only the header onward). A block-granularity exclusion would
    # miss it; the row-granularity capture (header line onward) keeps it flagged.
    text = (
        "| f0 | needs-user-decision | wait owner | rec |\n"
        "| finding | decision | action | verification |\n"
        "|---|---|---|---|\n"
        "| f1 | accepted | x | y |\n"
    )
    assert [lineno for lineno, _ in vaa._lax_lost_decision_rows(text)] == [1], text


def test_lax_lost_line_numbers_aligned_through_closed_fence() -> None:
    # Audit 698fba2c f3: the header-less/pre-header line map and the orphan set both
    # run through _strip_closed_fences + _pipe_blocks, so a closed fenced example
    # before the orphan must not shift the reported absolute line number.
    text = "\n".join([
        "Notes:",                                          # 1
        "",                                                # 2
        "```",                                             # 3
        "| x | accepted | demo | demo |",                  # 4 (inside fence)
        "```",                                             # 5
        "",                                                # 6
        "| finding | decision | action | verification |",  # 7
        "|---|---|---|---|",                               # 8
        "| f1 | accepted | x | y |",                       # 9
        "",                                                # 10
        "| f2 | needs-user-decision | wait | rec |",       # 11 (orphan)
        "",
    ])
    assert [lineno for lineno, _ in vaa._lax_lost_decision_rows(text)] == [11], text


# ===== real source line numbers (15a637f2 #2) =====


def test_violation_uses_real_source_line_number() -> None:
    text = "\n".join([
        "# preamble",
        "",
        "Some text.",
        "",
        "| finding | decision | action | verification | extra |",
        "|--|--|--|--|--|",
        "| a | accepted | fix | rerun | y |",
        "",
    ])
    rc, out = _run_main(text)
    assert rc == 1
    assert "line 5" in out, out  # header is on real file line 5, not filtered index 1


# ===== #306 engine heuristic hardening =====


def test_emphasis_decision_with_inner_spaces_passes() -> None:
    # normalize() must strip spaces exposed after removing emphasis markers, so a
    # bold decision written with inner spaces still matches the vocabulary.
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| f1 | ** accepted ** | fix it | rerun |\n"
    )
    assert rc == 0, out


def test_colon_only_separator_row_rejected() -> None:
    # A colon-only row (|:|:|:|:|) is not a valid GFM delimiter (no hyphen); it
    # must not be accepted as the separator row.
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n"
        "|:|:|:|:|\n| f1 | accepted | fix | rerun |\n"
    )
    assert rc == 1, out  # no valid separator -> table rejected


def test_inline_angle_bracket_content_allowed() -> None:
    # filled() must treat only a WHOLE-cell <...> token as a placeholder; inline
    # angle-bracket content inside a real sentence is legitimate.
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| missing <html> escaping in output | accepted | escape the tag | grep shows escaped output |\n"
    )
    assert rc == 0, out


def test_whole_cell_angle_bracket_placeholder_still_rejected() -> None:
    # A cell that IS a lone <...> token is still an unfilled placeholder.
    rc, out = _run_main(
        "\n| finding | decision | action | verification |\n|---|---|---|---|\n"
        "| <finding> | accepted | fix it | rerun |\n"
    )
    assert rc == 1, out


def test_split_cells_escaped_pipe_inside_code_span_preserved() -> None:
    # qwen f3 (#306): inside a code span, `\|` is literal per markdown code-span
    # semantics; split_cells must not strip the backslash.
    cells = vaa.split_cells(r"| a | `x \| y` |")
    assert len(cells) == 2
    assert r"\|" in cells[1]


def test_split_cells_double_backtick_span_does_not_fracture() -> None:
    # gpt f5 / gemini f3 (#306): a double-backtick code span embedding a pipe must
    # stay one cell — the inner pipe is in-code, not a delimiter.
    cells = vaa.split_cells("| a | ``cmd | x`` |")
    assert len(cells) == 2
    # Single-backtick spans (the common case) must keep working unchanged.
    assert len(vaa.split_cells("| a | `cmd | x` |")) == 2
