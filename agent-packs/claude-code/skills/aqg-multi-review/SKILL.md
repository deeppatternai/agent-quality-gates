---
name: aqg-multi-review
description: Use PROACTIVELY for a non-trivial change — a multi-file feature or larger — that needs balanced coverage across all five review dimensions, not a security pass alone; also use it to validate a filled dimension ledger. 5-dimension code review router (logic / edge_cases / security / performance / concurrency) — a cross-LLM panel where each dimension is judged by independent LLMs, not a single vendor's PR review. Two modes — `new` returns dimension-specific focus prompts plus a closeout-importable YAML skeleton; `validate` parses a filled ledger and emits `needs_llm_judgement` for cross-dimension conflicts, unfilled declared dimensions, or 3+ dimensions with findings plus an accept decision when at least one finding is non-LOW or an audit_id is missing. For test quality specifically, see aqg-test-quality-review. The skill does not call audit-mcp; the caller runs `/audit focus=<dim>` per dimension at the depth `docs/policies/audit-trigger.md` selects. Does not replace the host built-in /review slash command.
---

# AQG Multi-Dimension Review

## Three-layer position

| layer | tool | scope |
|---|---|---|
| pre-action | aqg-startup-preflight | worktree / context / live state |
| **dimension router** | **aqg-multi-review (this)** | **5-dim x cross-LLM review dispatch** |
| trigger meta-layer | aqg-phase-transition | when / how-deep to audit |
| security specialist | aqg-security-review | deep OWASP/CWE single dimension |
| pre-handoff | aqg-evidence-closeout | evidence ledger |

multi-review is the cross-LLM counterpart to Anthropic's /ultrareview; key differences:

| | Anthropic /ultrareview | aqg-multi-review |
|---|---|---|
| dimensions | 5 (logic / edge / security / perf / concurrency) | 5 (same) |
| verification method | single Claude × 5 agents (cloud fleet) | **cross-LLM panel** × 5 dim (caller invokes audit-mcp /audit, multiple independent LLM voices cross-validate) |
| output | GitHub PR inline comments | YAML ledger (closeout-importable) + verdict markdown |
| client | Claude Code | **cross-client** (cli / Codex / any) |
| cost | cloud billed per run | subscription auditors primarily (low cost) |

## How To Run

> **Repo context**: agent-quality-gates is the **development source** — the
> skill's decision logic lives here in the local script, in the open. Run it
> directly with the script below. The `aqg-cli` / cloud form is the future
> protected-IP release packaging (a thin client repackaged from this once it is
> mature), shown second for reference — not how you run the skill in this repo.

### Run it — local script (this repo, the development source)

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script="$aqg_root/skills/aqg-multi-review/scripts/aqg_multi_review.py"

# 1. Generate skeleton (default = all 5 dims; subset opt-in via --dimensions)
python3 "$script" new > /tmp/multi-review-skeleton.md
python3 "$script" new --dimensions security performance > /tmp/sec-perf-only.md

# 2. (caller fills YAML ledger from /audit panel results)

# 3. Validate filled ledger; exit 0 valid / 1 invalid
python3 "$script" validate --file /tmp/multi-review-filled.md
python3 "$script" validate --file /tmp/multi-review-filled.md --json  # machine-readable
```

Requires `pip install pyyaml>=6.0`.

### Cloud release form (future packaging — reference only)

When repackaged for distribution, the same two steps run through the thin
client and the decision logic moves server-side (that move is what protects it):

```bash
aqg-cli multi-review new --diff-file changes.diff > /tmp/multi-review-skeleton.md
# for each dim: /audit focus=<dim prompt> mode=<phase-transition recommended,
#               else read the depth from docs/policies/audit-trigger.md>
aqg-cli multi-review validate --file /tmp/multi-review-filled.md
```

`tests/test_multi_review.py` guards that the eventual cloud release form stays
behavior-identical to this development source (the source currently leads).

## Boundaries

Decision rules (matrix per dim, cross-dim conflict detection, needs_llm_judgement
heuristic) live in the local script here — this repo is the open development
source. In the future cloud release form they move server-side
(`/api/v1/skill/aqg-multi-review/verdict`), which is what keeps them protected
once packaged for distribution.

This skill **does not call audit-mcp** (per ADR §5 + ADR
`2026-05-09-multi-dimension-review-skill-a1.md`). It emits dimension-specific
focus prompts + ledger skeleton; caller invokes `/audit` per dim with
`focus=<dim>` and `mode=<phase-transition recommended>`.

If `aqg-phase-transition` was not invoked there is no recommended mode to
substitute. Do not fall through to the tool default — read the depth from
`docs/policies/audit-trigger.md` (either escalation gate fired → `deep`; rung 1,
trivial and non-sensitive → skip the dim; otherwise → `standard`).

## When to use vs alternatives

- **single security audit, none of the other 4 dims** → `aqg-security-review` (deeper OWASP/CWE)
- **unsure how deep to audit** → first run `aqg-phase-transition emit` to get recommended_audit_mode
- **want inline GitHub PR comments** → Claude Code `/ultrareview` (bound to a GitHub PR; the AQG verdict is a markdown ledger)
- **cross-LLM verification + cross-client + 5 dim** → **this skill** (strongest differentiation)
