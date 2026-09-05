# Audit depth selection — retired rule, see the policy

> **This rule is retired.** It used to be the "main path" for choosing external-audit
> depth (`fast` / `standard` / `deep`), with `aqg-phase-transition` as its fallback.
> Both halves of that model are gone.

**Where depth comes from now.** The `depth-by-stakes` mapping in
[`docs/policies/audit-trigger.md`](../docs/policies/audit-trigger.md) is the single
authority (Owner ruling 2026-08-11). Depth is a function of stakes alone:

| stakes | depth |
|---|---|
| trivial (rung 1, no gate fired) | `skip` |
| moderate (rung 2, substantive, non-sensitive) | `standard` |
| high (an escalation gate fired) | `deep` |

**Where timing comes from now.** `aqg-phase-transition` decides *when* to ask — at the
`PLAN_DONE` / `IMPL_DONE` / `TESTS_WRITTEN` boundaries — and reads depth from the policy.
It no longer carries a phase × stakes matrix of its own.

**Why the old rule was retired.** It carried a second, independent depth table that
routed trivial changes to `standard`, contradicting the policy's rung 1. Because the
decision model takes the deeper of two signals, the duplicate table silently won,
making it a source of audit over-firing.

**Nothing to adopt from this file.** The AQG rule templates
([`aqg-claude-rules.example.md`](aqg-claude-rules.example.md),
[`aqg-codex-agents.example.md`](aqg-codex-agents.example.md)) point their audit
orchestration section at the policy directly. See
[`docs/AUDIT_DECISION_MODEL.md`](../docs/AUDIT_DECISION_MODEL.md) for how the whole
audit chain fits together — who decides depth, where the audit is triggered, which
dimensions, and how findings are adjudicated.
