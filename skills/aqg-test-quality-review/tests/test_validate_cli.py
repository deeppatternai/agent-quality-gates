"""Slice 4 tests: anti-horizontal (commits), validate ledger NLJ triggers, CLI."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import aqg_test_quality_review as tq  # noqa: E402
from aqg_test_quality_review import (  # noqa: E402
    CommitStat,
    detect_anti_horizontal,
    parse_commit_numstat,
    validate_ledger,
)


# ---- anti-horizontal (commit history) ------------------------------------


def test_anti_horizontal_fires_when_tests_batched_before_impl():
    commits = [
        CommitStat("c1", test_funcs_added=5, impl_lines_added=0),  # tests only
        CommitStat("c2", test_funcs_added=0, impl_lines_added=40),  # impl only
    ]
    r = detect_anti_horizontal(commits)
    assert r["fired"] is True


def test_anti_horizontal_not_fired_when_interleaved():
    commits = [
        CommitStat("c1", test_funcs_added=1, impl_lines_added=10),
        CommitStat("c2", test_funcs_added=1, impl_lines_added=12),
    ]
    assert detect_anti_horizontal(commits)["fired"] is False


def test_parse_commit_numstat():
    text = (
        "COMMIT:abc123\n"
        "5\t0\ttests/test_a.py\n"
        "COMMIT:def456\n"
        "40\t2\tsrc/a.py\n"
    )
    commits = parse_commit_numstat(text)
    assert len(commits) == 2
    assert commits[0].test_funcs_added == 5 and commits[0].impl_lines_added == 0
    assert commits[1].impl_lines_added == 40 and commits[1].test_funcs_added == 0
    assert detect_anti_horizontal(commits)["fired"] is True


# ---- validate_ledger NLJ triggers ----------------------------------------

_BASE = """\
test_quality_review:
  candidates:
{cands}
  counts: {{ shape_ratio: {ratio} }}
  anti_horizontal: {{ fired: {ah} }}
  findings:
{findings}
  decision: {decision}
  decision_reason: {reason}
"""


def _ledger(cands="    []", ratio=0.0, ah="false", findings="    []",
            decision="accept", reason="looks fine") -> str:
    return _BASE.format(cands=cands, ratio=ratio, ah=ah, findings=findings,
                        decision=decision, reason=reason)


def test_validate_clean_no_nlj():
    r = validate_ledger(_ledger())
    assert r["valid"] is True
    assert r["needs_llm_judgement"] is False


def test_validate_unhandled_high_candidate_plus_accept_nlj():
    cands = (
        "    - id: cov-001\n"
        "      concern: coverage_gap\n"
        "      confidence: high\n"
        "      disposition: TODO\n"
    )
    r = validate_ledger(_ledger(cands=cands))
    assert r["valid"] is True
    assert r["needs_llm_judgement"] is True
    assert "cov-001" in r["unhandled_high_candidates"]


def test_validate_handled_high_candidate_no_nlj():
    cands = (
        "    - id: cov-001\n"
        "      concern: coverage_gap\n"
        "      confidence: high\n"
        "      disposition: false_positive\n"
    )
    r = validate_ledger(_ledger(cands=cands))
    assert r["valid"] is True
    assert r["needs_llm_judgement"] is False


def test_validate_low_conf_unhandled_no_nlj():
    # low-confidence candidate left unhandled must NOT fire NLJ (C2).
    cands = (
        "    - id: cov-002\n"
        "      concern: coverage_gap\n"
        "      confidence: low\n"
        "      disposition: TODO\n"
    )
    r = validate_ledger(_ledger(cands=cands))
    assert r["needs_llm_judgement"] is False


def test_validate_anti_horizontal_plus_accept_nlj():
    r = validate_ledger(_ledger(ah="true"))
    assert r["needs_llm_judgement"] is True


def test_validate_high_shape_ratio_plus_accept_nlj():
    r = validate_ledger(_ledger(ratio=0.6))
    assert r["needs_llm_judgement"] is True


def test_validate_high_severity_no_audit_nlj():
    findings = (
        "    - concern: shape_over_behavioral\n"
        "      severity: HIGH\n"
        "      audit_id: null\n"
    )
    r = validate_ledger(_ledger(findings=findings))
    assert r["needs_llm_judgement"] is True


def test_validate_reject_decision_suppresses_candidate_nlj():
    # unhandled high candidate but decision=reject → not accepting, no NLJ on that trigger
    cands = (
        "    - id: shape-001\n"
        "      concern: shape_over_behavioral\n"
        "      confidence: high\n"
        "      disposition: TODO\n"
    )
    r = validate_ledger(_ledger(cands=cands, decision="reject"))
    assert r["valid"] is True
    assert r["needs_llm_judgement"] is False


def test_validate_missing_top_key():
    r = validate_ledger("some_other_key: 1\n")
    assert r["valid"] is False


def test_validate_todo_reason_invalid():
    r = validate_ledger(_ledger(reason="TODO"))
    assert r["valid"] is False


# ---- CLI smoke -----------------------------------------------------------


def _run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = tq.main(argv)
    return rc, out.getvalue()


_DIFF = """\
diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,2 +1,3 @@
 def foo():
+    return 99
 # ctx
"""


def test_cli_analyze_smoke(tmp_path):
    df = tmp_path / "d.diff"
    df.write_text(_DIFF)
    rc, text = _run(["analyze", "--diff-file", str(df)])
    assert rc == 0
    assert "test-quality" in text.lower()
    assert "coverage_gap" in text  # source changed, no test → gap candidate


def test_cli_validate_smoke(tmp_path):
    lf = tmp_path / "ledger.md"
    lf.write_text(_ledger())
    rc, text = _run(["validate", "--file", str(lf)])
    assert rc == 0
    assert "VALID" in text
