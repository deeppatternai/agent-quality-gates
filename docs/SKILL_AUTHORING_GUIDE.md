# SKILL Authoring Guide

How to write a new AQG skill that fits the disk shape, metadata grammar,
trigger semantics, execution contract, boundary declarations, and release
plumbing of the 5 already-shipped skills (`aqg-startup-preflight`,
`aqg-code-construction`, `aqg-systematic-debugging`, `aqg-audit-adjudication`,
`aqg-evidence-closeout`).

A minimal worked example lives in [`templates/example-skill/`](../templates/example-skill/).

## 0. Charter

### 0.1 Audience

**Primary**: human engineer + reviewer writing or reviewing a new AQG skill.
A separate machine-readable template for AI-generated skills is planned as
a future deliverable; until then, AI agents should use this guide as a
checklist rather than a prompt.

### 0.2 Scope

This guide covers:

- skill disk shape (frontmatter + body + scripts/)
- description grammar that drives Claude/Codex auto-trigger
- helper script CLI contract (exit codes, stderr, output)
- boundary declarations (what skills must not do)
- behavior testing (fixture, drift, nightly cadence)
- packaging / installation / release synchronization

### 0.3 Out of scope

- the **semantic product content** of any individual skill (i.e. what your
  skill should decide / output) — that belongs in a sketch + audit thread,
  not this guide
- TDD discipline at large (already a framework-level rule)
- skill namespace strategy (`aqg-*` was unified in 0.3.0 and is fixed)
- agent-client runtime semantics (those live in Codex / Claude Code docs)

### 0.4 Skill entry criteria — when to write a skill at all

Write a new AQG skill only when **all four** are true:

1. The behavior needs to be reusable across multiple sessions / projects.
2. It benefits from description-based auto-trigger (Claude/Codex picks it up
   from the user's words rather than an explicit invocation).
3. It involves multi-step structured workflow or a structured ledger / table
   output, not a one-shot transformation.
4. It applies across the supported host agents (skills ship from one source
   tree, with a generated wrapper where a host needs one — single-client
   utilities should be regular scripts).

If any of those fail, prefer one of: a stand-alone script under `scripts/`,
a function inside an existing skill, a doc-only convention, or no artifact
at all. A bloated skill index dilutes auto-trigger signal across the whole
pack.

## 1. Skill anatomy

### 1.1 Frontmatter

YAML frontmatter contains exactly two keys:

```yaml
---
name: aqg-<skill-name>
description: <single-paragraph trigger semantics; see §2>
---
```

Adding any other key (version, tags, model, etc.) is forbidden — those
metadata channels are owned by `scripts/aqg_doctor.py` and behavior tests,
not the skill file.

### 1.2 Body shape

Body is markdown after the frontmatter. Total file (including frontmatter)
should stay under **150 lines**; the existing 5 skills run from 46 to 136
lines. Recommended sections in order:

1. one-paragraph context: what the skill does, who triggers it
2. **How To Run**: the exact bash snippet that resolves AQG_ROOT and
   invokes the helper script (see §3)
3. **Setup** (only if one-time per-repo install is required)
4. **Workflow** or **Required Table** or **Stop Boundaries**: structured
   step list / required output shape
5. **Rules** / **Boundaries**: explicit list of what the skill does not do
   (see §6)

Long-form prose, multi-paragraph rationale, and decision-tree diagrams
should live in `docs/discussion/`, not the skill body.

### 1.3 scripts/ subdirectory

Helper logic goes in `skills/<name>/scripts/`. Conventions:

- Python 3 standard library only — no third-party dependencies
- one entrypoint per concern (`<name>_check.py`, `validate_<name>.py`)
- a `self_test.py` exercising the happy path and at least one failure path
- shebang `#!/usr/bin/env python3`, executable bit set
- argparse with `--help`; `--json` flag for machine-readable output

### 1.4 Execution feedback contract

Helper scripts must signal state through exit codes that agent clients
can branch on:

| exit | meaning | when |
|---|---|---|
| 0 | success | check / validation / generation succeeded |
| 1 | expected check failure | finding produced, blocker fired, table malformed |
| 2 | usage error | bad flags, missing required arg, unknown subcommand |
| 3 | config / schema error | malformed input file, schema mismatch |
| 70 | internal error | unexpected exception, bug in helper |

`stdout` carries the result (table, JSON, skeleton). `stderr` carries
diagnostics and path-resolution errors so agents can surface them without
parsing stdout.

## 2. Description grammar

The `description` frontmatter value is the single biggest determinant of
auto-trigger accuracy. It must include four elements:

### 2.1 Lead verb

Start with an action verb that mirrors what a user would naturally say
when they want this skill. The 5 shipped skills use:

- `Verify` (preflight)
- `Run` (code-construction)
- `Root-cause` (systematic-debugging — verb-as-modifier form)
- `Decide` (audit-adjudication)
- `Close` (evidence-closeout)

### 2.2 Trigger conditions — derived from user prompts, not from fixtures

List specific surfaces and situations where this skill should fire,
using **vocabulary the user actually types** (not internal AQG jargon).

Process:
1. write 5–10 example user prompts that should trigger your skill
2. extract the recurring nouns / verbs from those prompts
3. mirror those words into the description's trigger clause
4. *then* write the behavior fixture using the same prompt set

Behavior fixtures are **regression tests** that catch drift. They are not
the optimization target — never tune the description to maximize fixture
pass rate. Doing so traps the skill in fixture-only language and degrades
real-user trigger accuracy.

This is the central lesson from the PR-5 wrong-trigger investigation
(audit-adjudication trigger pass rate went from 60 % to 100 % once the
description switched from internal jargon — "three-auditor", "convergent" — to
user vocabulary — "audit returned", "review finding", "accept or reject").

### 2.3 Boundary clause

Tell the agent when **not** to fire the skill: "not for repeated mid-session
checks", "not after audit/review returns", etc. Without this, skills with
broad lead verbs ("Use…") fire too often and dilute the pack's signal.

### 2.3b No competing-context / pushiness

A description must scope **when** to use the skill — it must NOT claim
**universal priority**. Pushy, self-promoting phrasing is "competing context":
if every skill's description shouts "always use me", the model's skill-selection
signal degrades for the *whole pack*, not just the pushy skill (Anthropic
skill-creator). The cost is paid by your neighbors, so the discipline is
pack-level, not per-skill taste.

Avoid these four families (case-insensitive):

| family | don't write | write instead |
|---|---|---|
| absolute-frequency | "**always use** this", "on every commit" | "Use **when** committing schema changes" |
| over-broad-scope | "**for all tasks**", "use it **for everything**" | "for **multi-file** code changes" |
| superlative | "the **best** skill", "**most powerful** reviewer" | (state the capability, drop the ranking) |
| exclusivity | "**the only** tool you need", "**never use** any other" | (describe the niche; let the model pick) |

Note the contrast with §2.1–2.3: a scoped lead verb plus trigger conditions
("Use PROACTIVELY **when** writing authentication", "Use **before** pushing")
is *not* pushiness — it is exactly the targeting the model needs. The line is
between *scoping the trigger* (good) and *claiming priority over other skills*
(bad).

`aqg_skill_validator.py` surfaces these as an **advisory warning** (not a hard
violation — wording is a judgment call). The shipped roster is pinned at zero
pushiness by the CI guard `tests/behavior/test_skill_description_pushiness.py`.
*Known follow-up (out of scope here):* §2's four-element description grammar is
documented but the validator only enforces non-empty `description` (+ canary
drift), not the four elements — a larger evaluation worth a separate pass.

### 2.4 Path-portable declaration

Include a clause like "in any repository" or "defaults to the current git
root and supports explicit repo paths". This signals to the agent that
the skill does not assume a specific project layout, and to reviewers
that path arguments must be explicit (no hardcoded `~/projects/foo`).

## 3. AQG_ROOT resolution

### 3.1 Current state — two variants

The 5 shipped skills resolve `AQG_ROOT` differently depending on whether
they are the source skill (used by Codex) or the Claude wrapper:

**Codex source skill** (`skills/aqg-<X>/SKILL.md`): `AQG_ROOT` env var
is **required**. Codex has no equivalent of `CLAUDE_SKILL_DIR`, so the
resolver hard-fails with a fix message if the env var is not set.

```bash
aqg_root="${AQG_ROOT:-}"
script="$aqg_root/skills/<skill-name>/scripts/<entrypoint>.py"
if [ -z "$aqg_root" ] || [ ! -f "$aqg_root/VERSION" ] || [ ! -f "$script" ]; then
  echo "ERROR: cannot resolve Agent Quality Gates root." >&2
  echo "  Fix: export AQG_ROOT=/path/to/agent-quality-gates checkout." >&2
  exit 1
fi
```

**Claude wrapper** (`agent-packs/claude-code/skills/aqg-<X>/SKILL.md`):
falls back through `CLAUDE_SKILL_DIR` so the user does not need to set
the env var when invoking via Claude Code's skill loader:

```bash
aqg_root="${AQG_ROOT:-}"
if [ -z "$aqg_root" ] && [ -n "${CLAUDE_SKILL_DIR:-}" ] && [ -d "$CLAUDE_SKILL_DIR" ]; then
  resolved="$(cd "$CLAUDE_SKILL_DIR" && pwd -P)"
  if [ -f "$resolved/.aqg-root" ]; then
    aqg_root="$(tr -d '[:space:]' < "$resolved/.aqg-root")"
  fi
  if [ -z "$aqg_root" ]; then
    aqg_root="$(cd "$resolved/../../../.." && pwd -P)"
  fi
fi
script="$aqg_root/skills/<skill-name>/scripts/<entrypoint>.py"
if [ -z "$aqg_root" ] || [ ! -f "$aqg_root/VERSION" ] || [ ! -f "$script" ]; then
  echo "ERROR: cannot resolve Agent Quality Gates root." >&2
  echo "  resolved aqg_root='$aqg_root'" >&2
  echo "  expected sentinel: \$aqg_root/VERSION + $script" >&2
  echo "  Fix: export AQG_ROOT=/path/to/agent-quality-gates checkout." >&2
  exit 1
fi
```

### 3.2 Known inversion risk

Carrying the same 30-line snippet in every skill is technical debt —
any future change to the resolution algorithm requires touching every
SKILL.md. This was acknowledged; the extraction has since **landed** as
the shared bash helper **`scripts/_aqg_context.sh`** (a *sourced* library,
not an executable — it `return`s, never `exit`s, and leaves the caller's
shell options intact). SKILL.md How-To-Run blocks now `source` it to
resolve `aqg_root` instead of carrying the inline snippet above.

Until the extraction lands, **do not invent a different resolver** —
matching the existing pattern keeps the eventual mass-rewrite trivial.

## 4. Packaging, installation, release checklist

A new skill `aqg-<X>` requires touching exactly these **11 files /
locations**, validated against PR #46 (rename, full sweep) and PR #47
(install.sh fix-up). Order doesn't matter; verify all 11 before opening
a PR.

| # | path | what to add |
|---|---|---|
| 1 | `skills/aqg-<X>/SKILL.md` + `scripts/` | the source skill (frontmatter + body + helpers) |
| 2 | `agent-packs/claude-code/skills/aqg-<X>/SKILL.md` | the Claude wrapper (description adds "in Claude Code", body trimmed ~30 %) |
| 3 | `scripts/install.sh` `skills` array | add `"aqg-<X>"` |
| 4 | `agent-packs/claude-code/install.sh` `skills` array | add `"aqg-<X>"` |
| 5 | `scripts/aqg_doctor.py` `CLAUDE_SKILL_NAMES` tuple | add `"aqg-<X>"` |
| 6 | `scripts/aqg_doctor.py` `CODEX_SKILL_NAMES` tuple | add `"aqg-<X>"` |
| 7 | `tests/behavior/fixtures/triggers.yaml` | add ≥5 cases (description-based + explicit invocation mix, ~70/30 — matches §7.1 minimum) |
| 8 | `skills/aqg-<X>/skill.template.json` | set `"wrapper_generated": true`, then `python3 scripts/aqg_skill_gen.py regen aqg-<X>` to emit the wrapper. The committed wrapper is guarded by the CI regen→git-status gate (this replaced the retired drift-hash baseline, 2026-05-31 §6/§7). **Validator-enforced**: an unmanaged skill fails cross-cutting registration. |
| 9 | `skills/aqg-<X>/agents/openai.yaml` | `interface.display_name: "AQG <Name>"` — without it the Codex picker falls back to the slug and renders `Aqg <Name>`. **Validator-enforced** (`_validate_codex_interface`); 4 skills shipped without it before the gate existed. |
| 10 | `tests/behavior/test_aqg_skill_trigger_canary.py` `SKILL_TRIGGER_KEYWORDS` | add ≥1 trigger keyword that is actually present in the description. **Missing entry fails `test_keyword_map_covers_skill_roster`** (self-computing 1:1 roster check). On a *description* change, update the keywords here too (see #152). |
| 11 | `tests/behavior/test_aqg_security_boundary_canary.py` | add `aqg-<X>` to **either** `SECURITY_BOUNDARY_SIGNATURES` (pin its body boundary-promise phrase, e.g. "remain separate authorization gates") **or** `KNOWN_NO_SECURITY_BOUNDARY` (with an exemption reason). **Missing entry fails `test_roster_is_partitioned`** (self-computing roster partition). |

> **The CI fixture-count gate is NOT a touchpoint** (as of PR #125→ADR
> `2026-05-26-self-computing-fixture-gate-a1.md`). `.github/workflows/behavior-tests.yml`
> runs `scripts/check_fixture_mix.py`, which **self-computes** invariants from the
> `skills/aqg-*/` roster — adding a skill (≥5 cases, registered dir) passes with no
> edit. The one exception is skill **removal**: it leaves a declared skill in the
> version-controlled `skills.list` manifest with no matching dir, which the I6
> guard rejects (WS-3), so a removal must also delete that skill's `skills.list`
> line — which is correct, since removal is an Owner-approved major change (§4.4). History (was a hidden extra touchpoint that bit
> PR #124).

> **Where CI-gated tests live.** `behavior-tests.yml` is the only workflow that runs
> pytest, and it collects `tests/behavior/` (+ `skills/*/tests/`) only; its `paths:`
> trigger also fires on `agent-packs/**/SKILL.md` and `skills/**`. A test placed in
> the top-level `tests/` runs locally (`scripts/smoke_test.sh` layer 4) but is **not**
> collected by CI, so it cannot gate a PR. Put PR-gating tests in `tests/behavior/`.

### 4.1 Source vs Claude wrapper

The Claude wrapper differs from the source in two predictable ways:

- description appends "in Claude Code" or substitutes a Claude-specific
  trigger phrase (so Claude's pack doesn't fire when Codex is the
  active agent)
- body trims agent-specific instructions (~30 % shorter) and tightens
  the How To Run snippet to use Claude's `CLAUDE_SKILL_DIR`

The two SKILL.md files share the same `scripts/` (only the skill
directory is duplicated; helpers live once under `skills/aqg-<X>/scripts/`
and the wrapper delegates back via the resolved `AQG_ROOT`).

### 4.2 Regenerating the Claude wrapper

The Claude wrapper is a generated verbatim copy of the source SKILL.md (with
sidecar anchor swaps) — never hand-edited. After any source SKILL.md or sidecar
change, regenerate it:

```bash
python3 scripts/aqg_skill_gen.py regen aqg-<X>
```

The committed wrapper is guarded by the CI `regen --check --all` + git-status
gate: a stale wrapper turns CI red. This replaced the retired per-skill
drift-hash baseline (spec 2026-05-31 §6/§7) once all skills migrated to wrapper
generation — there is no longer a hash to recompute on a description/body edit.

### 4.3 Verification

- `python3 scripts/aqg_doctor.py --no-cli` → `PASS=38 + 2N / WARN=0 / FAIL=0`
  on a clean reinstall (where N = number of new skills; baseline grew
  from 37 to 38 in PR #54 when `_aqg_context.sh` joined CRITICAL_SCRIPTS)
- `python3 -m pytest tests/behavior/` → all green
- `bash scripts/install.sh --list` → contains `aqg-<X>`
- `bash agent-packs/claude-code/install.sh --list` → contains `aqg-<X>`
- `python3 scripts/check_fixture_mix.py` → `OK: <N> skills, fixture invariants hold`.
  The fixture gate self-computes from the roster (no count to bump); this is the
  same check CI runs, so a local pass means a green check. Its negative-test
  battery lives in `tests/test_check_fixture_mix.py`.

### 4.4 Version bump

Adding a skill is a minor bump (e.g. 0.3.0 → 0.4.0 if the new skill is
the headline change; otherwise wait until the next planned minor and
batch). Renames or removals are major changes that require explicit
Owner approval — the 0.3.0 ai-team-* → aqg-* rename was a breaking
change accepted only because AQG had no users besides the Owner at
that point.

## 5. Path portability

- default to the current git root (resolved via `git rev-parse --show-toplevel`)
- accept `--repo /path/to/repo` and `--required-file <path>` for explicit
  overrides
- never hardcode `~/.codex`, `~/.claude`, `~/projects`, or any
  user-specific path
- never assume the cwd is the repo root — always resolve explicitly
- environment variables for paths only when documented in the skill
  (`AQG_ROOT`, `CODEX_HOME`, `CLAUDE_SKILL_DIR`) — no inventing new ones

These rules also future-proof skills for the cloud productization phase,
where `AQG_ROOT` will be a workspace-injected env var rather than a
local checkout.

## 6. Boundary declarations

Every skill body must contain an explicit boundaries / rules section
that excludes the following:

- writes to production systems, prod databases, prod credentials
- modifications to secrets or `.env` files
- branch-protection bypass (`gh pr merge --admin` etc.)
- destructive operations without explicit Owner authorization
  (`rm -rf`, `git reset --hard origin/<branch>`, `DROP TABLE`)
- Owner-only or external-permission actions

The skill is a **surface**: it provides structured prompt + helper.
Runtime enforcement is the **agent client's** responsibility (Claude
Code hooks, Codex policy checks, AQG `dangerous-guard.example.sh` for
ones that opt in). A skill that tries to enforce its own boundaries
will silently disagree with the client's enforcement and produce
inconsistent behavior across clients.

### 6.1 `boundary_class` semantics (sidecar field)

The `boundary_class` sidecar field is distinct from the §2.3 description
boundary *clause*: §2.3 is a human-readable phrase in the description, while
`boundary_class` is a machine-checked field that classifies the **runtime
behavior of the skill's `entry_script`** — not the whole-skill workflow, and
not the one-time Setup steps a human runs by hand.

Three values (`scripts/_skill_template_schema.py` `ALLOWED_BOUNDARY_CLASS`):

- **`read-only`** — the `entry_script` writes no disk state. Invariant A
  (schema) enforces `writes_paths: []` for this class. **Documented exception**:
  a default `git fetch` that updates remote-tracking refs under
  `.git/refs/remotes/*` does NOT break read-only (it touches neither the
  worktree, the source tree, nor any evidence file) and is therefore NOT listed
  in `writes_paths`; a skill relying on it must disclose the fetch in its
  SKILL.md Boundary section (with the skip flag, e.g. `--no-fetch`).
- **`writes-evidence`** — the `entry_script` writes evidence / ledger / state
  to local non-source locations (e.g. `.aqg/…`, the local append-only
  `events.jsonl`, `$CODEX_HOME/aqg-debug-cases`). List each path in
  `writes_paths`; the SKILL.md must carry an H2 Boundary section (for
  non-read-only classes a missing H2 is a hard error, not advisory).
- **`writes-code`** — the `entry_script` modifies user source files.

**One-time Setup is out of scope.** Steps a human runs once per repo to
provision the skill (install a hook, create `.aqg/`, append to `.gitignore`,
`cp`/`ln` a ledger) are NOT part of the `entry_script` boundary. A SKILL.md
that documents such Setup must label it as a manual provisioning step so the
body and the `boundary_class` field agree.

**`output_shape` describes the default.** `output_shape` is the shape of the
`entry_script`'s **default** stdout — the form emitted with no opt-in flag. If
the script emits markdown by default and JSON only under `--json`, the shape is
the markdown form (`multi_section_report` / `markdown_table`), not `json`.

## 7. Behavior testing

### 7.1 Fixture YAML

`tests/behavior/fixtures/triggers.yaml` holds the per-skill cases.
Each new skill needs at minimum 5 cases with a roughly 70 / 30 mix
of description-based vs. explicit-invocation prompts (matching the
sketch v3 §3.2 distribution).

### 7.2 Trigger keyword source

Pull keywords from real or anticipated user prompts (per §2.2). If you
are tempted to copy the description back into the fixture prompt to
"make the test pass," stop — you are about to overfit and lose the
real-user trigger signal.

### 7.3 Wrapper generation gate

The Claude wrapper is a generated verbatim copy of the source SKILL.md (with
sidecar anchor swaps). The CI `regen --check --all` + git-status gate keeps the
committed wrapper in sync, so an unintentional wrapper edit turns CI red. This
replaced the per-skill drift-hash baseline (retired 2026-05-31 §6/§7); there is
no longer a hash to recompute on a description / body edit — just rerun
`regen` (see §4.2). The fixture's M2 hash fields stay null as the
`managed ⟺ M2-null` marker that `check_fixture_mix.py` I7 asserts.

### 7.4 Nightly cadence

Behavior tests run nightly via `.github/workflows/behavior-tests-nightly.yml`
with real Claude invocation. Per-case Wilson lower bound is tracked in
`scripts/aqg_gate_analytics.py`; a sustained drop is the canonical
"trigger regression" signal and should be debugged before further
description changes.

### 7.5 Regression vs optimization

Reiterating: fixtures are **regression** assertions. If you find that a
fixture is failing because the user vocabulary has shifted, update both
the prompts and the description together — never just the description.

## 8. Anti-patterns

| # | don't | do |
|---|---|---|
| 1 | inline complete status enum / decision tree / multi-step workflow into SKILL.md body | put the logic in `scripts/`, the contract in tests, and a one-line summary in SKILL.md |
| 2 | write > 150 lines of body; use long-form prose | move rationale to `docs/discussion/`, keep body skeletal |
| 3 | hardcode `~/.codex`, `~/.claude`, `/Users/<name>` paths | use `${CODEX_HOME:-$HOME/.codex}`, resolve from `AQG_ROOT` |
| 4 | enforce boundaries inside the skill (auto-block, auto-redact) | declare boundaries; let the agent client enforce |
| 5 | use AQG-internal jargon ("three-auditor", "convergent", "objection prediction") in the description without mirroring user vocabulary | derive trigger words from real user prompts (§2.2) |
| 6 | import third-party Python packages in helpers | stdlib only — easier to ship, easier to audit |
| 7 | add frontmatter keys beyond `name` and `description` | use the doctor / fixture / test layer for additional metadata |
| 8 | invent a new AQG_ROOT resolver | reuse the §3.1 boilerplate verbatim until the shared lib lands |

## 9. Worked example

`templates/example-skill/` contains a minimal **source skill** plus a
registration checklist for the downstream touchpoints. It demonstrates:

- §1 frontmatter + body shape + scripts layout + §1.4 exit-code contract
  (covers exit 0, 1, 2, 3, and 70 via `--fail`, `--bad-config`, `--crash`
  flags)
- §2 description grammar (lead verb, user-vocabulary trigger words,
  boundary clause, path-portable declaration)
- §3.1 source-skill resolver variant (`AQG_ROOT` required — Codex case)
- §5 path portability (`--repo` defaulting to git root, `--required-file`
  override)
- §6 boundary declarations
- §7 self-test exercising the exit-code contract

What the template does **not** ship (because it lives in `templates/`,
not `skills/`):

- the Claude wrapper (touchpoint 2)
- install/doctor/fixture registration + `wrapper_generated` opt-in + Codex `display_name` + the two roster canaries (touchpoints 3–11)

For those, `NOTES.md` in the template gives a per-touchpoint checklist
with grep-friendly references into the existing production skills.
A new author copies the template and walks through `NOTES.md §4` to
register the real skill.

```bash
cp -R templates/example-skill skills/aqg-<your-name>
# ...edit name, description, helper, then walk NOTES.md §4 for downstream registration
```

The example is intentionally **not** registered in the install scripts
or doctor so it stays a clean reference without polluting the real skill
index.

## 10. Where to file follow-ups

- **AQG_ROOT shared lib extraction** (per §3.2): track it in
  the decision log (`docs/decisions/LOG.md`) or a future ADR
- **Machine-readable AI template** (per §0.1): same — pair with the
  shared lib extraction
- **New behavior testing dimensions** (e.g. cost ceilings, multi-turn
  trigger): file as a sketch under `docs/discussion/`, audit, then
  amend §7 of this guide
