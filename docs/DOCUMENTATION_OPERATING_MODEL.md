# Documentation Operating Model

## Principle

Every durable project decision or quality-gate behavior change should have a landing place. Do not rely on chat history as the only record.

Records should be concise and evidence-backed. Store enough to reconstruct what changed and why, but do not store secrets, raw private data, production credentials, or full sensitive logs.

## Directory Map

| path | purpose |
|---|---|
| `docs/ENGINEERING_FRAMEWORK.md` | the general-purpose engineering-discipline contract (shared across projects) |
| `docs/AGENT_COMPATIBILITY_STRATEGY.md` | cross-agent strategy for Codex, Claude Code, and future agent packs |
| `docs/DOCUMENTATION_OPERATING_MODEL.md` | rules for where records land |
| `docs/decisions/` | ADR-style decisions for config schema, strictness policy, CI behavior, or repo boundaries |
| `docs/bugfixes/` | bug fix records for gate/script/skill behavior changes |
| `docs/gate-rollouts/` | rollout records for warn-only or blocking adoption in a target repo |
| `docs/audit-evidence/` | summarized audit/evidence records with redaction proof |
| `examples/` | copyable configs and workflows |
| `templates/` | reusable PR, decision, bugfix, rollout, and audit/evidence templates |

## When To Create A Record

Create a bugfix record when:

- a script behavior is wrong
- a gate has a false positive or false negative
- CI adapter behavior changes after a failed run
- path portability, auth handling, or parsing behavior is fixed

Create a decision record when:

- choosing config format or schema semantics
- choosing project identity or agent-pack layout
- changing a default strictness policy
- deciding what is blocking versus warn-only
- introducing a dependency
- changing how private data is represented

Create a gate rollout record when:

- a target repo starts warn-only quality gates
- a target repo moves a gate to blocking
- a gate is temporarily disabled or overridden
- a false-positive policy is changed

Create an audit/evidence record when:

- external audit materially changes implementation
- dual/triple audit is used
- production-adjacent or private-data-sensitive evidence is summarized
- evidence is intentionally kept outside the repo

## Naming

Use date-prefixed lowercase slugs:

- `docs/bugfixes/<YYYY-MM-DD>-<slug>-a<n>.md`
- `docs/decisions/<YYYY-MM-DD>-<slug>-a<n>.md`
- `docs/gate-rollouts/<YYYY-MM-DD>-<slug>-a<n>.md`
- `docs/audit-evidence/<YYYY-MM-DD>-<slug>-a<n>.md`

Record files should include an `id` field matching the filename stem. Use a short suffix to avoid same-day slug collisions.

## Bug Fix Record Requirements

Required fields:

- symptom
- root cause
- fix
- verification
- regression coverage
- affected gate or script
- whether the behavior is backward compatible

Do not merge a gate semantics bug fix without at least one fixture or self-test that would have caught it.

## Decision Record Requirements

Required fields:

- context
- decision
- alternatives considered
- consequences
- reversal or migration plan
- validation evidence

Keep decisions short. If a decision needs extensive data, link to an audit/evidence record.

## Gate Rollout Record Requirements

Required fields:

- target repo
- mode: warn-only or blocking
- gates enabled
- expected failures
- override path
- rollback plan
- Owner/admin actions needed
- first-run evidence

Do not claim a repo is protected by a gate until blocking mode is live and verified.

## Audit/Evidence Record Requirements

Required fields:

- scope
- inputs
- audit/review method
- findings summary
- adjudication table link or embedded table
- verification commands
- redaction statement
- remaining blockers

For private/raw data, prefer:

- path
- row count
- hash
- schema
- redacted excerpt only when explicitly allowed

## Red Lines

Never commit:

- API tokens, passwords, private keys, cookies, or session files
- production credentials or host access details beyond approved identifiers
- raw private customer/user data
- full production logs if they contain secrets or private data
- hidden authorization assumptions

## Review Checklist

Before final answer or push:

- docs updated if user-facing behavior changed
- relevant record added if behavior/semantics changed
- validation commands rerun
- secret scan completed
- worktree status checked
