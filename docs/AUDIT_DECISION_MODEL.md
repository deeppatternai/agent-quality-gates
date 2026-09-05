# AQG Audit Decision Model — single source of authority

English | [中文](AUDIT_DECISION_MODEL.zh-CN.md)

> Within a single change: **who decides whether/how deeply to audit, where the external audit is triggered, what dimensions are audited, and what to do once it's done.**
> The "audit orchestration" sections of the rule templates (`CLAUDE.md` / `AGENTS.md`) point to this diagram, to avoid restating it in multiple places and drifting.
> This is the canonical home for "audit invocation conflicts": previously depth selection was encoded in 2 places, and self-review/dimensions were scattered across 3-4 places each telling its own story; this document collapses them into one chain.

## One diagram: audit orchestration for a single code change

```
session start → aqg-startup-preflight                (one-time worktree/state gate; not an audit)
             │
write code   → aqg-code-construction
             ├─ 6-step construction(workflow §2): step1–5(pattern→behavior→slice→rules→local-verify)
             │                          + step6 construction-phase self-review(5 axes, inline)   ← self-review first
             └─ audit-before-commit gate(workflow §5, after the 6 steps / before commit)   ← the only chokepoint for code external audit
                        │  depth = audit-trigger.md (the only source); phase-transition = timing
                        │  dimensions = multi-review(5-dim panel) / security-review(security single-dim)
                        ▼
                   de_audit(mode, focus)              ← audit-mcp, the real external-audit engine
                        │  any findings?
                        ▼
                   aqg-audit-adjudication             (accept / reject / needs-user-decision table)
commit → aqg-evidence-closeout(closeout gate, verifies the audit happened, does not audit itself)→ PR / handoff
```

> Numbering: `§2` / `§5` are the top-level subsection numbers under `aqg-code-construction` `## Workflow`; step1–6 live inside `§2` (**step6 = Self Review**).

## 1. Who decides the audit depth (fast / standard / deep)

**One authority: `docs/policies/audit-trigger.md`.** Its `depth-by-stakes` mapping
is the only place a depth is chosen — trivial → `skip`, moderate → `standard`, an
escalation gate → `deep`.

There is no longer a main-path / fallback pair. That framing named
`audit-self-routing.md` as the main path, but no installer ever shipped that file,
so the main path never fired and `aqg-phase-transition` was in practice the sole
decider — while carrying its own phase × stakes table that routed trivial changes
to `standard`, contradicting the policy's rung 1. Two rulebooks, opposite answers,
and the "take the deeper signal" rule meant the stricter one silently won. Owner
ruling 2026-08-11 collapsed them.

| Role | Tool | What it decides |
|---|---|---|
| **Depth** | `docs/policies/audit-trigger.md` | how deep, from stakes — the only source |
| **Timing** | `aqg-phase-transition` | *when* to ask, at PLAN / IMPL / TESTS boundaries; reads depth from the policy |

- **high stakes is never below deep** (safety floor; even a user's "quick scan" cannot lower it; the only exception is an explicit "don't audit" = skip opt-out, which on high stakes requires a second confirmation).
- A `skip` sourced from the depth mapping is the policy speaking, not a user opt-out, and is never treated as one.
- phase-transition is **signal-only**; it does not call the audit tool itself (ADR §5 boundary).

## 2. Who is the external-audit trigger entry (chokepoint)

| Change type | chokepoint | Timing |
|---|---|---|
| **Code** | the **audit-before-commit gate** of `aqg-code-construction`(workflow §5, after the 6-step construction) | before `git commit` |
| **Design contract**(DesignSpec / ADR / schema) | **default pass1 Deep**(discovery + revision, triggered before `PLAN_DONE`/finalization); if pass1 prompts revisions then a **pass2 Deep** verification close-out follows(pass1 clean → pass2 skipped/quick confirm). **trivial doc edits don't take this path; route them per §1** | before finalization |
| **Closeout / PR / handoff** | `aqg-evidence-closeout`(verifies the audit happened, **does not audit itself**) | before claiming done |

→ Each scenario **has its own single chokepoint** (table above); phase-transition / multi-review / security-review are all **inputs** feeding the chokepoint, not parallel entries.

## 3. Who supplies the dimensions

| Tool | Dimensions | Purpose |
|---|---|---|
| `aqg-multi-review` | 5-dim cross-LLM external panel: logic / edge_cases / security / performance / concurrency | Large changes (≥200 LOC or ≥3 files) needing multi-dimensional independent judgement |
| `aqg-security-review` | security single-dim in-session: OWASP Top 10 + CWE Top 25 | Security-sensitive code (auth/crypto/input/SSRF…) |

## 4. Self-review ≠ external audit (don't treat the external audit as the first reviewer)

- **Construction-phase self-review** = **step6 Self Review** of `code-construction`'s 6-step construction, 5 axes: correctness / readability / architecture / security / performance — an inline quick check, completed **before** the §5 gate.
- **External panel** = `multi-review`'s 5 dimensions — cross-LLM, run by `de_audit`, **different training distributions = genuinely independent judgement**.
- The two are **two sets of axes for different purposes; they don't need to be unified into one**; internal consistency within each is enough.
- **Rule**: before the external audit you **must first do a structured multi-dimensional self-review — an inline per-dimension evaluation suffices**; treating `de_audit` as the **first** reviewer = an AQG-design anti-pattern.
  (`aqg-multi-review` is the orchestrator for the external panel; its `new` subcommand can optionally **generate a dimension-prompt skeleton** to aid self-review, but the self-review action itself is inline and does not require running the external panel first.)

## 5. Who handles the audit output

`aqg-audit-adjudication` — give each finding `accepted` / `rejected` / `needs-user-decision`:
accepted gets implemented, rejected gets a technical rationale, needs-user-decision names the actor + the specific decision.
The audit output is an **input**, not a reason to stop.

## 6. Tier names

- **Tiers**: `fast` / `standard` / `deep` (plus `skip` when no audit is warranted).
- The **voice roster** (which LLMs sit at which tier) is **owned by decision-engine**; neither this doc nor the policy **enumerates** it — the roster changes across versions (voices added/removed), so the rule layer only recognizes tier names and never hard-codes the list.

---

> Maintenance: this document is the source-of-truth for audit orchestration. When changing it, also check that the "audit orchestration" pointer sections in
> `examples/aqg-claude-rules.example.md` / `examples/aqg-codex-agents.example.md`
> are still consistent (they only carry a condensed version + a pointer back here), and verify against
> `skills/aqg-code-construction/SKILL.md` that its §2 (6 steps, step6 self-review) / §5 (audit-before-commit gate) numbering is unchanged.
