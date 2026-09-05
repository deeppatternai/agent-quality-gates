"""Regression tests for the code-audit dual-audit (audit_id 27ff7041) findings.

Each test pins a real implementation bug the double-audit caught. RED before
the fix, GREEN after.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from aqg_test_quality_review import (  # noqa: E402
    analyze,
    classify_assertion,
    parse_diff,
    render_ledger_yaml,
    validate_ledger,
)
from _tq_core import AddedLine, FileDiff, group_added_tests  # noqa: E402


def _tf(path, lines, removed=()):
    added = tuple(AddedLine(lineno=1 + i, text=t) for i, t in enumerate(lines))
    return FileDiff(path=path, is_new=True, added=added, removed=tuple(removed))


_LEDGER = """\
test_quality_review:
  candidates: []
  counts: {{ shape_ratio: {ratio} }}
  anti_horizontal: {{ fired: {ah} }}
  findings: {findings}
  decision: accept
  decision_reason: ok
"""


# ---- C1: validate fail-closed --------------------------------------------


def test_c1_string_false_anti_horizontal_not_nlj():
    # bool("false") == True footgun: a string "false" must NOT fire NLJ.
    led = _LEDGER.format(ratio="0.0", ah='"false"', findings="[]")
    r = validate_ledger(led)
    # malformed fired (string) → fail-closed violation, and must not NLJ-fire on it
    assert not (r["valid"] and r["anti_horizontal_fired"] is True and r["needs_llm_judgement"])


def test_c1_malformed_counts_is_violation():
    led = (
        "test_quality_review:\n"
        "  candidates: []\n"
        "  counts: ignored-string\n"
        "  decision: accept\n"
        "  decision_reason: ok\n"
    )
    r = validate_ledger(led)
    assert r["valid"] is False


def test_c1_malformed_findings_is_violation():
    led = (
        "test_quality_review:\n"
        "  candidates: []\n"
        "  findings: none\n"
        "  decision: accept\n"
        "  decision_reason: ok\n"
    )
    r = validate_ledger(led)
    assert r["valid"] is False


# ---- C2: grouping ---------------------------------------------------------


def test_c2a_single_line_it_classified():
    f = _tf("src/x.test.ts", ["  it('works', () => expect(sum(1,2)).toBe(3))"])
    regions = group_added_tests(f)
    assert len(regions) == 1
    assert regions[0].kind == "behavioral_only", "def-line assertion must be classified"


def test_c2b_helper_def_ends_region():
    f = _tf("tests/test_x.py", [
        "def test_shape():",
        "    assert isinstance(make(), dict)",
        "def helper():",            # non-test def at same indent → ends region
        "    assert compute() == 1",
    ])
    regions = group_added_tests(f)
    shape_only = [r for r in regions if r.name == "test_shape"]
    assert shape_only and shape_only[0].kind == "shape_only", \
        "helper()'s behavioral assertion must not leak into test_shape"


# ---- P1: YAML escaping round-trip ----------------------------------------


def test_p1_quote_in_deleted_assertion_round_trips():
    # deleted assertion containing quotes → pattern has quotes → must stay valid YAML
    f = FileDiff(path="tests/test_x.py", removed=('    assert msg == "ok"',))
    result = analyze([f])
    ledger = render_ledger_yaml(result)
    r = validate_ledger(ledger)
    assert "yaml parse error" not in " ".join(r["violations"]).lower()
    # the ledger is structurally parseable (decision is TODO so not "valid", but
    # it must not be a parse error — the round-trip itself must survive)
    assert r["violations"] == [] or all("parse" not in v.lower() for v in r["violations"])


# ---- P2: `is None` is behavioral -----------------------------------------


def test_p2_is_none_is_behavioral():
    assert classify_assertion("    assert result.get('e') is None") == "behavioral"
    assert classify_assertion("    assert x is not None") == "behavioral"
    # and a test mixing isinstance guard + `is None` value assert is mixed, not shape_only
    f = _tf("tests/test_x.py", [
        "def test_login():",
        "    assert isinstance(s, Session)",
        "    assert s.error is None",
    ])
    assert group_added_tests(f)[0].kind == "mixed"


def test_p2_type_is_stays_shape():
    # regression guard: `type(x) is dict` must remain shape (checked before `is`)
    assert classify_assertion("    assert type(x) is dict") == "shape"


# ---- P3: key-presence wrappers are shape ---------------------------------


def test_p3_set_keys_is_shape():
    assert classify_assertion("    assert set(r.keys()) == {'id', 'name'}") == "shape"
    assert classify_assertion("    assert sorted(r.keys()) == ['a', 'b']") == "shape"


# ---- P4: coverage-gap basename pairing not substring ---------------------


def test_p4_substring_does_not_falsely_pair():
    src = FileDiff(path="src/app.py", added=(AddedLine(1, "x"),))
    unrelated_test = FileDiff(path="tests/happy_path_test.py", is_new=True,
                              added=(AddedLine(1, "def test_happy(): assert run() == 1"),))
    result = analyze([src, unrelated_test])
    gaps = [c for c in result.candidates if c.concern == "coverage_gap"]
    assert len(gaps) == 1, "app.py must not be 'paired' with happy_path_test.py by substring"
    assert gaps[0].confidence == "low"
