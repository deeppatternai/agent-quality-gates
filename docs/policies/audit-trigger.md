# AQG Audit Trigger Policy v0.1

> **Status**: v0.1 — **migration in progress.** This file is the *intended* single
> agent-neutral source for when to audit and how deeply. It does not yet hold that
> position exclusively: see **Known remaining copies** below.
> **Boundary**: AQG owns the policy. **AQG is NOT the invoker** — it never calls
> the audit tool. Every AQG skill and hook is signal-only; the calling agent
> (Claude Code / Codex / Cursor / a human) decides and invokes.

## Why this policy exists

The trigger rules lived in three places at once — a thin "When to use" section in
`skills/audit/SKILL.md`, the full ladder inside Codex's `AGENTS.md`, and a
hardcoded reminder string in each host's hook adapter. The copies drifted, and the
drift produced two opposite failures at the same time:

- **Codex over-fired.** Its adapter forwards hook text verbatim into model context
  via `hookSpecificOutput.additionalContext` on *every* code-file edit. The line
  read `Required for executable-code commits` — categorical, no exemption,
  repeated per file. That outranked the ladder's own "audit is the exception".
- **Claude never fired.** Its hooks emit on stderr with exit 0, a combination the
  Claude Code hook contract does not forward to the model, so no timing signal
  reached it at all.

Two pointers — `codex-config/audit-routing.md` and
`~/.claude/rules/common/audit-self-routing.md` — were referenced as the canonical
contract by `AGENTS.md`, the reminder hook, and `AUDIT_DECISION_MODEL.md` §1, but
neither file exists. **This document is what those pointers were describing.**

## Core stance

**Continue by default. An external audit is a deliberate, occasional escalation.**

When a task has an executable next step, keep executing. When the next step needs
judgment, the default is a reasoned *local* decision, then continue. A slow
cross-vendor audit on every small fork is the single biggest cause of a task
dragging on — over-firing is a real failure mode, not the safe side.

**But cheapness is not the only failure mode.** The escalation gates below exist
because "small", "safe" and "simple" are different axes, and the size axis must
not be allowed to answer a blast-radius question.

## Step 1 — The escalation gates (evaluate BOTH before the ladder)

### Gate A — sensitivity

The ladder is ordered by size and local verifiability. This gate is ordered by
blast radius, **and it wins.** If the change touches any of:

- authentication, authorization, permissions, or access control
- cryptography, secrets, credentials, or token handling
- parsing or validation at a trust boundary, or any path reachable by untrusted input
- data model, schema, or migration
- production config, deployment, CI, or install / integrity / digest machinery
- anything irreversible, or spanning repositories

<!-- gate-a-tokens: auth, permissions, crypto, secrets, trust-boundary, data model, CI/deploy, install integrity, irreversible, cross-repo -->
<!-- The abbreviations each host adapter must carry in its per-edit reminder. This
     comment is the single source for them: tests parse it and assert every token
     appears in the text each adapter ACTUALLY emits. Add a category above, add its
     abbreviation here, or the adapters fall out of step silently. -->

…then the change is **not** rung 1 and **not** skippable, *no matter how few lines
it touches*. Go straight to **`deep`**.

A two-line change to an auth check is a deep change. Size is not the question
being asked here.

### Gate B — complexity

Also evaluated before the ladder, for the same reason: first-match ordering would
otherwise let rung 2 swallow these. Route to **`deep`** when the change is a
multi-file behaviour change, introduces a pattern for the first time in this repo,
or is a major architecture decision.

Neither gate is reachable *through* the ladder — that is the point. The ladder
below only runs when both gates are silent.

## Step 2 — The ladder (stop at the first rung that matches)

### Rung 1 — Trivial and non-sensitive → **DO NOT audit**

Only reachable when neither escalation gate fired. Decide locally and continue:

- a rename, a reformat, a comment
- pure wiring already covered by existing tests or linters
- a change whose correctness a test, type-check, or lint fully settles
- code already audited that has not changed since

"Not sure which of two obvious local options" is a small judgment — pick the
conservative one and move on.

### Rung 2 — Substantive, non-sensitive → **`standard`, at most once**

Either of these is sufficient (they often both apply):

- **mid-task**: the change is substantive **and** cannot be settled locally (no
  test / type-check / lint resolves it); or
- **pre-commit**: the change is substantive and has not already been audited.

Do not audit intermediate steps. **One audit covers the whole logical change** —
if the mid-task trigger fires, that audit *is* the one allowed audit, and the
pre-commit trigger is then already satisfied.

### Rung 3 — `deep`

Reached only from Gate A or Gate B above, never by falling through rungs 1–2.
`deep` is the ceiling; there is no rung above it.

For an irreversible or production-affecting change, `deep` plus a human sign-off
is appropriate — but the sign-off is the caller's judgment, not something this
policy can enforce (see the boundary disclaimer).

### Where `fast` fits

`fast` is **not** a rung. It is the opt-in cheap pass for something the ladder
would otherwise skip — a surface/typo sweep on prose, or a sanity glance the user
explicitly asked for. Nothing in the ladder ever *routes* to `fast`.

## Frequency guards

These bound the total number of audits per task, independent of the ladder:

1. Never re-audit code that has not changed since its last audit.
2. **At most ONE audit per logical change** — never audit intermediate steps.
   This holds even if a per-edit reminder fires repeatedly; the reminder is a
   timing signal, not a counter.
3. If a test / type-check / lint can answer the question, run **that** instead.
4. When context is tight, prefer `standard` over `deep`.

**Guards 3 and 4 do not apply when either escalation gate fired.** A gate-routed
change is `deep`, or it waits — a local test settling one aspect does not settle
blast radius, and context pressure is a reason to narrow the artifact, never a
reason to downgrade the depth.

## Default skip conditions

Skip unless an escalation gate fired, or the user explicitly overrides:

| condition | rationale |
|---|---|
| Trivial change (rename, reformat, comment) | rung 1 — decide locally and continue |
| Pure doc or prose change | no executable surface |
| Same artifact re-audited within 5 turns, context unchanged | refer back to the prior `audit_id`; `/audit force` overrides |
| Whole repo / 10K+ lines | audit is single-context-window review, not RAG; pre-flight rejects with size advice. If such a change also trips a gate, this is **not** a licence to skip — split it and audit the sensitive portion |
| The user said not to audit | explicit opt-out |

**Config is deliberately absent from this table.** "Config" spans both inert
formatting preferences and some of the highest-blast-radius edits in a repo —
permissions, CI, deployment, install integrity. Route config by Gate A, not by
file type.

For high-stakes work, restate the risk once before honouring an opt-out. **The
user's answer is final** — this policy has no authority to refuse a skip.

## Depth by stakes — the one mapping every caller uses

<!-- depth-by-stakes: trivial=skip, moderate=standard, high=deep -->
<!-- The single source for depth selection. `aqg-phase-transition` decides WHEN to
     ask (phase boundaries) but reads its depth from here; a test asserts its
     router agrees with this line. Phase does not affect depth: a trivial change
     is trivial at PLAN_DONE and at IMPL_DONE alike. -->

| stakes | depth | why |
|---|---|---|
| **trivial** — rung 1, no gate fired | `skip` | rung 1 says do not audit; a phase boundary is not a reason to override it |
| **moderate** — rung 2, substantive, non-sensitive | `standard` | at most once per logical change |
| **high** — either escalation gate fired | `deep` | safety floor: high never drops below `deep` |

Phase boundaries (`PLAN_DONE` / `IMPL_DONE` / `TESTS_WRITTEN`) are **timing**
signals, not depth inputs. They answer "should I be asking the question now?",
never "how deep".

## Depth names

`fast` · `standard` (default) · `deep`, plus `skip`.

Never route by a count of auditors. The voice roster (which models sit at which
depth) is owned by the Decision Engine hub and is deliberately **not** enumerated
here — it changes across versions, and the rule layer must not hard-code it.

## After results return

The audit output is an **input**, not permission to stop. Classify each finding as
`accepted` / `rejected` / `needs-user-decision` (see `aqg-audit-adjudication`),
apply accepted fixes, run the relevant checks, and continue until the task is
actually handled. `rejected` items get a one-line reason; a `needs-user-decision`
finding must name the actor and the specific decision.

## Known remaining copies (migration debt)

This file is not yet the only copy. Until these are collapsed, treat drift between
them as expected and reconcile against **this** file.

This table has been wrong twice — it kept claiming the Decision Engine still held
the full ladder for days after that repo reduced it to a pointer. A debt table
that lies is worse than no table, because it is read as an inventory of what is
left to do. So the rows a test can judge are now **judged by a test**, named in
the row; see `tests/behavior/test_migration_debt_table_is_honest.py`. Rows without
a citation are claims no test currently checks — treat them accordingly.

| location | what it still holds | status |
|---|---|---|
| ~~`integrations/codex/AGENTS.md` (Decision Engine)~~ | the full original ladder, installed into `~/.codex/AGENTS.md` | **reduced to a pointer 2026-08-12** (Decision Engine `d445315`, merged to that repo's `origin/main`). Held there by `test_decision_engine_ladder_stays_reduced_to_a_pointer`. **What that guard does not cover**, stated so the row is not over-trusted: it reads the *local* `origin/main` ref without fetching, and it **skips entirely** on a host with no Decision Engine checkout (most of them, including CI) — set `AQG_DECISION_ENGINE_REPO` to force it, and it then fails rather than skips if that path is unusable. |
| ~~`scripts/cursor_aqg_hook.py`~~ | its own separately-hardcoded reminder wording | **aligned 2026-08-11** (commit 8d7a593); still a hand-maintained second copy. Held in step on the CATEGORIES by `test_audit_gate_sensitivity_list_does_not_drift_between_adapters`, and — since the reminder clauses were published — on the WORDING by `test_every_carrier_emits_the_policys_own_reminder_clauses`. Both compare this adapter's emitted text to THIS FILE; neither compares one adapter to the other, despite what the older test's name suggests. The 2026-08-11 alignment never held: that commit ITSELF wrote `size; the` while the hook already said `size. The` — its own message describes adding "clause separators so EXCEPT-outranks-SKIP does not read as one run-on blob", and the divergence shipped inside the change that declared the alignment. Only the category check was watching. That same message records an auditor proposing a shared canonical file and its rejection, on the grounds it would reopen a twice-audited hook and add a runtime file-read failure mode to both adapters; the markers here are read at TEST time only, so the adapters still hardcode their strings and neither objection applies |
| `agent-packs/claude-code/hooks/posttooluse_code_construction_reminder.sh` | 4 bullets: a condensed skip clause, the full sensitivity list in one sentence, the once-per-change guard, and a pointer to this file | **not debt — deliberate, and it stays.** The exemption must travel *with* the timing signal, or a per-edit reminder outranks a static file. The wording is condensed and explicitly marked non-authoritative in the hook text; the ladder is not restated. Neither the categories nor the wording can drift from this file: the text this hook **actually emits** is asserted (word for word, whitespace-insensitive) against the `gate-a-tokens` marker by `test_audit_gate_sensitivity_list_does_not_drift_between_adapters`, and against the `skip-clause` / `gate-a-clause` / `frequency-clause` markers by `test_every_carrier_emits_the_policys_own_reminder_clauses`. **What those guards do not cover**, stated so the row is not over-trusted: the bullet layout, the file-path preamble and the resolved pointer line are per-host transport and are compared by neither. |
| ~~references across `docs/`, `skills/`, `examples/`~~ | the non-existent `audit-self-routing.md` pointer | **swept 2026-08-11**; the design claim behind them (main-path vs fallback) was resolved by Owner ruling, not renamed. Held swept by `test_audit_self_routing_pointer_stays_swept`. |
| `skills/*/scripts/*.py` docstrings and comments, `docs/AUDIT_DECISION_MODEL.md` (+ zh-CN) | the retired `de_audit` tool name, in prose that EXPLAINS the boundary ("this script does NOT call it") rather than instructing anyone to call it | **partially swept 2026-08-12** (Owner scope ruling: sweep what is executed or installed verbatim). Two guards, split by what each can actually see: shipped prose — SKILL.md, sidecars, the `examples/` rules templates and both READMEs — by `test_no_shipped_skill_doc_names_the_retired_tool`; everything the scripts EMIT — prints, `--help`, ledger skeletons — by `test_no_emitted_runtime_string_names_the_retired_tool`, which parses string literals and deliberately exempts docstrings and comments. What remains is explanatory prose in ~13 files, unguarded by design. An earlier version of this row credited the doc guard with covering the emitted strings; it does not, and an audit caught the overclaim. |

## The reminder text every host adapter emits

These three sentences are the reminder **itself**, not a description of one. A
host adapter must emit them word for word; `test_every_carrier_emits_the_policys_own_
reminder_clauses` compares what each adapter ACTUALLY emits against the list
below. Everything around them stays per host — bullets, pipes, line wrapping, the
edited-file preamble, the resolved pointer path — per *What agent packs may and
may not carry*.

They live in the page, not in an HTML comment, because text an adapter is
required to reproduce is not a note to maintainers: a reader of the rendered
policy has to be able to see what their tools are telling them. The comment
markers around the list delimit it for the parser and carry no content, so
deleting one is a loud failure rather than a silent narrowing.

<!-- reminder-clauses: begin -->
- **skip-clause** — SKIP if trivial / mechanical / a test·type·lint settles it / already audited — audit is the exception, not the reflex.
- **gate-a-clause** — EXCEPT auth, permissions, crypto, secrets, trust-boundary input, data model, CI/deploy, install integrity, irreversible, cross-repo: deep regardless of size. The sensitivity list at the policy path is authoritative over this abbreviation and outranks SKIP.
- **frequency-clause** — Otherwise /audit ONCE before committing (at most one per change).
<!-- reminder-clauses: end -->

The `gate-a-clause` categories are the same list as the `gate-a-tokens` marker in
Gate A, and `test_the_two_gate_a_markers_agree_in_both_directions` holds them
equal in count, order and naming — that marker stays the source for WHICH
categories exist, this list for how the sentence reads.

## What agent packs may and may not carry

| layer | agent-neutral | agent-specific |
|---|---|---|
| The ladder, sensitivity gate, guards, skip conditions, depth names | **this file** | — |
| Reminder wording at hook time | **this file** — the `skip-clause` / `gate-a-clause` / `frequency-clause` markers in Gate A carry the three sentences verbatim, and `test_every_carrier_emits_the_policys_own_reminder_clauses` compares them to what each adapter ACTUALLY emits. This row read "manual today — no automated derivation exists" until that list was added. Stated as what is checkable rather than as history: each adapter's emitted wording is compared to this file, not to the other adapter. Word-for-word, whitespace-insensitive — the comparison collapses runs of whitespace so a host may wrap where its format needs | — |
| Transport (how text reaches the model) | — | per host: `additionalContext` JSON, `exit 2`, `permission: deny`, … |
| Which lifecycle events exist | — | per host capability |

Transport differences between hosts are legitimate and should be preserved — a
host that can block a commit should keep that ability. **Policy differences are
not.** Per `docs/AGENT_COMPATIBILITY_STRATEGY.md`, agent packs "should not fork
gate semantics".

## Boundary disclaimer (re-emphasized)

- AQG does not invoke the audit tool, does not hold enforcement authority, and
  does not monitor tool calls. Hooks emit text; the agent decides.
- Nothing here is a blocking gate. No check anywhere fails because an audit did
  not happen. Adding one is a separate decision with its own offline-degradation
  and explicit-exemption requirements.

## Refs

- `docs/AUDIT_DECISION_MODEL.md` — orchestration (who decides depth, where the
  chokepoint is, self-review vs external panel)
- `docs/AGENT_COMPATIBILITY_STRATEGY.md` — agent-neutral core / thin adapter principle
- `skills/audit/SKILL.md` (Decision Engine) — calling pattern and "NOT to use" list
- `docs/policies/dangerous-command-guard.md` — sibling policy, same shape
