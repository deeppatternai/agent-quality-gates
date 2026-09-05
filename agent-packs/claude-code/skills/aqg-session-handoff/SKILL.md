---
name: aqg-session-handoff
description: Generate a paste-ready cross-session handoff prompt that lets a next-session LLM cold-start on a generic project — repo/branch + environment, what is done and verified, current working state, next steps, discipline traps, the first concrete action, and open decisions. Two modes — `new` prints an 8-section skeleton (Done-so-far prefilled from the construction ledger when present); `validate` reads the filled handoff from stdin and gates it (8 sections present, no placeholder or secret residue, plus first-step / actor / audit-state checks) before paste. For a generic project session handoff WITHOUT EAF engine run state — if the repo is running an EAF engine, use eaf-session-handoff instead (it captures run_id / hold-gate / transition_log, which this does not); for a completion product HANDOFF.md, use eaf-handoff. Use at session boundaries, high context, after compaction, or when the user asks for a handoff to continue next session. Not a substitute for aqg-evidence-closeout (in-session completion evidence).
---

# AQG Session Handoff

Structure "the context of one work session" into a paste-ready prompt that lets the next-relay
LLM cold-start, replacing ad-hoc improvised handoffs. Generic layer (any repo) —— for an EAF
engine run use `eaf-session-handoff` (captures engine state), for a completion product document
use `eaf-handoff`.

## Workflow (mandatory order)

```
new(emit skeleton) → agent fills each section by hand → validate(gate before paste) → present to the user wrapped in ONE outer code fence → paste into next session
```

`validate` is **not an optional step**: it scans for secret + placeholder residue + structure,
gating before paste. This skill is a read-only CLI and technically cannot stop you from skipping
it —— "running validate" relies on discipline (same as all AQG skills: skill surfaces, human
acts). But gates like secret-scan only cover this handoff when you run validate.

## How To Run

> **Repo context**: agent-quality-gates is the development source — the skill's decision logic
> lives in local scripts, in the open. Just run the scripts below. (An `aqg-cli` / cloud form is
> not part of this release.)

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script="$aqg_root/skills/aqg-session-handoff/scripts/aqg_session_handoff.py"

# Use a unique name for the relay file (with timestamp) —— this skill is read-only and writes
# nothing to disk; this .md is your own scratch. Don't reuse a fixed name (like handoff-filled.md):
# multiple sessions / multiple handoffs in one session would overwrite each other.
hf="/tmp/handoff-$(date +%Y%m%d-%H%M%S).md"

# 1. Emit the 8-section skeleton (Done-so-far auto-prefilled from the construction ledger, if any) → store into $hf
python3 "$script" new > "$hf"             # default cwd
python3 "$script" new --repo /path/to/repo > "$hf"

# 1b. CTX tight (≥80–90% or imminent passive compaction) → degraded 3-section version (mission/🟡 stop-point/first step)
python3 "$script" new --minimal > "$hf"

# 2. agent fills each section of $hf by hand (replace all <FILL: ...> slots; keep the Environment section secret-free)

# 3. gate before paste (read the filled handoff from stdin)
python3 "$script" validate < "$hf"
python3 "$script" validate --minimal < "$hf"   # degraded gate (validates only 3 sections; secret scan still runs)
python3 "$script" validate --json < "$hf"   # machine-readable
```

Depends only on the Python 3.9+ standard library (no third-party packages).

## Fill guidance (when filling each section by hand)

- **Evidence anchoring (don't write state from memory)**: extract background from this session's known
  state (plan / TodoWrite / files changed in this session / open questions asked but unanswered),
  **but anchor the state sections by running a few cheap commands** —— `git status` /
  `git log --oneline -5` (O(1) cheap); don't run the full test suite just to fill a handoff (that's
  exactly the token spend to avoid), use the **latest known state, or run only the relevant quick
  check**. **Pick what applies per project; on non-git / no test command / read-only environments,
  skip that anchor and note "not collected: X"** rather than letting it block handoff generation. When in
  conflict with memory, **trust the command output** (memory drifts after compaction, the main
  source of state drift). Don't do a full `Glob` / broad `grep` re-audit, but don't rely purely on
  memory either. **Write only the command name + a conclusion summary; never paste raw command
  output / `env` / config dump** (high-entropy leak surface + signal dilution).
- **Fill in reverse order**: first lock down "the first step (section 7) + next step (section 5)" in your
  head, then work backwards to fill sections 2-4 with the minimal background that supports them ——
  writing top-down, your energy is spent by the time you reach the first step, wrecking exactly the
  most important part.
- **Review the whole conversation, not just the last few turns** —— handoff errors usually come from
  summarizing only the recent context.
- **Write 🟡 in-progress items down to "specific stop-point + strategy"**: the 🟡 in Current Working State
  must be written down to `file:line` / the specific error / the half-finished form, **and add a one-line
  current strategy/assumption** ("knowing where you stopped" isn't enough, you also need "why you went
  this way"); include anything running (background process shell ID + kill command, dev server + port,
  open worktrees —— if not passed on, the next relay can't find or kill them).
- **Use absolute paths** —— the next relay's working directory may differ.
- **Write `none` for missing content, don't drop the section** —— stable structure is the core (validate
  also blocks blanks).
- **Use 4-space indentation for multi-line commands / code, not triple-backtick fenced code blocks** ——
  the whole handoff is pasted to the next relay as **one code box**; the moment a triple-backtick fence
  appears in the body, the inner fence closes the outer box early and breaks the copy (the next relay can
  only select up to the fence, and has to manually paste the rest). For multi-line commands, prefix each
  line with 4 spaces (still renders as a code block in the box, and doesn't break the box). The `new`
  hints for the First step / Current Working State sections also flag this; when the body contains a
  triple-backtick fence, validate directly **BLOCK**s (`valid=False`, must be changed to 4-space
  indentation and re-run to pass —— a non-blocking warning can't stop it, and box-breaking keeps
  recurring). The trigger is **any line-leading triple-backtick** (≤3 spaces of indentation counts as a
  fence opener, including bare example lines); only command lines with **≥4 spaces of indentation** are
  safe.
- **Present the whole validated handoff wrapped in a SINGLE outer triple-backtick code fence** —— this
  is the required final step, not a stylistic option. A fenced block is what makes the host UI render the
  handoff as one copy-able unit (the one-click copy button), instead of bare Markdown prose the user has
  to hand-select line by line. It is the necessary complement to the fence-free-body rule above: the body
  carries no inner fences *precisely so* one outer fence can wrap the entire handoff without closing
  early. So after `validate` passes, always emit the handoff to the user as one fenced block; never paste
  it as bare text. Any Markdown-rendering agent client (Claude Code, Codex, etc.) shows a fenced block
  with a copy affordance, so this ships the same one-click-copy experience to every user, on any machine
  —— it does not depend on per-user or per-machine configuration.

**Two self-checks before validate** (validate only checks structure / secret, it can't catch "should
have been written but wasn't"; these two cover the semantic blind spot):
- **Reader simulation**: read the whole thing through, imagine a zero-memory agent that can only read
  files and run commands executing the first step —— at which step would it be forced to look up
  something it can't find (intent / verbal agreements / stop-points / ownership of half-finished work)?
  If so, add it.
- **Inter-section consistency**: every fact the first step (section 7) depends on can be found in
  sections 2-4 without contradiction (the reverse-order fill guards against a "dangling first step").

> **Degraded path**: CTX tight (≥80–90% or imminent passive compaction, no budget to run the full
> process) → `new --minimal` emits 3 sections (mission / 🟡 stop-point / first step), skipping
> collection and the full self-check, **but the secret check cannot be skipped** (`validate --minimal`
> still runs R3). The banner prompts the next relay to collect and verify on its own before the first
> step. There is also a ≤150-line **soft cap** (over it, validate emits a warning, not a fail; can be
> exceeded if task complexity genuinely needs it).

> **Continuity footer (auto-added, don't delete)**: after the 8 sections, `new` auto-appends an
> `<!-- AQG-CONTINUITY-FOOTER -->` continuity-discipline block —— it tells the next relay to "invoke the
> skill directly rather than a manual equivalent." **The footer is not a handoff request or trigger.**
> It says that only after a separate observable trigger should a future handoff be produced with this
> skill rather than freestyled (breaking the degradation chain). `validate` strips it at the sentinel before
> sectioning (so it doesn't pollute section 8), but secret-scan still covers the whole text. Keep it when
> filling, don't delete it.

## 8-section contract

| # | section | empty-value rule |
|---|---|---|
| 1 | Goal | required, real |
| 2 | Environment | required, real; secret-free |
| 3 | Done so far | required, real + audit state (`audit_id ...` or explicit `无 audit`) |
| 4 | Current Working State | may be none (`none` / `worktree clean`) |
| 5 | Next | required, real |
| 6 | Discipline traps | may be none (`none` / `N/A`) |
| 7 | First step | required, real; rejects placeholder words (`继续之前的活` / `TBD` …) |
| 8 | Open decisions | may be none; if not none, each item names an actor (Owner / a specific name / @user) |

"may be none" ≠ "may be blank": blank = forgot to fill (fail); explicit `none` = considered (pass).

## validate gate (R1-R8, structure present + light heuristics, no semantic judgment)

- **R1** all 8 `## N. Name` headers present (preamble before the first header, such as the minimal banner, is skipped).
- **R2** no residual `<...>` placeholders (the authoritative catch for unfilled slots).
- **R3** secret scan (reuses the closeout redaction bank, fail-closed) —— a hit fails, echoing only the
  line number + `[REDACTED:<type>]`, never returning the original secret.
- **R4** required sections real (non-empty, not pure `<...>`, not a lone none-token).
- **R5** may-be-none sections: real content OR an explicit none-token applicable to this section; blank fails.
- **R6** First step rejects placeholder/vague words (fails only when the whole section equals a blacklisted word; a real step that contains the word passes).
- **R7** Open decisions, when not none, must contain an actor token per item (existence check, not semantic).
- **R8** Done-so-far mentions audit / 审 (or explicit `无 audit`) —— a lightweight existence check, prompting a conscious account of the audit state.

> Note: validate sections by the exact `## N. <SectionName>` contract header; **don't write
> contract-form headings in the prose** (e.g. writing `## 5. Next` in Done-so-far as a reference) ——
> it would be taken as a section boundary and cause an R1 false positive. Reference a section name with
> backticks `Next` or drop the `## N.` prefix.

## Boundaries

- `boundary_class: read-only` —— prints stdout / reads stdin, **never writes the filesystem**, does not
  push / merge / deploy; writes nothing to disk (no `--save`, matching memory-hygiene's "use a
  paste-prompt for handoff, not memory").
- secret detected → **block (fail gate)** so the author removes it and switches to an env-var reference,
  **not** redact-and-ship on their behalf.
- This skill does not include EAF engine state (run_id / hold-gate / transition_log); for an EAF engine
  run use `eaf-session-handoff` instead. Routing is decided at the rules layer (CLAUDE.md / hook); this
  skill does not actively probe the EAF workspace; `validate` only gates structure/secret, it does not
  re-route.
- Not actively triggered: handoff is a session-boundary action, not run automatically after every closeout.
- Production / deploy / secrets / Owner-admin remain separate authorization gates that this skill does not touch.

Exit codes: `0` success (new printed / validate passed); `1` validate gate failed; `2` usage error;
`3` / `70` reserved.
