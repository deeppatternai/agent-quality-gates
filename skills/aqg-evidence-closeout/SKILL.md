---
name: aqg-evidence-closeout
description: Produce an evidence-backed closeout for project work, covering fresh verification, audit adjudication, durable state, untouched boundaries, worktree state, and remaining blockers. Use before claiming completion, posting a final answer, or PR/issue handoff; not to run checks, perform audits, or authorize production actions.
---

# AQG Evidence Closeout

Use this skill to prevent "summary without evidence." It turns a completion claim into a concise, reviewable evidence ledger.

## Quick Contract

- **When**: immediately before claiming completion, posting a final answer after project changes, or handing work to a PR, issue, owner, or next session.
- **What it produces**: current repository state plus six required evidence claims covering scope, verification, audit disposition, durable state, protected boundaries, and blockers.
- **Completion rule**: use overall `DONE` only when every required claim has fresh evidence, the worktree state is understood, and no blocker remains.
- **What it does not do**: run tests, perform or adjudicate an audit, authorize a mutation, deploy, merge, or prove that the evidence is semantically correct.
- **Language hygiene**: keep the ledger in one working language unless quoting commands, output, identifiers, or source material.

## Workflow

1. Rerun the smallest checks that prove the completion claim. Do not rely on old output.
2. Run the closeout helper to collect current repository state and print a ledger skeleton:

   ```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
python3 "$aqg_root/skills/aqg-evidence-closeout/scripts/aqg_closeout.py" --task "<short task name>"
```

   The helper defaults to the current Git root. Add `--repo /path/to/repo` one or more times for a multi-repository closeout. It only prints a skeleton; it does not run checks or complete the closeout by itself.
3. Review any auto-imported evidence, then fill every `TODO` from fresh evidence in the current session.
   `TODO` is a skeleton placeholder, not a valid claim status; replace every
   placeholder with `DONE`, `PARTIAL`, `BLOCKED`, `NOT_DONE`, or `UNVERIFIABLE`
   before submitting the closeout. The strict checker rejects unresolved `TODO`.
4. Assign a claim status from `DONE`, `PARTIAL`, `BLOCKED`, `NOT_DONE`, or `UNVERIFIABLE`. Record a changed implementation approach in the evidence text; `CHANGED` is not a completion status.
5. If durable project truth needs updating, perform that as a separate caller action under normal scope and authorization. If it is not done, mark the closeout `PARTIAL` or `BLOCKED` and name the missing update.
6. Save the completed closeout when a durable artifact is needed, then run the strict completeness checker shown below.
7. Write the final answer from the completed ledger. Do not use `DONE` when evidence is missing, stale, unverifiable, or blocked.

## Required Evidence Contract

The evidence ledger has six required claims. Repository/worktree state is supporting metadata, not a seventh claim.

| claim | required evidence |
|---|---|
| `scope completed` | changed files, PR/issue/status update, or other concrete scope artifact |
| `verification run` | exact fresh command plus exit status or summarized result |
| `audit adjudicated` | audit ID/table and disposition, or a concrete reason no audit was required |
| `durable state updated` | PR body, issue, status document, tracker, handoff, or a concrete reason no update was needed |
| `production boundary` | explicit non-actions or separately authorized action evidence |
| `remaining blockers` | `none` or the exact unresolved dependency, decision, authorization, or failed check |

Also report whether each worktree is clean or intentionally dirty. An unexpected dirty worktree prevents overall `DONE` until explained or resolved.

When explaining or enumerating the six required claims, list all six and only
these six: `scope completed`, `verification run`, `audit adjudicated`, `durable
state updated`, `production boundary`, and `remaining blockers`. Worktree state
is supporting metadata; it is not a seventh claim and must not replace any of
the six claims.

In Chinese, use this fixed mapping when naming the claims: `范围完成`、`最新
验证`、`审计裁决`、`持久状态更新`、`生产边界`、`剩余阻塞项`. Do not count
`worktree` as one of these six names.

### Claim Statuses

- **DONE** — the claim is satisfied by fresh evidence.
- **PARTIAL** — some evidence exists, but the claim is not fully satisfied.
- **BLOCKED** — completion requires external input, authorization, dependency, or state change.
- **NOT_DONE** — the claim was in scope but was not attempted.
- **UNVERIFIABLE** — work may be complete, but no fresh check can confirm it; overall status cannot be `DONE`.

For an overall closeout, an unresolved required external authorization,
production gate, dependency, or owner decision is `BLOCKED`, not merely
`PARTIAL`. Use `PARTIAL` for incomplete evidence without a hard external
gate; never downgrade a missing mandatory production authorization to
`PARTIAL`.

If the goal was met through a different approach than planned, keep the claim's real status and explain the approach change in its evidence.

## Optional Imports

- **Code construction ledger**: when `.aqg/current_ledger.md` exists, the helper imports its header, six-step table, Behavior Contract, Warning Acknowledgements, and Predicted Objections. Use `--construction-ledger <path>` to override or `--no-construction-import` to skip. Auto-discovery remains silent when no ledger exists, but if the task required `aqg-code-construction`, a missing ledger is a closeout blocker that must be recorded manually.
- **Transfer Test Pack**: when a v1 run summary is found, the helper adds an optional evidence row for `threshold_met`. A failed, inconsistent, malformed, unreadable, or explicitly missing summary never renders as a pass. Use `--transfer-summary <path>` to override or `--no-transfer-import` to skip; run `--help` for candidate paths.
- **Imported values**: construction and transfer values are secret-redacted, table-escaped, and read-only before rendering.

## Completeness Check

The helper always exits `0`: it is a reporter, not a gate. After replacing placeholders and assigning statuses, validate a saved Markdown closeout with:

```bash
python3 "$aqg_root/scripts/check_evidence_closeout.py" <closeout.md> --strict
```

The checker fails when a required evidence item is missing or still contains a placeholder. It does not judge evidence quality, audit necessity, authorization, or whether a `PARTIAL`/`BLOCKED` closeout should be promoted to `DONE`; those remain semantic decisions for the caller.

## Final Answer Shape

Keep the user-facing closeout concise and use the labels below literally. The
first line must be `Status: <state>`; do not render `Status` as a Markdown-only
heading such as `**Status**`. Keep the labels `Status`, `Scope`, `Verification`,
`Audit/Review`, `Durable State`, `Worktree`, `Boundaries`, and `Remaining`
exactly as shown, even when the evidence bullets use Chinese.
This exact first-line status rule also applies when the caller provides only
user-supplied facts or asks for an interim/multi-turn update; never replace it
with a prose-only conclusion such as "尚不能关闭". When the caller asks you
to explain language hygiene, include an explicit `Language hygiene:` line (or
the fixed Chinese label `语言卫生：`) and state that one working language is
used, with English retained only for technical identifiers, quoted output, or
source material.

```markdown
Status: <DONE | PARTIAL | BLOCKED | NOT_DONE | UNVERIFIABLE>

Scope:
- <what changed>

Verification:
- `<command>` -> <result>

Audit/Review:
- <audit/adjudication result, or why none was required>

Durable State:
- <updated artifact, or why no update was needed>

Worktree:
- <clean or intentionally dirty, with reason>

Boundaries:
- Not touched <production/deploy/restart/secrets/raw-data/etc.>

Remaining:
- <none or exact blocker>
```

For sensitive or production-adjacent work, include exact dates and current branch/PR facts.

If a required production deploy or restart authorization is still missing,
the first line must be exactly `Status: BLOCKED`; do not report only
`PARTIAL` for that unresolved production gate.

## Boundaries

- The helper is read-only: it prints to stdout and never saves files, pushes, deploys, merges, or updates remote systems.
- Saving the output or updating a PR, issue, status document, tracker, or handoff is a separate caller action governed by normal repository scope, permissions, and authorization.
- Production writes, deploys, restarts, secrets, raw private data, and Owner/admin actions remain separate authorization gates. Closeout may report their status but never authorizes them.
- Audit execution, audit adjudication, code-review acceptance, and PR/issue handoff occur upstream. If policy requires one and it was skipped, overall status is not `DONE`.
- Auto-imported construction and transfer evidence is read-only. Transfer summary schema validation remains upstream in `validate_transfer_record_v1.py`; malformed input is surfaced, not raised or silently passed.
