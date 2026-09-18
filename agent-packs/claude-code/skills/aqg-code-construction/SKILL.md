---
name: aqg-code-construction
description: "Run this before writing, modifying, refactoring, or implementing code: it is the code construction entry point, not a post-hoc review. Guides the 6-step workflow (Pattern Mining, Behavior Lock, Thin Slice, Construction Rules, Local Verification, Self Review), maintains the evidence ledger, predicts reviewer objections, and runs deterministic anti-pattern checks plus the audit-before-commit gate."
---

# AQG Code Construction

Use this skill before and during code edits in any repo where reactive audit failures are costly. It makes construction visible while the work is happening: read the local pattern, lock behavior where applicable, keep the slice small, verify locally, self-review, and leave a structured ledger.

## Quick Contract

- **When**: before writing, modifying, refactoring, or implementing code.
- **What it produces**: a `.aqg/code-construction/<task>.md` evidence ledger plus a pre-commit checker result.
- **What it enforces**: deterministic structure only — ledger presence, tier sizing, warning acknowledgements, objection shape, selected hard blockers, and secret/broad-except guards.
- **What it does not prove**: semantic correctness, test quality, or product fitness. Those remain the agent's job plus the audit-before-commit gate when policy routes to audit.
- **Language hygiene**: keep the ledger in one working language unless quoting code, command output, or existing identifiers.

## Setup (one-time per repo)

> One-time **manual provisioning** per repo. Per GUIDE §6.1 these are outside
> the `entry_script` (`aqg_construction_check.py`) boundary: the checker does
> not write project files. The detached AQG updater is covered in Boundary Rules.

```bash
# Install pre-commit hook (managed via core.hooksPath, opt-in)
python3 "${AQG_ROOT:?AQG_ROOT required}/scripts/install_aqg_construction_hook.py" --target-repo .

# Create workspace + gitignore
mkdir -p .aqg/code-construction
grep -qxF '.aqg/' .gitignore 2>/dev/null || echo '.aqg/' >> .gitignore
```

**Critical**: the pre-commit hook only enforces when `AQG_AGENT` is set to a known value (`codex`, `claude`, `human-opt-in`, `ci-rerun`). Without it the hook passes without enforcing (transparent to humans; the checker prints an inactive notice to stderr unless `--quiet`):

```bash
AQG_AGENT=codex git commit -m "..."
```

## Workflow

### 1. Session start — declare path

Create the ledger from template and choose a path tier by expected diff size:

```bash
slug="<short-task-slug>"
ledger=".aqg/code-construction/${slug}.md"
cp "${AQG_ROOT}/templates/code-construction-ledger.md" "$ledger"
ln -sf "code-construction/${slug}.md" .aqg/current_ledger.md
# Edit header: task_slug, path (mini|full|plan), created_at (now ISO 8601 UTC), session_agent
```

| path | size | required steps |
|---|---|---|
| `mini` | ≤ 30 lines | 1 + 5 + 6 |
| `full` | 31-300 lines | 1-6 (all) |
| `plan` | > 300 lines / cross-module | 1-6 + PRP-style plan |

Tier is process guidance, not the full gate: a **production-path** change (see prod-path scope below) requires **row-2 Behavior Lock evidence at ANY tier** — the row-2 hard blocker ignores the declared path.

### 2. 6-step construction

1. **Pattern Mining** — read neighbor code, tests, helpers, error style. Fill ledger row 1 with `file:line` evidence.
2. **Behavior Lock** (full/plan) — write/extend a focused test FIRST per the TDD cycle (RED → GREEN → REFACTOR). Fill ledger row 2 with the test path + RED-fail message + GREEN-pass result. For a production-path change, also fill the **`## Behavior Contract`** section (see below) with the requirement/scenario being locked, and cite it in row-2 evidence via a `covers: R1, R2` token. The full RED/GREEN/REFACTOR detail, the vertical-slice (anti-horizontal) rule, and the change-type scope table live in `docs/TESTING_METHODOLOGY.md` (repo root); the per-change-type summary is in **TDD scope** below.
3. **Thin Slice** (full/plan) — one behavior face per task; resist refactor.
   Decide the slice's **done condition** up front — the single check that flips
   it from in-progress to complete — so "done" is a pre-agreed observation, not a vibe.
4. **Construction Rules** (full/plan) — fail-closed + the **YAGNI ladder** (stop at the first rung that holds): ① need it at all? speculative → skip, say so in one line · ② stdlib / platform-native does it? → use it · ③ a helper already here, or an installed dep, does it? → use it, never add a dep for a few lines · ④ one line? → one line · ⑤ only then, the minimum that works. **Lazy ≠ careless** — never simplify away trust-boundary validation, data-loss error handling, security, or accessibility. An explanation longer than the code it defends is over-build as prose; cut it.
5. **Local Verification** — focused test + lint/type/compile/diff. Fill row 5 with command + summarized result.
6. **Self Review (5 axes)** — correctness / readability / architecture / security / performance. Fill row 6.

### 3. Predicted Objections section

Each objection MUST: include a `file:line` reference (file MUST exist — checker validates `pathlib.exists()`); have a concrete mitigation (command / test / check / specific handling); NOT be vague (rejected: "add tests", "do review", "watch boundary", "improve later"). At least 1 objection MUST reference a changed file in the diff (else set `objections_diff_coverage_exception` in the header).

| path | min objections |
|---|---|
| `mini` | 1 |
| `full` | 3 |
| `plan` | 5 |

### 4. Warning Acknowledgements section

For each anti-pattern warning the checker emits (new TODO/FIXME = W5, new dep entry = W6, large diff = W7, docs change = W8), add a row to `## Warning Acknowledgements`:

```markdown
| condition | ack | reason |
|---|---|---|
| W6: new dep tenacity | yes | retry needed; alternatives write ad-hoc not safe |
```

Empty / vague reason fails the checker (silent skip not allowed).

### 4b. Behavior Contract (warn-only in current 0.14.x)

For full/plan production-code changes, make the row-2 Behavior Lock into a structured,
referenceable contract: RFC-2119 requirement (`MUST`/`SHALL`) plus observable
`GIVEN`/`WHEN`/`THEN` scenarios. Tests cite it with `covers: R1, R2`; the audit
gate and closeout reuse it as acceptance criteria.

```markdown
## Behavior Contract

### R1: OTP challenge on valid credentials
The system MUST present an OTP challenge when a 2FA user submits valid credentials.

- Scenario S1.1: valid creds with 2FA
  - GIVEN a user with 2FA enabled
  - WHEN the user submits valid credentials
  - THEN an OTP challenge is presented
```

The checker emits **advisories only** in current 0.14.x (`WARNING: BC<n>: …`; exit
code unchanged; later minor promotes to exit 7):
- **BC1**: every `### R<n>` statement line has a normative keyword (`MUST`/`MUST NOT`/`SHALL`/`SHALL NOT`, upper-case).
- **BC2**: every requirement has ≥1 scenario, and every scenario has all of `GIVEN`/`WHEN`/`THEN` (case-insensitive).
- **BC3**: row-2 evidence's `covers: R…` list cites every declared requirement (coverage) and no undeclared one (dangling).
- **BC4**: no duplicate requirement id.
- **BC0**: any parse error is surfaced as an advisory (the parser is total — never crashes the checker).

Semantic quality is NOT checked: `THEN an OTP challenge is presented` is observable;
`THEN OtpService.challenge() is called` is implementation detail. Opt out with
`behavior_contract_exception: <concrete reason>`; it suppresses BC advisories only,
never the row-2 Behavior Lock hard block on prod changes.

### 5. Audit-before-commit gate (the chokepoint — not a mandate)

Cross-LLM verification happens HERE if it happens at all — at the gate, not at every TDD step. Whether it happens is decided by `docs/policies/audit-trigger.md`, not by this section: **audit is the exception, not the reflex**, and a trivial non-sensitive change is not audited. When the policy does route to an audit, run it BEFORE `git commit`:

```bash
# LLM agent or human invokes the /audit skill; depth per docs/policies/audit-trigger.md.
/audit mode=fast|standard|deep artifact="<staged diff>" context="<intent + acceptance criteria>"
```

- Depth selection per `docs/policies/audit-trigger.md`: the escalation gates (sensitivity, complexity) are evaluated first and route to `deep`; otherwise trivial → skip, substantive → `standard` at most once. `aqg-phase-transition` decides *when* to ask, never how deep.
- If findings exist → invoke `aqg-audit-adjudication` for the accept/reject table. ACCEPT → integrate fixes. REJECT → record reason in commit body or the ledger. NEEDS_USER_DECISION → escalate per `docs/policies/audit-trigger.md`.
- Cite the `audit_id` in the commit body to lock the chain. Doc/spec-only commits skip this gate. **Config does not get a file-type exemption** — the policy routes it by blast radius (Gate A lists production config, deployment, CI and install/integrity machinery as `deep` regardless of size).

### 6. Run construction checker before commit

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
AQG_AGENT=codex python3 "$aqg_root/skills/aqg-code-construction/scripts/aqg_construction_check.py" --mode staged
```

Exit codes: `0` PASS · `1` generic / fail-closed (e.g. a git-diff error) · `2` secret pattern in ledger (or argparse bad args — see stderr) · `3` ledger missing / malformed / resolved outside `.aqg/code-construction/` · `4` size mismatch (declared path vs diff) · `5` anti-pattern hard block · `6` objection malformed.

### 7. Commit with env var

```bash
AQG_AGENT=codex git commit -m "..."
```

The pre-commit hook re-runs the checker; passes only on exit 0.

## TDD scope — when the full RED→GREEN cycle applies

Not every change needs a fresh failing test (per Owner honest caveat):

| Change type | TDD cycle | Test action |
|---|---|---|
| **New behavior** (new function / endpoint / class) | Full RED → GREEN → REFACTOR | write the failing test first |
| **Bug fix** | Modified RED → GREEN | write a regression test that reproduces the bug FIRST (it FAILs), then fix (it PASSes) |
| **Pure refactor** | no new test | existing tests are the regression net and must stay GREEN; add coverage first if thin |
| **Pure doc / spec / config / .md** | N/A | no executable code; skip step 2 |
| **Migration / DDL / API contract** | verify via contract test | run migration / contract test in step 5 |

Test code is code — review it on the same 5 axes (assert the contract not an impl detail; cover edge cases; synthetic fixtures only; fast; isolated).

Record the exception, don't hide it:

- Pure docs/spec/config edits: row 2 may say `N/A — no executable behavior`; row 5 still names the validation actually run.
- Pure refactors: row 2 cites the existing regression net that stayed green; add coverage first if that net is thin.
- Unable to run a required local check: put the skipped check in `skipped_checks` as `{"check": "...", "reason": "..."}` with a concrete reason; vague reasons hard-block.
- Behavior Contract not applicable: set `behavior_contract_exception` only for a concrete non-prod/non-behavior reason. It suppresses BC advisories, not the row-2 prod-path hard block.

## What the checker actually enforces (deterministic, not semantic)

The checker validates field PRESENCE and structure, never code-quality semantics. Hard blockers (exit code in parens):

- **Secret-pattern scan** of the ledger — AWS, GitHub, Stripe, OpenAI, Slack, JWT, PEM (2).
- **Ledger** present + header well-formed + declared-path/size match + resolved path under `.aqg/code-construction/` + `created_at` not backfilled past the earliest edited file (3 / 4).
- **Objections** (count + `file:line` that must exist + non-vague mitigation per tier) and **warning acknowledgements** present (6 / 5).
- **6-step table** parses (5 columns) when a row-based blocker applies; **Behavior-Lock evidence (row 2)** when prod code changes (any tier); **Local-Verification regression keyword (row 5)** when a schema/contract path changes (5).
- **New broad `except Exception/BaseException`** handlers need the `# aqg: top-level boundary` marker within 5 lines; **`skipped_checks`** entries must be `{check, reason}` dicts with a concrete reason (5).
- **Behavior Contract (BC0–BC4)** — **advisory / warn-only in current 0.14.x** (stderr `WARNING: BC<n>: …`, exit code unchanged). Structural only: normative keyword per requirement, GIVEN/WHEN/THEN per scenario, `covers:` coverage/dangling, duplicate id, parse-error surfacing. See §4b. A later minor promotes these to a hard block under a new exit code 7.

It does NOT verify presence of steps 1/3/4/6 or require row 5 for ordinary code, and does NOT (yet) block on Behavior Contract issues — those are process guidance / advisory, not enforced.

**Prod-path scope** (row-2 blocker): `scripts/*.py`, `src/`, `skills/*/scripts/*.py`, `agent-packs/*/hooks/*.sh`. **Schema scope** (row-5 blocker): `*.proto`, `templates/*.yaml`, `schemas/`, `*.openapi.json`, `quality-gates.json`; migration/DDL/SQL contract-testing is process guidance unless the file matches a schema pattern. Broader executable-extension classification (`app/`, `lib/`, `server/`, `cmd/`, …) is a tracked follow-up, not yet enforced.

## Boundary Rules

- The Python CLI nudges AQG's signed updater in a detached background process on managed installs. It may update AQG's own installation/state, never the target project's source. Set `AQG_NO_UPDATE_CHECK=1` for strictly update-free invocation. Merely reading this skill does not trigger it.
- Enforces STRUCTURE only (deterministic field presence); code-quality semantics stay in the audit-before-commit gate / audit-mcp.
- Performance timeouts (3s/15s/60s for mini/full/plan) are warn-only.
- The sidecar conservatively declares `writes-code` for replacement of AQG's installed code and refresh of AQG-owned routes/hooks by the detached updater; checking the target project still does not edit its source.
- Pre-commit enforcement is gated by `AQG_AGENT`; humans not setting it are transparent. Enforcement is commit-time output discipline, not real-time process; `created_at <= first edit mtime` partially mitigates backfill. The Claude Code path additionally registers a PostToolUse hook for real-time feedback.
- Closeout integration: when `aqg-evidence-closeout` is installed, it auto-imports `.aqg/current_ledger.md` (read-only; secrets redacted) — it never edits the ledger or referenced files.
- TDD scope boundary: step 2 owns RED/GREEN/REFACTOR + test-code quality. It does NOT own per-task dispatch / parallelism / worktree isolation, Owner-only boundary enforcement, signed-envelope audit trail, or test sandboxing (those are EAF skills).

## Reporting Shape

```markdown
Code Construction Check (path={mini|full|plan}):
- ledger: <path>
- diff: <N files, M lines>
- 6-step rows present: <count>/<expected>
- objections: <count>/<min required>
- anti-patterns: <warn count> warn, <hard count> hard
- behavior contract: <BC advisory count> advisory (warn-only)
- decision: <PASS / BLOCK + exit code + reason>
- next safe step: <one concrete action>
```
