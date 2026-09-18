---
name: aqg-multi-review
description: Route non-trivial multi-file code changes through a 5-dimension review plan (logic / edge_cases / security / performance / concurrency) or validate a filled multi-review YAML ledger. Use when a change needs balanced cross-LLM review coverage across several dimensions, not only security; not for security-only review, test-quality deep dives, inline PR comments, or running `/audit` itself.
---

# AQG Multi-Review

## Purpose

`aqg-multi-review` is a signal-only review router for substantive code changes.
It does not review code by itself and it does not call audit-mcp. It emits:

- per-dimension focus prompts for `logic`, `edge_cases`, `security`, `performance`, and `concurrency`;
- a closeout-importable YAML ledger skeleton;
- a validator verdict for a filled ledger, including `needs_llm_judgement` signals and next safe steps.

Use it when a change is large enough, risky enough, or cross-cutting enough that a single security pass or ordinary inline review would miss important dimensions.

## Position

| layer | tool | scope |
|---|---|---|
| pre-action | `aqg-startup-preflight` | worktree, context, and live-state risk |
| construction | `aqg-code-construction` | pattern mining, behavior lock, implementation, local verification |
| trigger meta-layer | `aqg-phase-transition` | when to audit and which depth the policy selects |
| dimension router | `aqg-multi-review` | five-dimension review dispatch and ledger validation |
| security specialist | `aqg-security-review` | deeper OWASP/CWE security review |
| pre-handoff | `aqg-evidence-closeout` | evidence, adjudication, durable state, remaining blockers |

The differentiation is balanced review coverage: each requested dimension is reviewed independently, then the filled ledger is checked for cross-dimension conflicts, suspicious accepts, missing audit evidence, and degraded fallback overclaims.

## When To Use

Use this skill for:

- a non-trivial multi-file change that needs coverage across logic, edge cases, security, performance, and concurrency;
- a user request for a five-dimension review plan, per-dimension audit focus prompts, or a cross-LLM review panel;
- validating a completed multi-review ledger before closeout;
- a subset review such as `security performance` when the user explicitly narrows the dimensions.

Prefer another tool when the request is narrower:

- security-only review: use `aqg-security-review`;
- test behavior, test weakening, flaky tests, or coverage gaps: use `aqg-test-quality-review`;
- audit depth selection only: use `aqg-phase-transition`;
- inline GitHub PR comments: use the host review tool instead;
- final completion evidence: use `aqg-evidence-closeout`.

## How To Run

Run the local script in this repository. This repo is the development source; the future `aqg-cli` form is packaging only and must remain behavior-identical to the local script.

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script="$aqg_root/skills/aqg-multi-review/scripts/aqg_multi_review.py"

# Generate a skeleton and all five focus prompts.
python3 "$script" new > /tmp/multi-review-skeleton.md

# Generate only selected dimensions.
python3 "$script" new --dimensions security performance > /tmp/sec-perf-only.md

# Validate a filled ledger.
python3 "$script" validate --file /tmp/multi-review-filled.md
python3 "$script" validate --file /tmp/multi-review-filled.md --json
```

Requires `PyYAML>=6.0` for `validate`.

## Operating Flow

1. Decide the dimension set. Default to all five dimensions unless the user narrows the review.
2. Get audit depth from `aqg-phase-transition` when it has already emitted a recommendation. If there is no phase-transition result, read `docs/policies/audit-trigger.md`; do not fall through to an audit tool default.
3. Run one external `/audit` per dimension with the generated focus prompt and the policy-selected depth.
4. Fill the YAML ledger from the independent audit results.
5. Run `validate` on the filled ledger.
6. If `needs_llm_judgement` fires, resolve the reasons before treating the ledger as closeout evidence.

The script's `new --fallback-session-llm` mode is only for engine-unavailable degraded operation. It is a single-model, same-vendor, non-independent rough evaluation. Keep `audit_id: null`, keep `fallback_mode: session-llm`, and prefer `decision: needs-cross-llm-rerun` so a real cross-vendor panel still runs later.

## Ledger Contract

The validator expects a top-level `multi_review:` mapping with:

- `dimensions_audited`: a list drawn from `logic`, `edge_cases`, `security`, `performance`, and `concurrency`;
- `findings`: a list of findings whose `dimension` is declared, whose `severity` is one of `CRITICAL`, `HIGH`, `MEDIUM`, or `LOW`, and whose `evidence` is non-empty;
- `decision`: `accept`, `reject`, or `needs-cross-llm-rerun`;
- `decision_reason`: a real reason, not a `TODO` placeholder;
- `audit_id`: a real audit id when an independent panel was run, or `null` for fallback mode.

Useful optional fields:

- `verified`: boolean only; `true` means the finding was independently reproduced;
- root-level `repo_overrides`: caller-supplied review tuning such as `ignore`, `emphasize`, or `notes`. It must stay at document root, not nested under `multi_review:`.

Evidence is also used for overlap detection. `file:line` and `file:start-end` evidence enables cross-dimension conflict grouping and `convergent_findings` confidence signals. In fallback mode, each finding must include a locatable `file:line` token with no space after the colon; prose-only fallback evidence is rejected because no independent panel corroborates it.

When `AQG_DE_RESULTS_DIR` points at a Decision Engine results store, `validate` cross-checks whether `audit_id` was actually issued on this machine. A missing id is downgraded to "no audit id" for `needs_llm_judgement` purposes. If the results directory is unset or unavailable, issuance is reported as `unverifiable` and prior behavior is preserved.

## Validation Signals

`validate` can return a structurally valid ledger and still emit `needs_llm_judgement`. Treat that as a real review stop, not as a formatting problem.

Primary triggers include:

- two or more dimensions flag overlapping evidence;
- three or more dimensions have findings but the decision is `accept`, and at least one finding is non-LOW or the ledger lacks a real audit id;
- the decision is `needs-cross-llm-rerun` but `audit_id` is null;
- any `CRITICAL` finding lacks a real audit id;
- the ledger declares reviewed dimensions, reports zero findings, and accepts the change.

The validator also emits `convergent_findings` when multiple dimensions point to the same location. Use those as prioritization evidence: two dimensions means elevated confidence; three or more means high confidence.

## Boundaries

- Signal only: this skill emits prompts, skeletons, and validation verdicts. It never calls `/audit`, audit-mcp, or external LLMs.
- Read-only by default: the entry script writes no repo files unless the caller redirects stdout.
- Depth authority stays in `docs/policies/audit-trigger.md`; this skill chooses dimensions, not audit depth.
- A fallback ledger is never a cross-vendor or independent panel. Any fallback marker combined with a real cross-panel `audit_id` is an overclaim and invalid.
- The leak gate is a best-effort drift tripwire for local fallback templates, not a cryptographic proof that no proprietary hub behavior leaked.
- Do not use this skill as a substitute for final evidence closeout, security-specialist review, or test-quality review.
