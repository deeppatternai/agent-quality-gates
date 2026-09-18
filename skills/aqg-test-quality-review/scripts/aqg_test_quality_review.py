#!/usr/bin/env python3
"""aqg-test-quality-review local helper — review-side test-quality analysis.

Signal-only (per ADR 2026-05-08 section 5): this script does NOT call audit tooling.
It parses a unified diff, emits a structured test-quality ledger (mechanical
candidates, each with a stable id + disposition) + per-concern focus prompts,
and a preliminary needs_llm_judgement recommendation. The CALLER (human / Claude /
EAF) runs /audit when the audit policy routes the logical change there and fills
the final findings.

Two subcommands:
- `analyze`  → parse diff, emit ledger skeleton (pre-filled mechanical candidates)
- `validate` → parse a filled ledger, emit needs_llm_judgement signal + verdict

Design (per ADR 2026-05-26-test-quality-review-skill-a1.md):
- Mechanical layer is PRECISION-FIRST: when unsure, do NOT emit a candidate
  (defer to the LLM). shape_over_behavioral candidates come only from
  `shape_only` tests (>=1 shape assertion + ZERO behavioral assertion);
  `mixed` tests are treated as having a guard, not a smell.
- anti-horizontal slicing is a TEMPORAL signal: a single squashed diff cannot
  prove it, so single-diff mode only emits an advisory `bulk_shape_test_risk`
  (never an NLJ trigger). Real detection needs `--commits-numstat` (per-commit
  test/impl interleaving).

The pure diff/classification/detection helpers live in `_tq_core.py` (split
to keep each file under the 800-line house limit) and are re-exported below.

Exit codes:
  0: success — `analyze` printed ledger OR `validate` ledger valid
  1: validation failure — `validate` found schema / disposition violations
  2: usage error — bad args, missing required arg, unknown subcommand
  3: reserved — config or schema error (not currently emitted)
  70: reserved — internal error placeholder for future use

Python stdlib only; PyYAML optional (only for `validate` ledger parsing).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from _tq_core import (  # noqa: F401  (re-exported public API)
    BULK_SHAPE_RATIO_MIN,
    BULK_TEST_MIN,
    SHAPE_RATIO_NLJ,
    AddedLine,
    AnalyzeResult,
    Candidate,
    CommitStat,
    Counts,
    FileDiff,
    FOCUS_PROMPTS,
    TestRegion,
    analyze,
    bulk_shape_test_risk,
    classify_assertion,
    compute_counts,
    detect_anti_horizontal,
    detect_coverage_gaps,
    detect_flaky_candidates,
    detect_shape_candidates,
    detect_suppression_candidates,
    group_added_tests,
    is_source_file,
    is_test_file,
    parse_commit_numstat,
    parse_diff,
)


# ===== Ledger rendering (closeout-importable YAML) =============================


def render_ledger_yaml(result: AnalyzeResult) -> str:
    lines = ["test_quality_review:"]
    lines.append("  mode: " + ("commits" if result.anti_horizontal.get("test_only_commits", 0)
                               or result.anti_horizontal.get("impl_only_commits", 0) else "single_diff"))
    lines.append(f"  diff_files: {{ source_changed: {result.source_changed}, "
                 f"test_changed: {result.test_changed} }}")
    lines.append("  candidates:")
    if result.candidates:
        for c in result.candidates:
            lines.append(f"    - id: {c.id}")
            lines.append(f"      concern: {c.concern}")
            lines.append(f"      confidence: {c.confidence}")
            if c.subtype:
                lines.append(f"      subtype: {c.subtype}")
            # json.dumps → valid double-quoted YAML scalar; escapes quotes /
            # backslashes from deleted-assertion text (audit P1 — round-trip safe).
            lines.append(f"      evidence: {json.dumps(c.evidence, ensure_ascii=False)}")
            lines.append(f"      pattern: {json.dumps(c.pattern, ensure_ascii=False)}")
            lines.append("      disposition: TODO   # accepted | false_positive | deferred | covered_elsewhere")
    else:
        lines.append("    []   # no mechanical candidates")
    co = result.counts
    lines.append("  counts:")
    lines.append(f"    test_funcs_added: {co.test_funcs_added}")
    lines.append(f"    shape_only_tests: {co.shape_only_tests}")
    lines.append(f"    mixed_tests: {co.mixed_tests}")
    lines.append(f"    behavioral_only_tests: {co.behavioral_only_tests}")
    lines.append(f"    shape_ratio: {co.shape_ratio}   # test-level shape_only/total")
    lines.append(f"  bulk_shape_test_risk: {{ fired: {str(result.bulk_shape_risk).lower()}, "
                 f"advisory: true }}   # NOT anti_horizontal (C1)")
    lines.append(f"  anti_horizontal: {{ fired: {str(result.anti_horizontal['fired']).lower()} }}")
    lines.append("  findings:   # fill from /audit per focus prompt; link candidate_ids")
    lines.append("    # - concern: shape_over_behavioral")
    lines.append("    #   severity: HIGH        # CRITICAL | HIGH | MEDIUM | LOW")
    lines.append("    #   candidate_ids: [shape-001]")
    lines.append("    #   verdict: <one line>")
    lines.append("    #   fix: <one line>")
    lines.append("    #   audit_id: <audit id> | null")
    lines.append("  decision: TODO        # accept | reject | needs-deeper-review")
    lines.append("  decision_reason: TODO")
    lines.append("  audit_id: null")
    return "\n".join(lines) + "\n"


# ===== Validate filled ledger ==================================================

CONCERNS = ("shape_over_behavioral", "coverage_gap", "test_suppression", "flaky")
_DISPOSITIONS_HANDLED = {"accepted", "false_positive", "covered_elsewhere"}
_DISPOSITIONS_ALL = _DISPOSITIONS_HANDLED | {"deferred"}
_DECISION_ALIASES = {
    "accept": "accept", "accepted": "accept",
    "reject": "reject", "rejected": "reject",
    "needs-deeper-review": "needs-deeper-review",
    "needs_deeper_review": "needs-deeper-review",
    "needs-deeper": "needs-deeper-review",
}
_AUDIT_ID_PLACEHOLDERS = {"todo", "tbd", "pending", "n/a", "na", "null", "none", "?", "<set>", ""}


def _is_real_audit_id(v) -> bool:
    return isinstance(v, str) and v.strip().lower() not in _AUDIT_ID_PLACEHOLDERS


def _try_yaml_load(text: str):
    try:
        import yaml
    except ImportError:
        return None, ("PyYAML not installed; install `pip install pyyaml>=6.0` "
                      "to use validate mode")
    try:
        for tok in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(tok, (yaml.AnchorToken, yaml.AliasToken)):
                return None, "YAML anchors/aliases not allowed (Billion Laughs defense)"
    except yaml.YAMLError:
        pass
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as exc:
        return None, f"yaml parse error: {str(exc).splitlines()[0][:200]}"


def validate_ledger(text: str) -> dict:
    """Parse a filled test_quality_review ledger; emit needs_llm_judgement.

    NLJ (uniform high-conf rule per C2): only HIGH-confidence unhandled
    candidates feed NLJ; low-conf are advisory. Plus anti_horizontal(commits),
    high-severity-finding-without-audit-id, and high shape_ratio on accept.
    """
    violations: list[str] = []
    parsed, err = _try_yaml_load(text)
    if err is not None:
        return _invalid(err)
    if not isinstance(parsed, dict) or "test_quality_review" not in parsed:
        return _invalid("ledger missing top-level `test_quality_review:` key")
    block = parsed["test_quality_review"]
    if not isinstance(block, dict):
        return _invalid("`test_quality_review` value must be a mapping")

    # candidates
    raw_c = block.get("candidates") or []
    if not isinstance(raw_c, list):
        violations.append("candidates must be a list")
        raw_c = []
    unhandled_high: list[str] = []
    for i, c in enumerate(raw_c):
        if not isinstance(c, dict):
            violations.append(f"candidates[{i}] must be a mapping")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            violations.append(f"candidates[{i}].id must be non-empty str")
        if c.get("concern") not in CONCERNS:
            violations.append(f"candidates[{i}].concern {c.get('concern')!r} unknown")
        conf = c.get("confidence")
        if conf not in ("high", "low"):
            violations.append(f"candidates[{i}].confidence must be high|low")
        disp = c.get("disposition")
        disp_norm = disp.strip().lower() if isinstance(disp, str) else None
        if disp_norm is not None and disp_norm not in (_DISPOSITIONS_ALL | {"todo"}):
            violations.append(
                f"candidates[{i}].disposition {disp!r} invalid "
                f"(one of {sorted(_DISPOSITIONS_ALL)} or TODO)")
        handled = disp_norm in _DISPOSITIONS_HANDLED
        if conf == "high" and not handled:
            unhandled_high.append(str(cid))

    # decision
    raw_decision = block.get("decision")
    parsed_decision = None
    if not isinstance(raw_decision, str) or not raw_decision.strip():
        violations.append("decision must be non-empty string")
    else:
        parsed_decision = _DECISION_ALIASES.get(raw_decision.strip().lower())
        if parsed_decision is None:
            violations.append(f"unrecognized decision {raw_decision!r}")
    raw_reason = block.get("decision_reason")
    if (not isinstance(raw_reason, str) or not raw_reason.strip()
            or raw_reason.strip().upper() == "TODO"):
        violations.append("decision_reason must be non-empty (no TODO placeholder)")

    # findings (for high-severity-without-audit_id) — fail-closed (audit C1):
    # a present-but-malformed field is a violation, never silently ignored.
    raw_f = block.get("findings")
    high_sev_no_audit: list[str] = []
    if raw_f is None:
        raw_f = []
    elif not isinstance(raw_f, list):
        violations.append("findings must be a list (or omitted)")
        raw_f = []
    for f in raw_f:
        if not isinstance(f, dict):
            violations.append("findings[] entries must be mappings")
            continue
        sev = str(f.get("severity", "")).strip().upper()
        if sev in {"CRITICAL", "HIGH"} and not _is_real_audit_id(f.get("audit_id")):
            high_sev_no_audit.append(sev)

    # counts: if present must be a mapping; shape_ratio if present must be numeric
    _counts_raw = block.get("counts")
    shape_ratio = 0.0
    if _counts_raw is not None and not isinstance(_counts_raw, dict):
        violations.append("counts must be a mapping (or omitted)")
    elif isinstance(_counts_raw, dict):
        sr = _counts_raw.get("shape_ratio")
        if sr is not None:
            if isinstance(sr, bool) or not isinstance(sr, (int, float)):
                violations.append("counts.shape_ratio must be numeric")
            else:
                shape_ratio = float(sr)

    # anti_horizontal: if present must be a mapping; `fired` must be a real bool
    # (rejects the bool("false") footgun — a string "false" is a violation).
    _ah_raw = block.get("anti_horizontal")
    ah_fired = False
    if _ah_raw is not None and not isinstance(_ah_raw, dict):
        violations.append("anti_horizontal must be a mapping (or omitted)")
    elif isinstance(_ah_raw, dict):
        fired_val = _ah_raw.get("fired", False)
        if not isinstance(fired_val, bool):
            violations.append("anti_horizontal.fired must be a boolean")
        else:
            ah_fired = fired_val

    valid = not violations

    nlj = False
    nlj_reasons: list[str] = []
    if valid:
        if parsed_decision == "accept" and unhandled_high:
            nlj = True
            nlj_reasons.append(
                f"{len(unhandled_high)} high-confidence candidate(s) unhandled + decision=accept: "
                f"{unhandled_high}")
        if parsed_decision == "accept" and ah_fired:
            nlj = True
            nlj_reasons.append("anti_horizontal fired (commit-history) + decision=accept")
        if high_sev_no_audit:
            nlj = True
            nlj_reasons.append(
                f"{len(high_sev_no_audit)} {'/'.join(high_sev_no_audit)} finding(s) with no audit_id")
        if parsed_decision == "accept" and shape_ratio >= SHAPE_RATIO_NLJ:
            nlj = True
            nlj_reasons.append(
                f"shape_ratio {shape_ratio} >= {SHAPE_RATIO_NLJ} (shape-dominant) + decision=accept")

    if not valid:
        next_step = f"fix {len(violations)} validation error(s) before adjudication"
    elif nlj:
        next_step = ("ledger valid but needs_llm_judgement fired; run /audit on the "
                     "flagged focus prompt(s) and record audit_id before accepting")
    else:
        next_step = ("ledger valid; paste test_quality_review block into "
                     "`.aqg/current_ledger.md` Evidence section")
    return {
        "valid": valid, "violations": violations, "decision": parsed_decision,
        "unhandled_high_candidates": unhandled_high, "shape_ratio": shape_ratio,
        "anti_horizontal_fired": ah_fired, "high_severity_no_audit": high_sev_no_audit,
        "needs_llm_judgement": nlj, "needs_llm_judgement_reasons": nlj_reasons,
        "next_safe_step": next_step,
    }


def _invalid(msg: str) -> dict:
    return {
        "valid": False, "violations": [msg], "decision": None,
        "unhandled_high_candidates": [], "shape_ratio": 0.0,
        "anti_horizontal_fired": False, "high_severity_no_audit": [],
        "needs_llm_judgement": False, "needs_llm_judgement_reasons": [],
        "next_safe_step": "fix 1 validation error before adjudication",
    }


# ===== CLI =====================================================================


def _read_input(path: str | None) -> str:
    if path and path != "-":
        return Path(path).read_text(encoding="utf-8", errors="replace")
    return sys.stdin.read()


def cmd_analyze(args: argparse.Namespace) -> int:
    diff_text = _read_input(args.diff_file)
    files = parse_diff(diff_text)
    commits = None
    if args.commits_numstat:
        commits = parse_commit_numstat(_read_input(args.commits_numstat))
    result = analyze(files, commits=commits)
    ledger = render_ledger_yaml(result)

    if args.json:
        print(json.dumps({
            "candidates": [c.__dict__ for c in result.candidates],
            "counts": {**result.counts.__dict__, "shape_ratio": result.counts.shape_ratio},
            "bulk_shape_test_risk": result.bulk_shape_risk,
            "anti_horizontal": result.anti_horizontal,
            "source_changed": result.source_changed, "test_changed": result.test_changed,
            "preliminary_needs_llm_judgement": result.preliminary_nlj,
            "preliminary_needs_llm_judgement_reasons": list(result.preliminary_nlj_reasons),
            "focus_prompts": FOCUS_PROMPTS,
            "ledger_yaml": ledger,
        }, indent=2, ensure_ascii=False))
        return 0

    print("# AQG Test-Quality Review — ledger + focus prompts (mode=analyze)")
    print()
    print(f"- generated_at: {_dt.datetime.now(_dt.timezone.utc).isoformat()}")
    print(f"- files: source_changed={result.source_changed} test_changed={result.test_changed}")
    print(f"- candidates: {len(result.candidates)} "
          f"(high={sum(1 for c in result.candidates if c.confidence == 'high')})")
    print(f"- bulk_shape_test_risk (advisory, NOT anti-horizontal): {result.bulk_shape_risk}")
    print(f"- anti_horizontal (commit-history): {result.anti_horizontal['fired']} "
          f"— {result.anti_horizontal['reason']}")
    print()
    if result.candidates:
        print("## Mechanical candidates (LLM dispositions each — NOT verdicts)")
        print()
        print("| id | concern | conf | evidence | pattern |")
        print("|---|---|---|---|---|")
        for c in result.candidates:
            print(f"| {c.id} | {c.concern} | {c.confidence} | `{c.evidence}` | {c.pattern} |")
        print()
    print("## Per-concern focus prompts (paste as `focus=<prompt>` to /audit)")
    print()
    for k, v in FOCUS_PROMPTS.items():
        print(f"### {k}")
        print(f"> {v}")
        print()
    print("## Closeout-importable YAML ledger")
    print()
    print("```yaml")
    print(ledger.rstrip())
    print("```")
    print()
    print("## Next safe step")
    print("- Check `docs/policies/audit-trigger.md` for this logical change. If it "
          "routes to `/audit`, run one policy-depth audit with the relevant focus "
          "prompt(s), fill candidate dispositions + findings, then "
          "`aqg_test_quality_review.py validate --file <ledger>`.")
    print("> Per ADR section 5: this script emits SIGNAL only. Caller runs /audit when policy routes it.")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    if not args.file:
        print("ERROR: --file required", file=sys.stderr)
        return 2
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2
    result = validate_ledger(path.read_text(encoding="utf-8", errors="replace"))

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["valid"] else 1

    suffix = " (NEEDS LLM JUDGEMENT)" if result["needs_llm_judgement"] else ""
    head = "VALID" if result["valid"] else "INVALID"
    print(f"# AQG Test-Quality Review verdict: {head}{suffix}")
    print()
    if result["violations"]:
        print("## Violations")
        for v in result["violations"]:
            print(f"- {v}")
        print()
    if result["valid"]:
        print("## Parsed summary")
        print(f"- decision: `{result['decision']}`")
        print(f"- shape_ratio: {result['shape_ratio']}")
        print(f"- unhandled high-conf candidates: {result['unhandled_high_candidates']}")
        print(f"- anti_horizontal_fired: {result['anti_horizontal_fired']}")
        print()
    if result["needs_llm_judgement"]:
        print("## needs_llm_judgement: TRUE")
        for r in result["needs_llm_judgement_reasons"]:
            print(f"- {r}")
        print()
    print("## Next safe step")
    print(f"- {result['next_safe_step']}")
    return 0 if result["valid"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="aqg_test_quality_review.py",
        description=("AQG review-side test-quality analysis (signal-only). Emits a "
                     "test-quality ledger + focus prompts; caller runs /audit when policy routes it."),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ap = sub.add_parser("analyze", help="parse a diff, emit test-quality ledger + focus prompts")
    ap.add_argument("--diff-file", default=None,
                    help="path to unified diff (default: stdin); '-' = stdin")
    ap.add_argument("--commits-numstat", default=None,
                    help="path to `git log --numstat --format=COMMIT:%%H --reverse <range>` "
                         "output to enable anti_horizontal temporal detection")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.set_defaults(func=cmd_analyze)

    vp = sub.add_parser("validate", help="validate a filled ledger, emit needs_llm_judgement")
    vp.add_argument("--file", required=True, help="path to filled YAML ledger")
    vp.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    vp.set_defaults(func=cmd_validate)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: unexpected: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
