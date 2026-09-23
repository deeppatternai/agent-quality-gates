---
name: aqg-phase-transition
description: Use at phase boundaries after a plan/design doc, a non-trivial implementation, or written tests to emit PLAN_DONE, IMPL_DONE, or TESTS_WRITTEN and return the recommended audit mode. Signal-only; it reads depth from docs/policies/audit-trigger.md, applies dedup/user-signal/safety-floor rules, and leaves the caller to run or skip /audit.
---

# AQG Phase-Transition Audit Trigger

Use this skill at the **three AQG phase boundaries** to ask whether an audit
decision is due before moving to the next phase. The skill emits a timing signal,
records local phase state, and returns `recommended_audit_mode`. It does not call
`/audit`; the caller decides whether to invoke or skip the audit tool.

## Three-layer position

| layer | tool | scope |
|---|---|---|
| pre-action gate | `aqg-startup-preflight` | worktree / context / live state |
| **phase-boundary signal** | **aqg-phase-transition (this)** | **PLAN -> IMPL -> TESTS -> RUN boundaries** |
| pre-handoff gate | `aqg-evidence-closeout` | 6-question evidence ledger |

Phase-transition fills the **middle ground** between preflight (before acting) and closeout (before completion).

## How To Run

Resolve the AQG root, then run the helper:

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script="$aqg_root/skills/aqg-phase-transition/scripts/aqg_phase_emit.py"

# emit a phase
python3 "$script" emit \
  --phase plan_done \
  --stakes moderate \
  --task "sprint-11a-aqg-automation-audit" \
  --artifact-file /path/to/plan.md

# query current state
python3 "$script" query --task "sprint-11a-aqg-automation-audit"

# user override (downgrade or skip)
python3 "$script" override --task "sprint-11a-aqg-automation-audit" --user-signal "quick scan"
```

Or via cloud (thin-client):

```bash
aqg-cli phase emit --phase plan_done --stakes moderate --task "..." --artifact-file plan.md
aqg-cli phase query --task "..."
aqg-cli phase override --task "..." --user-signal "quick scan"
```

The verdict prints `recommended_audit_mode` (`fast` / `standard` / `deep` / `skip`), `reason`, `matrix_default`, and decision fields including `user_override_applied`, `safety_floor_applied`, `high_stakes_skip_confirm`, `dedup_hit`, and `dedup_reason`. The **caller** (you / Claude session / EAF) is responsible for invoking `/audit` at that mode. AQG itself does NOT call the audit tool (per ADR §5 boundary).

## State and Query Contract

Each task persists to its own local file under the project root:
`.aqg/phase-state-<safe-task>-<short-hash>.json`. The safe task label is derived
from the task ID; the short hash is derived from the raw task ID and prevents
collisions after sanitizing or truncating the label. Query that same task with
`python3 "$script" query --task "<task-id>"` (or `aqg-cli phase query --task
"<task-id>"` in cloud thin-client form). State is local and is not synced to the
cloud.

## Depth comes from the policy, not from this skill

This skill decides **when** to ask (the three phase boundaries) and applies dedup,
user signals and the safety floor. It does **not** own a depth table: depth is read
from the `depth-by-stakes` mapping in `docs/policies/audit-trigger.md`, which is the
single authority (Owner ruling 2026-08-11).

| stakes | depth |
|---|---|
| trivial (rung 1, no gate fired) | `skip` |
| moderate (rung 2, substantive, non-sensitive) | `standard` |
| high (an escalation gate fired) | `deep` |

Phase does not affect depth — a trivial change is trivial at `PLAN_DONE` and at
`IMPL_DONE` alike. The table above is a convenience copy; the policy wins. Both the router and
this table are asserted against the policy marker by
`test_router_depth_matches_the_policy_single_source` and
`test_skill_md_depth_table_matches_the_policy`.

Previously this skill carried its own phase × stakes matrix that routed trivial to
`standard`, contradicting the policy's rung 1. Because the decision model takes the
deeper of two signals, this skill silently won — making it a second source of the
over-firing that work removed.

**Safety floor**: high stakes is not allowed to drop below `deep` (even if the user asks for a quick scan; the only exception is an explicit no-audit opt-out). The high column is filled directly with `deep`, not silently raised by the floor. When a lower non-skip request is raised to `deep`, the verdict emits `safety_floor_applied=true`.

**High-stakes skip second confirmation** (issue #282): an explicit no-audit opt-out on high stakes is still honored (mode=`skip`, ADR §4 invariant 4), but the verdict additionally emits `high_stakes_skip_confirm=true` — the caller **must first obtain a second explicit confirmation from the user** before honoring this skip; without that re-confirmation, run `/audit mode=deep` instead. Explicit opt-outs on moderate/trivial and dedup-forced skips at any stakes do not trigger it (`false`). This is a compensating control for the high-stakes opt-out exception: the user can still skip, but must confirm twice.

**Dedup**: (task-id + content-hash + phase) triple key + requested-mode rank gate (rank: skip<fast<standard<deep). Within a 5-min window, a repeat emit with the same key and a requested depth ≤ the recorded depth returns `skip` (a stricter request bypasses dedup). What is recorded is the emit's **recommended** mode, not a completed audit — the caller runs `/audit` itself per ADR §5.

**User signals** (override the recommended mode; English examples shown):
- "quick scan" / "quick look" / "sanity check" / "/audit fast" -> `fast` (subject to the safety floor)
- "review it" / "cross-check" / "double-check" / "second opinion" -> `standard`
- "deep audit" / "thorough" / "strict" / "before prod" -> `deep`
- "no audit" / "skip the audit" / "i'll decide" -> `skip` (records warning to log)

A recognized user signal sets `user_override_applied=true` unless a dedup hit has
already forced the separate skip path.

The parser also recognizes bilingual equivalents in code so existing users can
keep their natural-language shortcuts. Keep those functional literals in
`aqg_phase_router.py`; the visible skill documentation stays English.

## Stakes Classification

Classify stakes before emitting the phase. The phase tells you **when** to ask;
the stakes tell the policy **how deep** the recommendation should be.

| stakes | use when |
|---|---|
| `trivial` | No escalation gate fired, and the change is a rename, reformat, comment, inert prose edit, or a change fully settled by test/type/lint. |
| `moderate` | The change is substantive, non-sensitive, and not fully settled by local checks. |
| `high` | A sensitivity or complexity gate fired: auth/permissions, crypto/secrets/tokens, trust-boundary input, data model/schema/migration, production config/deploy/CI/install integrity, irreversible work, cross-repo contracts, multi-file behavior change, first-use pattern, or major architecture decision. |

If classification is unclear, read `docs/policies/audit-trigger.md` and choose
the conservative stakes value. Do not use the phase name itself as a depth input.

## When Not To Use

Route non-phase-boundary requests to the named alternative; do not stop at saying
that this skill should not run.

| scenario | route |
|---|---|
| Session start | `aqg-startup-preflight` to check worktree, upstream, GitHub, and required context files. |
| Final handoff or completion | `aqg-evidence-closeout` to summarize verification, audit adjudication, durable state, and blockers. |
| User explicitly requests an audit of a concrete artifact | `audit` directly when no phase-boundary decision is needed. |
| An ordinary file was just edited | Do not run a phase transition; phase boundaries are timing checkpoints, not per-edit reminders. |

## Workflow

1. **Identify phase** — which of plan / implementation / tests just completed?
2. **Estimate stakes** — trivial / moderate / high (based on the policy gates)
3. **Emit phase** — `aqg-cli phase emit --phase <p> --stakes <s> --task <t> --artifact-file <f>`
4. **Read verdict** — look at `recommended_audit_mode`, `reason`, `dedup_hit`, and `dedup_reason`
5. **Act**: invoke `/audit` at the recommended mode (e.g. `mode="standard"` or `mode="deep"`); or skip if dedup hit / the user said not to audit

## Boundaries

- **Does NOT call the audit tool** — emits signal only (per ADR `2026-05-08-aqg-skill-distribution-boundary-a1.md` §5)
- **Does NOT enforce phase order** — skipping IMPL_DONE → TESTS_WRITTEN emits a warning but allows it; this is fail-loud, not fail-stop
- **Does NOT auto-pick stakes** — caller must classify (server-side hint heuristic available but not authoritative)
- **State persists locally** — `.aqg/phase-state-<safe-task>-<short-hash>.json` under the project root (one file per task; the safe task label is derived from the task ID, and the short hash of the raw task ID prevents collisions); not synced to the cloud

## References

- Depth authority: `docs/policies/audit-trigger.md` (`depth-by-stakes` marker)
- Sibling signal pattern: `aqg-security-review`'s `needs_llm_judgement` (Sprint 10 first landing of signal-not-action mode)
