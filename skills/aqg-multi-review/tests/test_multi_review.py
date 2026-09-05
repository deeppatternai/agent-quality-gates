"""Tests for aqg-multi-review local helper — cross-impl parity with cloud.

Mirrors `the cloud backend/tests/test_skill_multi_review.py` key cases:
- 5 NLJ triggers (cross-dim conflict / 3+ dims accept narrowed / needs-rerun /
  CRITICAL no audit_id / skeleton-paste trap)
- C1 line-range overlap detection
- C2 dim-not-in-parsed_dims rejection
- C3 skeleton-paste trap
- C4 dim uniqueness handled (in cmd_new dedupe)
- audit_id placeholder rejection
- findings empty-dict not silently accepted

Run from `skills/aqg-multi-review/`:
    python3 -m pytest tests/

Requires PyYAML (pip install pyyaml>=6.0).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Resolve scripts dir
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from aqg_multi_review import (  # noqa: E402
    DIMENSIONS,
    FOCUS_PROMPTS,
    FALLBACK_MODE_SESSION_LLM,
    cmd_new,
    cmd_validate,
    is_real_audit_id,
    verify_audit_id_issued,
    parse_evidence,
    evidence_overlaps,
    skeleton_yaml,
    validate_ledger,
)


# ---- Constants sanity ----------------------------------------------------


def test_dimensions_count():
    assert len(DIMENSIONS) == 5


def test_focus_prompts_full_coverage():
    for d in DIMENSIONS:
        assert d in FOCUS_PROMPTS
        assert len(FOCUS_PROMPTS[d]) >= 50


# ---- Skeleton -------------------------------------------------------------


def test_skeleton_yaml_contains_dims():
    s = skeleton_yaml(["security", "performance"])
    assert "multi_review:" in s
    assert "dimensions_audited: [security, performance]" in s
    assert "decision: TODO" in s


# ---- Evidence parsing ----------------------------------------------------


def test_parse_evidence_simple():
    assert parse_evidence("src/foo.py:42") == ("src/foo.py", 42, 42)


def test_parse_evidence_range():
    assert parse_evidence("src/foo.py:42-44") == ("src/foo.py", 42, 44)


def test_parse_evidence_no_match():
    assert parse_evidence("multiple files") is None
    assert parse_evidence("just text") is None


def test_evidence_overlaps_same_line():
    a = ("src/foo.py", 42, 42)
    b = ("src/foo.py", 42, 44)
    assert evidence_overlaps(a, b) is True


def test_evidence_overlaps_different_paths():
    a = ("src/foo.py", 42, 42)
    b = ("src/bar.py", 42, 42)
    assert evidence_overlaps(a, b) is False


def test_evidence_overlaps_disjoint_lines():
    a = ("src/foo.py", 42, 42)
    b = ("src/foo.py", 99, 99)
    assert evidence_overlaps(a, b) is False


# ---- audit_id placeholder ------------------------------------------------


def test_is_real_audit_id_real_value():
    assert is_real_audit_id("abc12345") is True


def test_is_real_audit_id_placeholder_rejected():
    for placeholder in ["TODO", "todo", "tbd", "n/a", "NULL", "None", "  ", ""]:
        assert is_real_audit_id(placeholder) is False, f"{placeholder!r} should not count"


def test_is_real_audit_id_non_string():
    assert is_real_audit_id(None) is False
    assert is_real_audit_id(123) is False


# ---- WS-8 P2-2: DE-side audit_id issuance cross-check ---------------------


def test_verify_issued_when_result_file_present(tmp_path):
    (tmp_path / "bd5d8c44.json").write_text("{}", encoding="utf-8")
    assert verify_audit_id_issued("bd5d8c44", results_dir=str(tmp_path)) == "issued"


def test_verify_fabricated_when_dir_present_but_file_absent(tmp_path):
    # dir exists, no <id>.json -> the id was never issued by de_audit here
    assert verify_audit_id_issued("panel-fake999", results_dir=str(tmp_path)) == "fabricated"


def test_verify_unverifiable_when_no_results_dir(monkeypatch):
    monkeypatch.delenv("AQG_DE_RESULTS_DIR", raising=False)
    assert verify_audit_id_issued("bd5d8c44") == "unverifiable"


def test_verify_unverifiable_when_dir_missing(tmp_path):
    missing = tmp_path / "nope"
    assert verify_audit_id_issued("bd5d8c44", results_dir=str(missing)) == "unverifiable"


def test_verify_traversal_id_is_fabricated_without_fs_escape(tmp_path):
    # an id with path separators / dots must never stat outside the dir
    assert verify_audit_id_issued("../../etc/passwd", results_dir=str(tmp_path)) == "fabricated"
    assert verify_audit_id_issued("a/b", results_dir=str(tmp_path)) == "fabricated"


def test_verify_reads_env_when_results_dir_arg_omitted(tmp_path, monkeypatch):
    (tmp_path / "abc12345.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AQG_DE_RESULTS_DIR", str(tmp_path))
    assert verify_audit_id_issued("abc12345") == "issued"


def test_verify_case_insensitive_lookup(tmp_path):
    # DE ids are lowercase hex; an upper/mixed-case paste of a legit id must still
    # resolve (audit 22d13af0 f3/f4 — else false-fabricated on a case-sensitive FS).
    (tmp_path / "bd5d8c44.json").write_text("{}", encoding="utf-8")
    assert verify_audit_id_issued("BD5D8C44", results_dir=str(tmp_path)) == "issued"


def test_verify_expanduser_runtimeerror_degrades(monkeypatch):
    # Path.expanduser() raises RuntimeError on `~unknownuser` / unset HOME; the
    # broadened except must degrade to unverifiable, not crash (audit 22d13af0 f2).
    import pathlib

    def boom(self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(pathlib.Path, "expanduser", boom)
    assert verify_audit_id_issued("bd5d8c44", results_dir="~/de-results") == "unverifiable"


def test_verify_replay_residual_is_not_bound_to_ledger(tmp_path):
    # DOCUMENTED RESIDUAL (audit 22d13af0 f1, 4/4 convergent): existence-only, no
    # ledger binding — a real issued id makes ANY ledger read 'issued'. This locks the
    # known limitation so a future DE-side content-binding change has an anchor to flip.
    (tmp_path / "realid99.json").write_text("{}", encoding="utf-8")
    assert verify_audit_id_issued("realid99", results_dir=str(tmp_path)) == "issued"


# ---- WS-8 P2-2: a fabricated audit_id must not suppress needs_llm_judgement --

_CRITICAL_LEDGER = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: CRITICAL
      pattern: hardcoded secret
      evidence: src/x.py:10
      fix: rotate + move to env
  decision: reject
  decision_reason: critical secret must be rotated before merge
  audit_id: panel-fake999
"""


def test_fabricated_audit_id_does_not_suppress_nlj(tmp_path, monkeypatch):
    # results dir set, but panel-fake999.json absent -> fabricated -> NLJ still fires
    monkeypatch.setenv("AQG_DE_RESULTS_DIR", str(tmp_path))
    r = validate_ledger(_CRITICAL_LEDGER)
    assert r["valid"] is True
    assert r["has_audit_id"] is False, "fabricated id must be downgraded"
    assert r["audit_id_issuance"] == "fabricated"
    assert r["needs_llm_judgement"] is True
    assert any("not found in the DE results" in v["message"] for v in r["violations"])


def test_unverifiable_audit_id_keeps_prior_behavior(monkeypatch):
    # no results dir configured -> unchanged: audit_id still suppresses NLJ, no violation
    monkeypatch.delenv("AQG_DE_RESULTS_DIR", raising=False)
    r = validate_ledger(_CRITICAL_LEDGER)
    assert r["valid"] is True
    assert r["has_audit_id"] is True
    assert r["audit_id_issuance"] == "unverifiable"
    assert r["needs_llm_judgement"] is False
    assert not any("DE results" in v["message"] for v in r["violations"])


def test_issued_audit_id_suppresses_nlj(tmp_path, monkeypatch):
    (tmp_path / "panel-fake999.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AQG_DE_RESULTS_DIR", str(tmp_path))
    r = validate_ledger(_CRITICAL_LEDGER)
    assert r["valid"] is True
    assert r["has_audit_id"] is True
    assert r["audit_id_issuance"] == "issued"
    assert r["needs_llm_judgement"] is False


# ---- Validate: structural ------------------------------------------------


VALID_LEDGER = """multi_review:
  reviewed_at: 2026-05-09 12:00 UTC
  diff_chars: 1234
  dimensions_audited: [security, performance]
  findings:
    - dimension: security
      severity: HIGH
      pattern: SQL string concat
      evidence: src/users.py:42
      fix: parameterize
    - dimension: performance
      severity: MEDIUM
      pattern: N+1
      evidence: src/views.py:120
      fix: select_related
  decision: reject
  decision_reason: SQL concat must be fixed
  audit_id: panel-abc123
"""


def test_validate_valid_ledger_passes():
    r = validate_ledger(VALID_LEDGER)
    assert r["valid"] is True
    assert r["parsed_decision"] == "reject"
    assert "security" in r["parsed_dimensions"]
    assert r["findings_count_per_dim"]["security"] == 1
    assert r["findings_count_per_dim"]["performance"] == 1
    assert r["has_audit_id"] is True


def test_validate_missing_top_level_key_invalid():
    r = validate_ledger("not_multi_review: {}\n")
    assert r["valid"] is False


def test_validate_invalid_yaml_returns_violation():
    r = validate_ledger("multi_review:\n  dimensions_audited: [unclosed\n")
    assert r["valid"] is False


def test_validate_yaml_anchor_rejected():
    bad = """multi_review:
  dimensions_audited: [security]
  decision: &x accept
  decision_reason: *x
"""
    r = validate_ledger(bad)
    assert r["valid"] is False


# ---- C1: cross-dim conflict via line-range overlap -----------------------


def test_C1_cross_dim_overlap_detected():
    text = """multi_review:
  dimensions_audited: [security, performance]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: src/foo.py:42
    - dimension: performance
      severity: MEDIUM
      pattern: y
      evidence: src/foo.py:42-44
  decision: reject
  decision_reason: review needed
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert len(r["cross_dim_conflicts"]) >= 1
    assert r["needs_llm_judgement"] is True


def test_C1_no_overlap_different_lines():
    text = """multi_review:
  dimensions_audited: [security, performance]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: src/foo.py:42
    - dimension: performance
      severity: MEDIUM
      pattern: y
      evidence: src/foo.py:99
  decision: reject
  decision_reason: review needed
  audit_id: x
"""
    r = validate_ledger(text)
    assert len(r["cross_dim_conflicts"]) == 0


# ---- dedup-confirm: convergent findings (multi-dim same file:line) -------


def _convergent_ledger(dims_sevs, decision="reject"):
    """Build a ledger where every (dim, severity) flags the SAME line src/foo.py:42."""
    lines = ["multi_review:", f"  dimensions_audited: [{', '.join(d for d, _ in dims_sevs)}]", "  findings:"]
    for d, s in dims_sevs:
        lines += [f"    - dimension: {d}", f"      severity: {s}", "      pattern: p",
                  "      evidence: src/foo.py:42"]
    lines += [f"  decision: {decision}", "  decision_reason: r", "  audit_id: x", ""]
    return "\n".join(lines)


def test_convergent_findings_two_dims_elevated():
    r = validate_ledger(_convergent_ledger([("security", "HIGH"), ("performance", "MEDIUM")]))
    assert r["valid"] is True
    cf = r["convergent_findings"]
    assert len(cf) == 1
    e = cf[0]
    assert e["dim_count"] == 2
    assert e["dimensions"] == ["performance", "security"]
    assert e["confidence"] == "elevated"
    assert e["severity_agreement"] is False  # HIGH vs MEDIUM
    assert "src/foo.py:42" in e["evidences"]


def test_convergent_findings_three_dims_high_confidence():
    r = validate_ledger(_convergent_ledger(
        [("security", "HIGH"), ("performance", "HIGH"), ("logic", "HIGH")]))
    cf = r["convergent_findings"]
    assert len(cf) == 1
    assert cf[0]["dim_count"] == 3
    assert cf[0]["confidence"] == "high"
    assert cf[0]["severity_agreement"] is True  # all HIGH


def test_convergent_findings_empty_when_no_overlap():
    text = """multi_review:
  dimensions_audited: [security, performance]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: src/foo.py:42
    - dimension: performance
      severity: MEDIUM
      pattern: y
      evidence: src/foo.py:99
  decision: reject
  decision_reason: r
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["convergent_findings"] == []


def test_convergent_findings_single_dim_not_convergent():
    # One dimension flagging two overlapping lines is NOT cross-dim corroboration.
    text = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: src/foo.py:42
    - dimension: security
      severity: LOW
      pattern: y
      evidence: src/foo.py:42-44
  decision: reject
  decision_reason: r
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["convergent_findings"] == []


def test_invalid_ledger_has_empty_convergent_findings():
    r = validate_ledger("not_multi_review: {}\n")
    assert r["convergent_findings"] == []


def test_convergent_chain_decomposes_to_pairwise_not_overstated():
    # audit 63cf450f f1: a transitive chain (10-20 ~ 20-30 ~ 30-40) has NO point
    # common to all three, so it must yield two ELEVATED pairwise convergences,
    # never one HIGH 3-dim. (cross_dim_conflicts, transitively grouped, still 1.)
    text = """multi_review:
  dimensions_audited: [logic, security, performance]
  findings:
    - dimension: logic
      severity: HIGH
      pattern: a
      evidence: f.py:10-20
    - dimension: security
      severity: HIGH
      pattern: b
      evidence: f.py:20-30
    - dimension: performance
      severity: HIGH
      pattern: c
      evidence: f.py:30-40
  decision: reject
  decision_reason: r
  audit_id: x
"""
    r = validate_ledger(text)
    cf = r["convergent_findings"]
    assert all(e["confidence"] == "elevated" for e in cf), cf
    assert all(e["dim_count"] == 2 for e in cf), cf
    dim_pairs = sorted(tuple(e["dimensions"]) for e in cf)
    assert dim_pairs == [("logic", "security"), ("performance", "security")], dim_pairs
    # the transitive conflict grouping still collapses the chain into one group
    assert len(r["cross_dim_conflicts"]) == 1


def test_convergent_range_overlap_common_region():
    # Two dims over ranges sharing a common region → one elevated 2-dim entry.
    text = """multi_review:
  dimensions_audited: [logic, security]
  findings:
    - dimension: logic
      severity: MEDIUM
      pattern: a
      evidence: f.py:10-25
    - dimension: security
      severity: HIGH
      pattern: b
      evidence: f.py:20-30
  decision: reject
  decision_reason: r
  audit_id: x
"""
    r = validate_ledger(text)
    assert len(r["convergent_findings"]) == 1
    e = r["convergent_findings"][0]
    assert e["dimensions"] == ["logic", "security"]
    assert e["severity_agreement"] is False


def test_convergent_raw_exact_evidence():
    # Unparseable-but-identical evidence strings are a common location too.
    text = """multi_review:
  dimensions_audited: [logic, security]
  findings:
    - dimension: logic
      severity: HIGH
      pattern: a
      evidence: the auth refresh flow
    - dimension: security
      severity: HIGH
      pattern: b
      evidence: the auth refresh flow
  decision: reject
  decision_reason: r
  audit_id: x
"""
    r = validate_ledger(text)
    cf = r["convergent_findings"]
    assert len(cf) == 1
    assert cf[0]["evidences"] == ["the auth refresh flow"]
    assert cf[0]["dim_count"] == 2


def test_convergent_finding_full_shape():
    r = validate_ledger(_convergent_ledger([("security", "LOW"), ("logic", "CRITICAL")]))
    assert len(r["convergent_findings"]) == 1
    e = r["convergent_findings"][0]
    assert set(e) == {
        "evidences", "dimensions", "dim_count", "severities",
        "severity_agreement", "confidence",
    }
    assert e["severities"] == ["CRITICAL", "LOW"]  # sorted
    assert e["dimensions"] == ["logic", "security"]  # sorted


def test_cross_dim_conflicts_string_format_unchanged():
    # Back-compat: the conflict string format is byte-stable (dedup-confirm did
    # not touch the cross_dim_conflicts loops).
    r = validate_ledger(_convergent_ledger([("security", "HIGH"), ("performance", "MEDIUM")]))
    assert len(r["cross_dim_conflicts"]) == 1
    s = r["cross_dim_conflicts"][0]
    assert s == (
        "evidences=['src/foo.py:42'] flagged by "
        "['performance', 'security'] with severities ['HIGH', 'MEDIUM']"
    )


# ---- C2: dim-not-in-parsed_dims ------------------------------------------


def test_C2_finding_dim_outside_declared_rejected():
    bad = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: performance
      severity: HIGH
      pattern: out-of-scope
      evidence: src/foo.py:42
  decision: reject
  decision_reason: x
  audit_id: x
"""
    r = validate_ledger(bad)
    assert r["valid"] is False


# ---- C3: skeleton-paste trap ---------------------------------------------


def test_C3_zero_findings_accept_fires_nlj():
    text = """multi_review:
  dimensions_audited: [logic, security, performance]
  decision: accept
  decision_reason: looked at it briefly, all good
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert r["needs_llm_judgement"] is True


# ---- C5: Trigger 3 narrowed ---------------------------------------------


def test_C5_pure_LOW_with_audit_id_no_signal():
    text = """multi_review:
  dimensions_audited: [logic, security, performance]
  findings:
    - dimension: logic
      severity: LOW
      pattern: x
      evidence: a.py:1
    - dimension: security
      severity: LOW
      pattern: y
      evidence: b.py:2
    - dimension: performance
      severity: LOW
      pattern: z
      evidence: c.py:3
  decision: accept
  decision_reason: minor nits, all documented
  audit_id: panel-clean
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert r["needs_llm_judgement"] is False


def test_C5_3_dims_no_audit_id_fires():
    text = """multi_review:
  dimensions_audited: [logic, security, performance]
  findings:
    - dimension: logic
      severity: LOW
      pattern: x
      evidence: a.py:1
    - dimension: security
      severity: LOW
      pattern: y
      evidence: b.py:2
    - dimension: performance
      severity: LOW
      pattern: z
      evidence: c.py:3
  decision: accept
  decision_reason: minor and not blocking
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert r["needs_llm_judgement"] is True


# ---- audit_id placeholder rejection --------------------------------------


def test_audit_id_TODO_rejected_critical_finding_fires():
    text = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: CRITICAL
      pattern: x
      evidence: a.py:1
  decision: reject
  decision_reason: review needed
  audit_id: TODO
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert r["has_audit_id"] is False  # TODO is placeholder
    assert r["needs_llm_judgement"] is True  # CRITICAL + no real audit_id


# ---- findings empty-dict not silently accepted ---------------------------


def test_findings_empty_dict_rejected():
    bad = """multi_review:
  dimensions_audited: [security]
  findings: {}
  decision: accept
  decision_reason: ok
"""
    r = validate_ledger(bad)
    assert r["valid"] is False


# ---- Batch-3 audit 6066aaeb regressions ----------------------------------


def test_windows_path_evidence_parses_and_overlaps():
    """C1: a Windows drive-letter path must parse (not fall back to raw-string
    match, which misses overlaps → cross-dim NLJ false-negative)."""
    assert parse_evidence(r"C:\foo.py:10-20") == (r"C:\foo.py", 10, 20)
    a = parse_evidence(r"C:\foo.py:10-20")
    b = parse_evidence(r"C:\foo.py:15-25")
    assert a is not None and b is not None
    assert evidence_overlaps(a, b) is True


def test_windows_path_cross_dim_conflict_detected():
    """C1 end-to-end: overlapping Windows-path findings in two dims must raise a
    cross-dim conflict (the NLJ trigger the regex bug silently defeated)."""
    text = r"""multi_review:
  dimensions_audited: [security, performance]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: C:\repo\foo.py:10-20
    - dimension: performance
      severity: MEDIUM
      pattern: y
      evidence: C:\repo\foo.py:15-25
  decision: reject
  decision_reason: overlapping windows-path concerns
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert len(r["cross_dim_conflicts"]) >= 1, "windows-path overlap missed"
    assert r["needs_llm_judgement"] is True


def test_missing_evidence_finding_rejected():
    """C2: a finding with valid dim+severity but MISSING evidence must invalidate
    the ledger (fail-closed), not be silently counted + excluded from conflicts."""
    missing = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
  decision: accept
  decision_reason: looks fine
  audit_id: x
"""
    r = validate_ledger(missing)
    assert r["valid"] is False, "finding missing evidence must invalidate the ledger"


def test_non_string_evidence_finding_rejected():
    """C2: non-string evidence (a number) is also a fail-closed violation."""
    bad = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: 42
  decision: accept
  decision_reason: looks fine
  audit_id: x
"""
    r = validate_ledger(bad)
    assert r["valid"] is False


def test_reversed_range_normalized_not_clamped():
    """C3: a reversed range must normalize (swap), not clamp to one line (which
    hides overlaps with intermediate lines)."""
    assert parse_evidence("f.py:20-10") == ("f.py", 10, 20)
    rng = parse_evidence("f.py:20-10")
    mid = parse_evidence("f.py:15")
    assert rng is not None and mid is not None
    assert evidence_overlaps(rng, mid) is True


def test_non_string_dimension_entry_rejected():
    """gem-f5: a non-string entry in dimensions_audited must be a violation, not
    silently ignored."""
    bad = """multi_review:
  dimensions_audited: [logic, 5, security]
  findings: []
  decision: accept
  decision_reason: ok
  audit_id: x
"""
    r = validate_ledger(bad)
    assert r["valid"] is False, "non-string dimension entry must invalidate"


def test_transitive_overlap_grouped_across_three_dims():
    """gem-f4: A overlaps B, B overlaps C, A disjoint C — all three must land in
    ONE cross-dim group (not dropped by the greedy seen-skip)."""
    text = """multi_review:
  dimensions_audited: [logic, security, performance]
  findings:
    - dimension: logic
      severity: HIGH
      pattern: a
      evidence: f.py:10-20
    - dimension: security
      severity: HIGH
      pattern: b
      evidence: f.py:20-30
    - dimension: performance
      severity: HIGH
      pattern: c
      evidence: f.py:30-40
  decision: reject
  decision_reason: overlapping concerns
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert r["needs_llm_judgement"] is True
    joined = " ".join(r["cross_dim_conflicts"])
    assert "logic" in joined and "security" in joined and "performance" in joined, \
        f"transitive overlap dropped a dim: {r['cross_dim_conflicts']}"


# ---- Tier A3 (PR #183) absorption: quote-line gate / verified / repo_overrides ----


def test_A3_focus_prompts_include_quote_line_gate():
    """① every dimension prompt ends with the pre-emit "cite the exact line"
    verification gate so auditors quote evidence before flagging."""
    for d in DIMENSIONS:
        assert "Cite the exact line" in FOCUS_PROMPTS[d], f"{d} missing quote-line gate"


def test_A3_skeleton_includes_repo_overrides_and_verified():
    """③ header carries the optional repo_overrides block + ② finding template
    carries the optional verified field."""
    s = skeleton_yaml(["security", "performance"])
    assert "repo_overrides" in s
    assert "verified: true | false" in s
    # repo_overrides sits in the header, above the multi_review block
    assert s.index("repo_overrides") < s.index("multi_review:")


def test_A3_verified_true_counted():
    """② verified: true is parsed + tallied into findings_verified_count."""
    text = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: a.py:1
      verified: true
  decision: reject
  decision_reason: confirmed by reproduction
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert r["findings_verified_count"] == 1


def test_A3_verified_non_bool_warns_not_invalid():
    """② a non-bool verified value is a soft warning, not a hard error; the
    finding is treated as unverified (fail-soft on an optional field)."""
    text = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: a.py:1
      verified: yes-please
  decision: reject
  decision_reason: needs work
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert r["findings_verified_count"] == 0
    assert any(v["severity"] == "warning" and v.get("field") == "findings"
               for v in r["violations"]), "non-bool verified must warn"


def test_A3_top_level_repo_overrides_not_gated():
    """③ a caller-supplied top-level repo_overrides block must NOT invalidate the
    ledger — the validator ignores it entirely (caller-parsed, not validator-gated)."""
    text = """repo_overrides:
  ignore: [legacy/]
  emphasize: [security]
  notes: this repo uses framework X
multi_review:
  dimensions_audited: [security]
  decision: accept
  decision_reason: reviewed clean
  audit_id: panel-x
"""
    r = validate_ledger(text)
    assert r["valid"] is True


# ---- Tier A3 audit 1f6d550d (dual-audit) convergent-fix coverage ----


def test_A3_quote_line_gate_exact_suffix():
    """① every dim prompt ends with the EXACT shared gate constant (guards
    against per-dim drift of the gate text)."""
    from aqg_multi_review import _QUOTE_LINE_GATE
    for d in DIMENSIONS:
        assert FOCUS_PROMPTS[d].endswith(_QUOTE_LINE_GATE), f"{d} missing exact gate suffix"
    assert "Cite the exact line" in _QUOTE_LINE_GATE
    assert _QUOTE_LINE_GATE.rstrip().endswith("do not report it.")


def test_A3_verified_false_not_counted():
    """② an explicit verified: false parses cleanly (no warning) and is not tallied."""
    text = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: a.py:1
      verified: false
  decision: reject
  decision_reason: needs work
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert r["findings_verified_count"] == 0
    assert not any(v["severity"] == "warning" and "verified" in v["message"]
                   for v in r["violations"])


def test_A3_verified_skipped_for_rejected_finding():
    """② fix (audit f3466101 f1 convergent): verified is a property of an ACCEPTED
    finding. A finding rejected by the local evidence gate is NOT tallied and its
    verified field is NOT warned on — the verified ≤ accepted-finding invariant
    holds. (Cloud keeps the finding since evidence is optional → it DOES warn/
    count; that divergence is the pre-existing evidence-required drift correctly
    propagating, not an A3 parity bug.)"""
    text = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      verified: not-a-bool
  decision: reject
  decision_reason: needs work
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is False  # evidence required locally → finding rejected
    assert not any(v["severity"] == "warning" and "verified" in v["message"]
                   for v in r["violations"]), "rejected finding must not warn on verified"
    assert r["findings_verified_count"] == 0


def test_A3_verified_true_not_counted_when_rejected():
    """② fix: verified:true with MISSING evidence is NOT tallied locally — the
    finding is rejected, so findings_verified_count stays 0 (never exceeds the
    accepted-finding count)."""
    text = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      verified: true
  decision: reject
  decision_reason: needs work
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is False
    assert r["findings_verified_count"] == 0


def test_A3_verified_count_never_exceeds_total():
    """② fix invariant (audit f3466101 f1): findings_verified_count ≤ total
    accepted findings, even with a mix of accepted + rejected verified:true."""
    text = """multi_review:
  dimensions_audited: [security, logic]
  findings:
    - dimension: security
      severity: HIGH
      pattern: ok
      evidence: a.py:1
      verified: true
    - dimension: logic
      severity: LOW
      pattern: rejected-no-evidence
      verified: true
  decision: reject
  decision_reason: mixed
  audit_id: x
"""
    r = validate_ledger(text)
    total = sum(r["findings_count_per_dim"].values())
    assert total == 1  # only the evidence-bearing finding accepted
    assert r["findings_verified_count"] == 1
    assert r["findings_verified_count"] <= total


def test_A3_deep_nested_repo_overrides_warns():
    """③ fix (audit f3466101 #2 convergent): repo_overrides mis-indented at ANY
    depth (here inside a findings[] item) is detected, not just a direct
    multi_review child."""
    text = """multi_review:
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: HIGH
      pattern: x
      evidence: a.py:1
      repo_overrides:
        ignore: [x]
  decision: reject
  decision_reason: needs work
  audit_id: x
"""
    r = validate_ledger(text)
    assert any(v["severity"] == "warning" and v.get("field") == "repo_overrides"
               for v in r["violations"]), "deep-nested repo_overrides must warn"


def test_A3_nested_repo_overrides_warns_not_gated():
    """③ fix (audit 1f6d550d #2 convergent): repo_overrides mis-indented INSIDE
    multi_review stays valid (not gated) but emits a misplacement warning so the
    caller's tuning isn't silently dropped."""
    text = """multi_review:
  dimensions_audited: [security]
  repo_overrides:
    ignore: [legacy/]
  decision: accept
  decision_reason: clean
  audit_id: x
"""
    r = validate_ledger(text)
    assert r["valid"] is True
    assert any(v["severity"] == "warning" and v.get("field") == "repo_overrides"
               for v in r["violations"]), "nested repo_overrides must warn"


def test_A3_skeleton_is_valid_yaml():
    """③ the skeleton (incl. the commented repo_overrides header) loads as valid
    YAML — comments are inert, the multi_review block parses, and the commented
    repo_overrides does NOT leak into the parsed document."""
    import yaml
    sk = skeleton_yaml(["security", "performance"])
    loaded = yaml.safe_load(sk)
    assert "multi_review" in loaded
    assert "repo_overrides" not in loaded


# ---- WS-4 §9-5: --fallback-session-llm degraded mode -----------------------
# Hard constraint (R1-Cluster J): the fallback (engine-unavailable) path must be
# machine-labeled single-model / same-vendor / non-independent / rough-eval-only,
# and MUST NOT be presentable as a cross-vendor independent panel.


def test_skeleton_fallback_stamps_machine_marker():
    """new --fallback-session-llm emits a YAML marker the validator can read:
    fallback_mode + independence, so the degradation survives into the ledger."""
    import yaml
    sk = skeleton_yaml(["security"], fallback=True)
    loaded = yaml.safe_load(sk)
    mr = loaded["multi_review"]
    assert mr["fallback_mode"] == "session-llm"
    assert mr["independence"] == "none"


def test_skeleton_non_fallback_has_no_marker():
    """Default skeleton stays unchanged — no fallback marker leaks in."""
    import yaml
    mr = yaml.safe_load(skeleton_yaml(["security"]))["multi_review"]
    assert "fallback_mode" not in mr
    assert "independence" not in mr


def test_validate_fallback_ledger_reports_mode():
    """A well-formed fallback ledger (audit_id null) is valid and the verdict
    dict carries fallback_mode so downstream output can label it."""
    text = """multi_review:
  reviewed_at: 2026-07-11 00:00 UTC
  fallback_mode: session-llm
  independence: none
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: LOW
      pattern: rough single-model note
      evidence: src/x.py:10
      fix: revisit under real panel
  decision: needs-cross-llm-rerun
  decision_reason: single-model rough eval only; escalate to cross-vendor panel
  audit_id: null
"""
    r = validate_ledger(text)
    assert r["valid"] is True, r["violations"]
    assert r["fallback_mode"] == "session-llm"


def test_validate_non_fallback_ledger_mode_is_none():
    """Existing ledgers report fallback_mode None (backward compatible)."""
    r = validate_ledger(VALID_LEDGER)
    assert r["valid"] is True
    assert r["fallback_mode"] is None


def test_validate_fallback_with_real_audit_id_is_overclaim_error():
    """Anti-overclaim gate (Cluster J): a fallback ledger CANNOT also claim a real
    cross-panel audit_id — that packages a single-model rough eval as an
    independent panel. Must be an ERROR (invalid)."""
    text = """multi_review:
  reviewed_at: 2026-07-11 00:00 UTC
  fallback_mode: session-llm
  independence: none
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: LOW
      pattern: note
      evidence: src/x.py:10
      fix: x
  decision: accept
  decision_reason: looks fine
  audit_id: panel-realid123
"""
    r = validate_ledger(text)
    assert r["valid"] is False
    assert any(
        v["severity"] == "error" and v.get("field") == "fallback_mode"
        for v in r["violations"]
    ), r["violations"]


def test_validate_unknown_fallback_mode_warns():
    """Only session-llm is a recognized fallback marker; anything else warns so a
    typo can't silently pass as a known degraded mode."""
    text = """multi_review:
  reviewed_at: 2026-07-11 00:00 UTC
  fallback_mode: bogus-mode
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: LOW
      pattern: note
      evidence: src/x.py:10
      fix: x
  decision: reject
  decision_reason: whatever
  audit_id: null
"""
    r = validate_ledger(text)
    assert any(
        v.get("field") == "fallback_mode" and v["severity"] == "warning"
        for v in r["violations"]
    ), r["violations"]


# ---- WS-4 §9-5: fallback CLI output labeling (end-to-end) -------------------


def _ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


def test_cmd_new_fallback_banner_labels_degradation(capsys):
    """The rendered `new --fallback-session-llm` output MUST carry every required
    label up front and MUST NOT read as a cross-vendor panel (Cluster J)."""
    rc = cmd_new(_ns(dimensions=["security"], fallback_session_llm=True, json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "DEGRADED FALLBACK" in out
    for token in ("single-model", "same-vendor", "non-independent", "rough-eval-only"):
        assert token in out, f"missing required label token: {token}"
    assert "NOT a cross-vendor" in out
    # the machine marker is embedded in the emitted skeleton
    assert f"fallback_mode: {FALLBACK_MODE_SESSION_LLM}" in out


def test_cmd_new_default_has_no_degraded_banner(capsys):
    """Default mode stays a normal panel skeleton — no degraded banner leaks in."""
    rc = cmd_new(_ns(dimensions=["security"], fallback_session_llm=False, json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "DEGRADED FALLBACK" not in out
    assert "fallback_mode:" not in out


def test_cmd_validate_echoes_degraded_label(tmp_path, capsys):
    """Validating a fallback ledger re-prints the degraded label so no downstream
    consumer of the verdict can present it as an independent review."""
    ledger = tmp_path / "fb.md"
    ledger.write_text(
        "multi_review:\n"
        "  reviewed_at: 2026-07-11 00:00 UTC\n"
        f"  fallback_mode: {FALLBACK_MODE_SESSION_LLM}\n"
        "  independence: none\n"
        "  dimensions_audited: [security]\n"
        "  findings:\n"
        "    - dimension: security\n"
        "      severity: LOW\n"
        "      pattern: rough note\n"
        "      evidence: src/x.py:10\n"
        "      fix: revisit\n"
        "  decision: needs-cross-llm-rerun\n"
        "  decision_reason: single-model rough eval; escalate\n"
        "  audit_id: null\n",
        encoding="utf-8",
    )
    rc = cmd_validate(_ns(file=str(ledger), json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "DEGRADED FALLBACK" in out
    assert "NOT a cross-vendor" in out


# ---- WS-4 §9-5 audit 14c08bb3: anti-overclaim must fail CLOSED on non-exact markers
# (4/4 convergent finding: the has_audit_id error was nested in the exact-match
# branch, so a mis-cased / typo'd / non-string fallback_mode + a real audit_id
# bypassed the gate and read as an independent panel).


def _fb_ledger(mode_line, audit_id="null", independence="  independence: none\n",
               evidence="src/x.py:10"):
    return (
        "multi_review:\n"
        "  reviewed_at: 2026-07-11 00:00 UTC\n"
        f"{mode_line}"
        f"{independence}"
        "  dimensions_audited: [security]\n"
        "  findings:\n"
        "    - dimension: security\n"
        "      severity: LOW\n"
        "      pattern: note\n"
        f"      evidence: {evidence}\n"
        "      fix: x\n"
        "  decision: accept\n"
        "  decision_reason: looks fine\n"
        f"  audit_id: {audit_id}\n"
    )


def test_fallback_case_variant_is_recognized():
    """Mis-cased marker canonicalizes to session-llm (gets the degraded label),
    not silently discarded."""
    r = validate_ledger(_fb_ledger("  fallback_mode: Session-LLM\n"))
    assert r["fallback_mode"] == FALLBACK_MODE_SESSION_LLM


def test_fallback_case_variant_plus_audit_id_is_overclaim_error():
    r = validate_ledger(_fb_ledger("  fallback_mode: SESSION-LLM\n", audit_id="panel-realid123"))
    assert r["valid"] is False
    assert any(v["severity"] == "error" and v.get("field") == "fallback_mode"
               for v in r["violations"]), r["violations"]


def test_fallback_typo_plus_audit_id_fails_closed():
    """A typo'd (unrecognized) marker + a real audit_id must STILL error — the
    fallback field is present, so it cannot claim an independent panel."""
    r = validate_ledger(_fb_ledger("  fallback_mode: session_llm\n", audit_id="panel-realid123"))
    assert r["valid"] is False
    assert any(v["severity"] == "error" and v.get("field") == "fallback_mode"
               for v in r["violations"]), r["violations"]


def test_fallback_nonstring_plus_audit_id_fails_closed():
    r = validate_ledger(_fb_ledger("  fallback_mode: true\n", audit_id="panel-realid123"))
    assert r["valid"] is False
    assert any(v["severity"] == "error" and v.get("field") == "fallback_mode"
               for v in r["violations"]), r["violations"]


def test_fallback_independence_contradiction_is_error():
    """fallback_mode: session-llm with independence claiming a cross-vendor panel is
    a contradiction that must error."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        independence="  independence: cross-vendor-panel\n",
    ))
    assert r["valid"] is False
    assert any(v["severity"] == "error" and v.get("field") == "independence"
               for v in r["violations"]), r["violations"]


# ---- PR3 (Local Degraded Audit v5 §17.7): fallback findings need file:line ----
# In session-LLM fallback mode there is no independent cross-vendor panel to
# corroborate a finding, so an un-locatable (non file:line) citation is
# unactionable and MUST be rejected. The cross-vendor path is UNCHANGED
# (evidence-non-empty only), so this hard gate is fallback-mode ONLY
# (Owner ruling 2026-07-21 — minimal blast radius).


def _has_fileline_evidence_error(violations):
    return any(
        v.get("severity") == "error"
        and v.get("field") == "findings"
        and "file:line" in v.get("message", "")
        for v in violations
    )


def test_fallback_finding_non_fileline_evidence_is_rejected():
    """S1.1: a fallback finding whose evidence is non-empty but does not parse to
    file:line MUST be rejected — no panel corroborates it (v5 §17.7)."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence="auth code somewhere",
    ))
    assert r["valid"] is False, r["violations"]
    assert _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_fallback_finding_fileline_evidence_is_accepted():
    """S1.2: a fallback finding WITH a real file:line citation is not rejected on
    evidence grounds (the gate does not over-fire)."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence="src/x.py:10",
    ))
    assert not _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_non_fallback_finding_non_fileline_evidence_still_accepted():
    """S1.3 (scope guard): the file:line hard gate is fallback-ONLY. A cross-vendor
    ledger with prose-only evidence stays valid — unchanged behavior."""
    text = """multi_review:
  reviewed_at: 2026-07-11 00:00 UTC
  dimensions_audited: [security]
  findings:
    - dimension: security
      severity: LOW
      pattern: rough note
      evidence: auth code somewhere
      fix: x
  decision: accept
  decision_reason: looks fine
  audit_id: null
"""
    r = validate_ledger(text)
    assert r["fallback_mode"] is None
    assert not _has_fileline_evidence_error(r["violations"]), r["violations"]
    assert r["valid"] is True, r["violations"]


# ---- PR3 hardening (audit 47f85142, 4/4 convergent): the gate must use a strict
# locator, NOT the permissive whole-string parse_evidence (which lets prose with a
# trailing ":N" bypass it, and rejects a valid file:line that carries a rationale).


def test_fallback_prose_with_trailing_colon_number_is_rejected():
    """BYPASS guard (gemini f1 CRITICAL): 'auth code somewhere: 10' must NOT count
    as a locator — a real path token has no spaces. Fail closed. (Quoted so the
    colon reaches the gate as a string instead of tripping YAML's mapping rule.)"""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence='"auth code somewhere: 10"',
    ))
    assert r["valid"] is False, r["violations"]
    assert _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_fallback_prose_with_colon_number_no_space_is_rejected():
    """BYPASS guard, no-space variant: 'auth code somewhere:10' is a valid YAML
    plain scalar (colon not followed by space) and reaches the gate — still no
    path-like token, so it must be rejected."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence="auth code somewhere:10",
    ))
    assert r["valid"] is False, r["violations"]
    assert _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_fallback_fileline_with_trailing_prose_is_accepted():
    """Over-rejection guard (4/4 convergent): a real file:line that carries a
    trailing rationale is locatable, so it MUST be accepted."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence="src/x.py:10 - missing null check",
    ))
    assert not _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_fallback_windows_drive_letter_path_is_accepted():
    """Windows guard (grok): a drive-letter path C:\\x.py:10 is a real locator."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence="C:\\x.py:10",
    ))
    assert not _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_fallback_miscased_marker_drives_the_fileline_gate():
    """The SAME shared predicate governs the new gate: a mis-cased 'Session-LLM'
    marker still enables the strict file:line reject for prose evidence."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: Session-LLM\n",
        evidence="auth code somewhere",
    ))
    assert r["valid"] is False, r["violations"]
    assert _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_nonstring_fallback_marker_does_not_drive_the_fileline_gate():
    """A non-string fallback_mode is NOT the recognized marker, so is_fallback is
    False and the new gate does not fire (prose evidence stays accepted)."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: true\n",
        evidence="auth code somewhere",
    ))
    assert not _has_fileline_evidence_error(r["violations"]), r["violations"]


# ---- PR3 hardening round 2 (audit e904d91d, 3/3 convergent): merely requiring a
# '.' re-opened the bypass — dotted-numeric prose (versions / IPs / ratios) and
# abbreviations are NOT locators. A path-like token needs a separator or a
# letter-initial extension.


@pytest.mark.parametrize("prose_evidence", [
    "2.0:1",              # version / ratio
    "10.0.0.1:8080",      # IPv4:port
    '"ratio of 2.5:1"',   # ratio embedded in prose (quoted: colon+space-free but safe)
    "e.g.:1",             # abbreviation with trailing dot
    "3.11:1",             # dotted version
])
def test_fallback_dotted_numeric_prose_is_rejected(prose_evidence):
    """Dotted-numeric / abbreviation prose must NOT masquerade as a locator."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence=prose_evidence,
    ))
    assert r["valid"] is False, r["violations"]
    assert _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_fallback_bare_filename_with_extension_is_accepted():
    """A bare filename with a letter-initial extension IS a locator."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence="foo.py:42",
    ))
    assert not _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_fallback_space_after_colon_is_rejected_documented_residual():
    """Documented fail-closed residual: a space after the colon ('src/x.py: 10')
    splits the token, so it is rejected. The producer must write '<path>:<line>'
    with no space — a safe, recorded contract (see the module comment)."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence='"src/x.py: 10"',
    ))
    assert r["valid"] is False, r["violations"]
    assert _has_fileline_evidence_error(r["violations"]), r["violations"]


# ---- PR3 hardening round 3 (audit 2add047b, 3/3 convergent): a blocklist regex is
# whack-a-mole + was quadratic (ReDoS). The allowlist + linear parser closes the
# remaining prose classes (URL:port, @scope/pkg, N.x, module.attr, host:port).


@pytest.mark.parametrize("prose_evidence", [
    "http://10.0.0.1:8080",   # scheme URL — the '/' is not a path separator here
    "https://example.com:443",
    "3.x:1",                  # letter-final version shorthand
    "os.path:42",             # module.attr, not a file (.path not allowlisted)
    "api.example.com:443",    # host:port with lettered TLD (was a residual)
    "host.net:80",
])
def test_fallback_url_and_module_prose_is_rejected(prose_evidence):
    """URL / host:port / module-shorthand prose is NOT a code locator."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence=prose_evidence,
    ))
    assert r["valid"] is False, r["violations"]
    assert _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_fallback_scoped_package_prose_is_rejected():
    """A scoped package ref '@angular/core:17' has a '/' but no file extension."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence='"@angular/core:17"',  # quoted: '@' is a reserved YAML indicator
    ))
    assert r["valid"] is False, r["violations"]
    assert _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_fallback_allowlisted_non_python_extension_is_accepted():
    """A real file with an allowlisted non-Python extension is a locator."""
    r = validate_ledger(_fb_ledger(
        "  fallback_mode: session-llm\n",
        evidence="db/schema.sql:12",
    ))
    assert not _has_fileline_evidence_error(r["violations"]), r["violations"]


def test_evidence_has_locator_is_linear_on_pathological_input():
    """ReDoS guard (gemini f1): a huge no-locator string returns fast — the parser
    is linear (whitespace split + rpartition), never a backtracking regex."""
    import time
    from aqg_multi_review import evidence_has_locator
    payload = "a" * 500_000 + ":x"
    start = time.monotonic()
    assert evidence_has_locator(payload) is False
    assert time.monotonic() - start < 1.0
