---
name: aqg-phase-transition
description: Use PROACTIVELY at three points — after producing a plan/PRD/design doc, after finishing a non-trivial implementation, after writing tests — to emit a phase-transition signal (PLAN_DONE / IMPL_DONE / TESTS_WRITTEN) and get a recommended audit mode before moving on. The depth rule lives in `docs/policies/audit-trigger.md`, which this skill applies — a sensitive-surface change is audited at the strictest depth regardless of size, and a trivial non-sensitive one is not audited at all. Signal only — the caller runs /audit.
---

# AQG Phase-Transition Audit Trigger

Use this skill at the **three Anthropic-spec phase boundaries** to decide whether (and how deeply) to audit before moving to the next phase. The skill is a **router** — it selects an audit depth from the matrix; the caller invokes audit-mcp `/audit` separately.

## Three-layer position

| layer | tool | scope |
|---|---|---|
| pre-action gate | `aqg-startup-preflight` | worktree / context / live state |
| **phase-transition fallback** | **aqg-phase-transition (this)** | **PLAN→IMPL→TESTS→RUN boundaries** |
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
python3 "$script" override --task "sprint-11a-aqg-automation-audit" --user-signal "快速扫一下"
```

Or via cloud (thin-client):

```bash
aqg-cli phase emit --phase plan_done --stakes moderate --task "..." --artifact-file plan.md
aqg-cli phase query --task "..."
aqg-cli phase override --task "..." --user-signal "快速扫一下"
```

The verdict prints `recommended_audit_mode` (`fast` / `standard` / `deep` / `skip`). The **caller** (you / Claude session / EAF) is responsible for invoking `/audit` at that mode. AQG itself does NOT call audit-mcp (per ADR §5 boundary).

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

**Safety floor**: high stakes is not allowed to drop below `deep` (even if the user says "快速扫"; the only exception is an explicit "别审" = skip opt-out). The high column is filled directly with `deep`, not silently raised by the floor.

**High-stakes skip second confirmation** (issue #282): an explicit "别审" = skip opt-out on high stakes is still honored (mode=`skip`, ADR §4 invariant 4), but the verdict additionally emits `high_stakes_skip_confirm=true` — the caller **must first obtain a second explicit confirmation from the user** before honoring this skip; without that re-confirmation, run `/audit mode=deep` instead. The opt-out skip and dedup skip on moderate/trivial do not trigger it (`false`). This is a compensating control for the "opt-out exception": the user can still skip, but must confirm twice.

**Dedup**: (task-id + content-hash + phase) triple key + requested-mode rank gate (rank: skip<fast<standard<deep). Within a 5-min window, a repeat emit with the same key and a requested depth ≤ the recorded depth returns `skip` (a stricter request bypasses dedup). What is recorded is the emit's **recommended** mode, not a completed audit — the caller runs `/audit` itself per ADR §5.

**User signals** (override matrix selection — English AND Chinese are recognized):
- "quick scan" / "quick look" / "sanity check" / "/audit fast" · "快速扫" / "随便看看" → `fast` (subject to the safety floor)
- "review it" / "cross-check" / "double-check" / "second opinion" · "看看" / "审一下" / "交叉验证" → `standard` (dual-mainstream 2 models)
- "deep audit" / "thorough" / "strict" / "before prod" · "严格审" / "深审" / "挖深" / "上 prod 前再过一遍" → `deep`
- "no audit" / "skip the audit" / "i'll decide" · "别审" / "我自己拍板" → `skip` (records warning to log)

## Workflow

1. **Identify phase** — which of plan / impl / tests just completed?
2. **Estimate stakes** — trivial / moderate / high (based on change surface, prod contact, irreversibility)
3. **Emit phase** — `aqg-cli phase emit --phase <p> --stakes <s> --task <t> --artifact-file <f>`
4. **Read verdict** — look at `recommended_audit_mode` + reason + dedup_status
5. **Act**: invoke `/audit` at the recommended mode (e.g. `mode="standard"` or `mode="deep"`); or skip if dedup hit / the user said not to audit

## Boundaries

- **Does NOT call audit-mcp** — emits signal only (per ADR `2026-05-08-aqg-skill-distribution-boundary-a1.md` §5)
- **Does NOT enforce phase order** — skipping IMPL_DONE → TESTS_WRITTEN emits a warning but allows it; this is fail-loud, not fail-stop
- **Does NOT auto-pick stakes** — caller must classify (server-side hint heuristic available but not authoritative)
- **State persists locally** — `.aqg/phase-state-<task>.json` (per-task file; the name carries a short hash of the raw task-id to avoid collisions) under the project root; not synced to the cloud

## References

- Depth authority: `docs/policies/audit-trigger.md` (`depth-by-stakes` marker)
- Sibling signal pattern: `aqg-security-review`'s `needs_llm_judgement` (Sprint 10 first landing of signal-not-action mode)
