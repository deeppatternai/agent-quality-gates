"""#234 / L3-6c audit f1 — convert() must render ALL validated adjudication tables.

Bug (pre-fix, scripts/audit_to_adjudication_table.py convert(), lines 43-51):
convert() returned ``render_table()`` of the FIRST adjudication-shaped table
only. GP-03 hardened ``validate_text`` to treat ALL adjudication tables as
authoritative (the all-tables contract), so a document with two *clean*
adjudication tables validated OK, yet convert() silently dropped every row after
the first table.

Fix (design call: option 1, merge — see issue #234): convert() merges rows from
every validated adjudication table into one normalized "## Audit Adjudication"
table (lossless), aligning convert with validate_text's all-tables contract.
Option 2 ("reject >1 table as ambiguous") was rejected because real
multi-adjudication-table docs already exist in-repo (one carries 3) and the
two-round dual-audit workflow legitimately produces them.

Single-table output stays byte-identical (back-compat). Metadata tables (headers
not a superset of REQUIRED_HEADERS) are still skipped.

convert() is a pure ``str -> str`` function; these tests call it directly with
in-memory markdown (no temp file), so the assertions cannot be polluted by
pytest tmp_path test-name injection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import audit_to_adjudication_table as conv  # noqa: E402
import validate_audit_adjudication as adj  # noqa: E402


# Two CLEAN adjudication tables (both validate OK): accepted + rejected, every
# cell filled, no needs-user-decision row (so who/what detail is not required).
TWO_CLEAN_TABLES = """# Audit Round 1

| finding | decision | action | verification |
|---|---|---|---|
| ROW-ALPHA-first-table | accepted | applied the alpha fix | pytest alpha suite green |

# Audit Round 2

| finding | decision | action | verification |
|---|---|---|---|
| ROW-BETA-second-table | rejected | left out of scope for this slice | behavior left unchanged |
"""

SINGLE_TABLE = """# Audit

| finding | decision | action | verification |
|---|---|---|---|
| ROW-ALPHA-first-table | accepted | applied the alpha fix | pytest alpha suite green |
"""

METADATA_BEFORE = """# Audit

| metadata | value |
|---|---|
| reviewer | fixture-only |

| finding | decision | action | verification |
|---|---|---|---|
| ROW-GAMMA-real-table | accepted | did the work | verified locally |
"""

INVALID_DECISION = """# Audit

| finding | decision | action | verification |
|---|---|---|---|
| ROW-DELTA | maybe | did something | checked it |
"""

METADATA_ONLY = """# Audit

| metadata | value |
|---|---|
| reviewer | fixture-only |
"""

# f1 (audit 3f0f4e86): first table has reordered required headers + an extra
# (non-required) column; second table is canonical order. Exercises per-table
# header projection — each table indexes by its OWN headers.
HETEROGENEOUS_TABLES = """# Audit A

| decision | finding | extra | action | verification |
|---|---|---|---|---|
| accepted | REORDER-finding | ignore-me | reorder action done | reorder verified |

# Audit B

| finding | decision | action | verification |
|---|---|---|---|
| CANON-finding | rejected | canon action | canon verified |
"""


def test_renders_rows_from_all_validated_tables():
    """RED pre-fix: the second clean table's row was silently dropped."""
    out = conv.convert(TWO_CLEAN_TABLES)
    assert "ROW-ALPHA-first-table" in out
    assert "ROW-BETA-second-table" in out  # dropped before the fix
    # order preserved: first table's rows precede the second table's rows
    assert out.index("ROW-ALPHA-first-table") < out.index("ROW-BETA-second-table")


def test_merged_output_revalidates_with_all_decision_counts():
    """RED pre-fix: only the first table's `accepted` was counted.

    Re-validating convert()'s own output is the strongest all-tables invariant:
    the merged normalized table must carry BOTH decisions (accepted + rejected).
    """
    out = conv.convert(TWO_CLEAN_TABLES)
    ok, issues, counts = adj.validate_text(out)
    assert ok and not issues, issues
    assert counts["accepted"] == 1
    assert counts["rejected"] == 1  # would be 0 pre-fix (second table dropped)
    assert counts["needs-user-decision"] == 0


def test_single_table_output_canonical_and_revalidates():
    """No-regression: a single adjudication table renders the canonical form."""
    out = conv.convert(SINGLE_TABLE)
    assert out.startswith("## Audit Adjudication")
    assert "| finding | decision | action | verification |" in out
    assert "ROW-ALPHA-first-table" in out
    ok, _issues, counts = adj.validate_text(out)
    assert ok and counts["accepted"] == 1


def test_metadata_table_still_skipped():
    """No-regression: a metadata table before the adjudication table is excluded."""
    out = conv.convert(METADATA_BEFORE)
    assert "ROW-GAMMA-real-table" in out
    assert "reviewer" not in out  # metadata table must not bleed into the output
    assert adj.validate_text(out)[0] is True


def test_invalid_table_still_raises():
    """No-regression: a non-canonical decision keeps failing the validation gate."""
    with pytest.raises(ValueError):
        conv.convert(INVALID_DECISION)


def test_metadata_only_input_raises():
    """No-regression: input with no adjudication-shaped table raises ValueError."""
    with pytest.raises(ValueError):
        conv.convert(METADATA_ONLY)


def test_merge_projects_reordered_headers_and_extra_columns():
    """f1 (audit 3f0f4e86): a table with reordered required headers and an extra
    column must still project each cell under the canonical column order, and the
    extra column is dropped. (Also RED pre-fix: the second canonical table was
    silently dropped.)
    """
    out = conv.convert(HETEROGENEOUS_TABLES)
    assert "| REORDER-finding | accepted | reorder action done | reorder verified |" in out
    assert "| CANON-finding | rejected | canon action | canon verified |" in out
    assert "ignore-me" not in out  # the extra (non-required) column is not rendered
    ok, _issues, counts = adj.validate_text(out)
    assert ok and counts["accepted"] == 1 and counts["rejected"] == 1


def test_single_table_output_is_byte_identical():
    """f1 (audit 3f0f4e86): lock single-table back-compat at the byte level so a
    separator / trailing-newline regression would be caught.
    """
    expected = (
        "## Audit Adjudication\n"
        "\n"
        "| finding | decision | action | verification |\n"
        "|---|---|---|---|\n"
        "| ROW-ALPHA-first-table | accepted | applied the alpha fix | pytest alpha suite green |\n"
    )
    assert conv.convert(SINGLE_TABLE) == expected


# ===== #306 converter hardening (CF-2/CF-3/CF-4) =====


def test_convert_rejects_duplicate_required_column() -> None:
    # CF-4 (#306): a table with a duplicate required column is ambiguous —
    # render_table would silently project only the first occurrence, dropping a
    # contradictory second column. convert() must fail closed instead.
    text = (
        "| finding | decision | decision | action | verification |\n"
        "|---|---|---|---|---|\n"
        "| f | accepted | rejected | do it | checked |\n"
    )
    with pytest.raises(ValueError):
        conv.convert(text)


def test_convert_rejects_silently_dropped_row() -> None:
    # CF-3 (#306): a data row whose cell count != header width is silently
    # dropped by the LAX parse; convert() would then ship a table missing a real
    # adjudication row. Fail closed instead of losing the finding.
    text = (
        "| finding | decision | action | verification |\n"
        "|---|---|---|---|\n"
        "| good | accepted | did it | verified |\n"
        "| malformed row with too few cells | rejected |\n"
    )
    with pytest.raises(ValueError):
        conv.convert(text)


def test_convert_rejects_blank_line_orphan_row() -> None:
    # #306 converter sibling: a stray blank line orphans a tail decision row into a
    # HEADERLESS pipe block. _parse_tables_verbose never finds a header for that
    # block, so the row is lost WITHOUT being counted as a dropped row (a different
    # mechanism than CF-3's cell-count-mismatch drop) — convert() would ship a table
    # missing a real needs-user-decision finding. Fail closed instead, reusing the
    # gate's decision-value-anchored orphan detector (#339).
    text = (
        "| finding | decision | action | verification |\n"
        "|---|---|---|---|\n"
        "| f1 | accepted | fix the thing | check passes |\n"
        "\n"
        "| f2 | needs-user-decision | wait for owner authorization | recorded |\n"
    )
    with pytest.raises(ValueError):
        conv.convert(text)


def test_convert_clean_blank_separated_tables_still_convert() -> None:
    # False-positive guard for the orphan check: two COMPLETE tables separated by a
    # blank line (each with its own header+separator) are not orphans — convert()
    # must still merge both rows, not fail closed.
    out = conv.convert(TWO_CLEAN_TABLES)
    assert "ROW-ALPHA-first-table" in out and "ROW-BETA-second-table" in out


def test_convert_separator_less_table_still_converts() -> None:
    # Audit 82b1979f f2 (claude+grok convergent) regression guard: the LAX parse
    # collects a required-superset header block even WITHOUT a separator row, so a
    # separator-less table is an input convert() used to normalize. The orphan check
    # must NOT false-fail it — _lax_lost_decision_rows restricts to header-LESS
    # blocks, and this block has a header.
    text = (
        "| finding | decision | action | verification |\n"
        "| ROW-SEP-LESS | accepted | did it | verified |\n"
    )
    out = conv.convert(text)
    assert "ROW-SEP-LESS" in out
    ok, _issues, _counts = adj.validate_text(out)
    assert ok  # the rendered canonical output (with a separator) passes the gate


def test_convert_rejects_pre_header_stray_row() -> None:
    # Audit 698fba2c f1 (claude+gpt-5.5+grok convergent): a recognized decision row
    # glued ABOVE the header in the SAME pipe block is dropped by the LAX parse
    # (which collects only the header onward). convert() must fail closed, not ship a
    # table missing the stray finding.
    text = (
        "| STRAY-PRE-HEADER | needs-user-decision | wait owner | recorded |\n"
        "| finding | decision | action | verification |\n"
        "|---|---|---|---|\n"
        "| f1 | accepted | did it | verified |\n"
    )
    with pytest.raises(ValueError):
        conv.convert(text)


def test_convert_preserves_pipe_in_cell() -> None:
    # CF-2 (#306): a shell pipeline written as a whole-cell code span must
    # survive normalization as an escaped pipe, not be mangled into "/".
    text = (
        "| finding | decision | action | verification |\n"
        "|---|---|---|---|\n"
        "| f | accepted | rerun the gate | `python3 a.py | tee log` |\n"
    )
    rendered = conv.convert(text)
    assert r"python3 a.py \| tee log" in rendered  # escaped, not "python3 a.py / tee log"
    assert "python3 a.py / tee log" not in rendered
    ok, _issues, _counts = adj.validate_text(rendered)
    assert ok  # the escaped pipe round-trips through split_cells under strict re-validation


def test_convert_preserves_pipe_in_midcell_code_span() -> None:
    # CF-2 follow-up (audit 790410fc): a pipe inside a code span that is NOT the
    # whole cell must also survive. clean_cell must be code-span aware — escaping
    # an in-span pipe to `\|` would leave a spurious backslash because split_cells
    # does not decode escapes inside a code span.
    text = (
        "| finding | decision | action | verification |\n"
        "|---|---|---|---|\n"
        "| f | accepted | rerun | check `grep x | sort` output |\n"
    )
    rendered = conv.convert(text)
    assert "grep x | sort" in rendered  # literal in-span pipe preserved, no backslash
    assert r"grep x \| sort" not in rendered
    ok, _issues, _counts = adj.validate_text(rendered)
    assert ok
