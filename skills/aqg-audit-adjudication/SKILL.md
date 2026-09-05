---
name: aqg-audit-adjudication
description: Decide accept / reject / needs-user-decision for each Claude Opus, Gemini 3.1, o3, Codex internal-review, or local-audit finding and produce a structured adjudication table for AI team project work. Use AFTER any audit / review / second-opinion returns with a list of issues to integrate before fixing code, especially for production-adjacent scripts, ADRs, decision packets, CI failures, or evidence artifacts.
---

# AQG Audit Adjudication

Use this skill when review output arrives. Audit output is input to Codex judgment, not permission to stop.

## Workflow

1. Read the audit result completely.
2. Build an adjudication table:

   ```markdown
   | finding | decision | action | verification |
   |---|---|---|---|
   | <short finding> | accepted/rejected/needs-user-decision | <fix or reason> | <check to run> |
   ```

3. Classify each finding with canonical decision values:
   - `accepted`: concrete, correct, within current scope and permissions.
   - `rejected`: false positive, conflicts with project constraints, overbroad, or lower ROI than the risk. Give a technical reason. Before rejecting as a false positive, locate the exact `file:line` the finding cites — if the cited location doesn't exist or doesn't say what the finding claims, that absence is itself independent reject evidence (note it); if it does exist, the rejection must engage with what's actually there.
   - `needs-user-decision`: architecture direction, repo topology, production rollout/rollback, accepted/proposed ADR status, Owner-only permission, secret/credential, destructive action, product semantics, or user preference.
4. Apply every accepted fix that does not cross a stop boundary.
5. Run the listed verification checks.
6. Use the evidence closeout skill before final answer.

## Stop Boundaries

Stop only for:

- Owner-only or external permission action.
- Major architecture/repository/production/data-model decision.
- User preference or product semantics choice.
- External wait after all independent cleanup is done.
- Context risk requiring a paste-ready handoff.

If the review merely says "be careful" or "consider X", do not stop. Decide, document, and continue.

## Validator

When an adjudication table is saved to a file, validate it:

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
python3 "$aqg_root/scripts/validate_audit_adjudication.py" path/to/adjudication.md
```

The validator enforces both shape and content: exactly the `finding`, `decision`, `action`, `verification` columns (no missing / extra / duplicate columns, a separator row, matched row widths), no empty or placeholder cells, and a `needs-user-decision` row that names who and what is awaited. Prefer canonical decision values; Chinese / Owner-decision aliases are accepted for legacy notes.
