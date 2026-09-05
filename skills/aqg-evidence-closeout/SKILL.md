---
name: aqg-evidence-closeout
description: Evidence-backed closeout for AI team project tasks in any repository path, covering fresh verification, audit adjudication, durable state, and remaining blockers. Use before saying work is complete, before final answers after code/docs/status edits, before PR/issue handoff, after tests/checks, and whenever the task touched production-adjacent preflight, audit artifacts, status docs, or handoff prompts.
---

# AQG Evidence Closeout

Use this skill to prevent "summary without evidence." It turns completion into a short evidence ledger.

## Workflow

1. Rerun the smallest checks that prove the claim. Do not rely on old output.
2. Run the closeout helper to collect current repo state and print a ledger skeleton:

   ```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
python3 "$aqg_root/skills/aqg-evidence-closeout/scripts/aqg_closeout.py" --task "<short task name>"
```

   This helper defaults to the current git root. Add `--repo /path/to/repo` one or more times for multi-repo closeouts. It does not run tests or satisfy closeout by itself.

   **PR-D auto-import**: if the current repo has `.aqg/current_ledger.md` (from `aqg-code-construction` skill), closeout automatically imports + renders the parsed 6-step table, Warning Acknowledgements, and Predicted Objections sections, with secret patterns redacted (AWS / GitHub / Stripe / PEM). Use `--construction-ledger <path>` to override path or `--no-construction-import` to skip. Backwards compatible: silent fallback to skeleton-only if ledger missing.

   **Q7 Transfer Test Pack 7th-line**: if a Transfer Test Pack v1 run summary is found, closeout adds a 7th evidence-ledger row surfacing `threshold_met`. Failure renders prominently (`**FAIL — threshold NOT MET**`) so it cannot be missed; pass renders concisely; an inconsistent summary (e.g. `threshold_met: true` contradicting numerics) renders as `**INCONSISTENT** — TODO` (never silently PASS); an auto-discovery miss (no candidate summary found and no `--transfer-summary` passed) **omits the row entirely** — the Transfer Test Pack is opt-in, so a plain closeout is not polluted with a perpetual missing-pack row; an explicit `--transfer-summary` typo path still surfaces the actual path for diagnosis. Candidate paths checked in order: `<repo>/.aqg/transfer/last_run_summary.yaml` (canonical) → `<repo>/run_summary.yaml` (README quickstart default) → `<repo>/tests/transfer/_last_run_summary.yaml`. Override via `--transfer-summary <path>` or skip via `--no-transfer-import`.
3. Fill every TODO manually from fresh evidence in the current session:
   - exact commands run and exit status
   - tests/checks and results
   - changed files or PR/issue/status updates
   - audit adjudication result if an audit was used
   - explicit things not done, especially production writes, deploys, restarts, secrets, raw/private data, or authorization packets
4. If the task changed durable project truth, update the durable place before final answer:
   - PR body/comment, issue comment, `STACK_STATUS.md`, `PROJECT_STATUS.md`, tracker, or handoff prompt as appropriate.
5. If evidence is missing, say the task is not fully closed and name the blocker.

## Completion Standard

Do not claim completion unless the response can answer:

- What changed?
- What proves it?
- What was deliberately not touched?
- What remains blocked, if anything?
- Is the worktree clean or intentionally dirty?

When the closeout is a per-item table (one row per change or claim), tag each
row with a status so a partial result can't wear a "done" badge:

- **DONE** — built + freshly verified.
- **PARTIAL** — started; some verified. Name what's left.
- **NOT_DONE** — declared in scope but not attempted.
- **CHANGED** — goal met via a *different* approach than planned (prevents both
  the over-claim "did the plan" and the under-claim "didn't do X" when X was
  deliberately replaced).
- **UNVERIFIABLE** — done, but no fresh check could confirm it; say why.

## Final Answer Shape

Keep the user-facing closeout concise:

```markdown
Done <scope>.

Verification:
- `<command>` -> <result>

Boundaries:
- Not touched <prod/deploy/restart/secret/raw-data/etc.>

Remaining:
- <none or blocker>
```

For sensitive or production-adjacent work, include exact dates and current branch/PR facts.

## Boundaries

Closeout surfaces evidence; it does not authorize anything. Before claiming completion:

- This skill prints an evidence ledger skeleton to stdout for the user to save; it never writes the filesystem, never pushes, deploys, or merges. If you save the output (typical: `docs/decisions/`, `docs/incidents/`), that write is the user's action, not the skill's.
- The helper ALWAYS exits 0 — it is a reporter, not a gate. Missing / malformed / inconsistent imported evidence renders as a not-done or FAIL row in the output; gate on those rendered rows, never on the exit code.
- Imported evidence (construction ledger cells + Transfer Test Pack fields) is secret-redacted and table-escaped before rendering; redaction is fail-closed (a local secret-pattern fallback applies even when the construction parser is absent).
- Production / deploy / restart / secrets / raw private data / Owner-admin actions remain separate authorization gates — list them in the "Not touched" line, never claim them done from this skill alone.
- Audit adjudication, code-review accept/reject, and PR/issue handoff are explicit upstream steps. If they are skipped, mark the closeout as not fully closed and name the blocker.
- Auto-imported `aqg-code-construction` ledger is read-only; closeout never edits the ledger or the source files it references.
- Auto-imported Transfer Test Pack run summary is read-only; closeout parses minimal fields (run_id / date / overall_pass_rate / threshold_strict / threshold_met / task5_advisory) and never writes the summary file. Schema validation of the summary is NOT performed by closeout (run `validate_transfer_record_v1.py` upstream); closeout treats schema drift as MALFORMED and surfaces a TODO row, never raises.
