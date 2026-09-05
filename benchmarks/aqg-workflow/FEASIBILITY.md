# Feasibility probe: can we benchmark AQG-workflow adherence with headless claude?

*2026-06-21. `claude` 2.1.185, Haiku 4.5. Empirical — every claim below is from
an actual `claude -p` run, not docs.*

The MVP (#320) shipped the scorer + selftest but deferred `run.py` behind one
make-or-break question: can a headless `claude -p` run (a) load AQG skills for
the `aqg-full` arm while a `baseline` arm has them isolated out, and (b) let us
detect that the agent actually followed the workflow? This probe answers it.

## Verdict: feasible, with caveats that shape `run.py`

| question | answer | evidence |
|---|---|---|
| Nested `claude -p` runs at all? | **yes** | `--output-format json` → `is_error:false`, `result:"PROBE_OK"`. Auth is inherited from the host login. |
| `--bare` for a clean run? | **no** | `--bare` skips *plugin credentials* → `"Not logged in"`. Use `--setting-sources`, not `--bare`. |
| Can `baseline` isolate out AQG? | **yes — partial, needs combining** | trivial prompt: default load = 34,555 cache-creation tok / $0.072; `--setting-sources project` = 21,876 tok / $0.046 (−37%). The remainder is repo-project settings + default MCP/tool schemas; `--strict-mcp-config` + a neutral cwd drive it lower. |
| Can we detect tool/skill calls? | **yes** | `--output-format stream-json --verbose` emits per-event JSON; a Bash task surfaced `tool_use name="Bash"`. Skill invocations travel the same `tool_use` channel, so an `aqg-code-construction` call is detectable the same way. |

## The non-obvious constraint: AQG is a skill, not a plugin

ponytail is a **plugin** — a SessionStart hook injects its rules into every
turn, so its effect is unconditional. AQG is a **skill** — description-triggered,
the model decides whether to invoke it. So "the AQG arm" cannot be just "skills
present on disk"; the workflow only fires if it is *triggered*. The arm
definitions must reflect this:

| arm | how | isolation flags |
|---|---|---|
| `baseline` | neutral cwd, no AQG at all | `--setting-sources project --strict-mcp-config` |
| `aqg-full` | AQG skills available **+ the AQG CLAUDE.md rules injected** (mirrors the real Owner setup that *mandates* the skills, not just exposes them) | user settings + `--append-system-prompt-file <aqg-rules>` |
| `claude-md-lite` | a few lines of YAGNI / validation rules only | `--append-system-prompt "<short rules>"` |

Adherence is then measured two ways from the `aqg-full` run: skill `tool_use`
events in the stream-json, and any construction / closeout ledger artifact left
on disk.

## What `run.py` must do (next chunk)

1. **Per cell**: fresh temp copy of the task seed; `claude -p --output-format
   stream-json --verbose` with the arm's flags; `--no-session-persistence` (this
   probe leaked session state into `~/.claude/projects/`); `--model haiku`.
2. **Score**: run `checks.run_checks` on the produced file; parse stream-json
   for tool/skill adherence; read cost / tokens / turns from the `result` event.
3. **Isolation hard-check**: assert the `baseline` cell sees zero `aqg-*` skills
   — else it is contaminated, the exact bug ponytail caught in its own benchmark
   (a SessionStart hook firing on every arm).
4. **Budget**: even isolated, a cell is ~$0.05 on Haiku; `N tasks × 3 arms × n
   runs` must be budgeted (and logged, not silently capped) before a full run.

## Cost note

This probe was ~4 Haiku calls, ~$0.20 total. The full benchmark is bounded by
the cell count above; keeping `baseline` context minimal is what controls it.

## `run.py` validation status & known limitations

`run.py` shipped (#327). Its pure logic — scoring, stream-json parsing, the
isolation hard-check, arm/argv building, budget — is proven by `test_run.py`
with fixtures (zero API). A Standard external audit (`9aad54d9`) hardened it:
subprocess timeouts/spawn-failures/non-zero-exits are caught (one bad cell never
aborts the matrix); execution failures (`infra_failed`) and un-importable
solutions (`errored`) are segregated from the defect mean so a worst-case outcome
can't masquerade as a zero-defect win; `--max-cost` bounds **actual** cumulative
spend, not just the pre-run estimate. All CLI flags are confirmed against
`claude --help` on 2.1.185.

Two limitations remain, deliberately deferred (not silently capped):

1. **`aqg-full` carries a user-env confound.** To have the AQG *skills* available
   (not just the rules text), `aqg-full` runs with the full user settings — so it
   also carries any *other* user-level skills / MCP / hooks. The `baseline ↔
   aqg-full` delta therefore conflates "AQG present" with "full user config". The
   **clean** comparison is `baseline ↔ claude-md-lite` (both isolated; the only
   difference is the injected rules) — that arm is the rigorous test of "isn't AQG
   just a few lines of CLAUDE.md?". Fully isolating "only AQG" needs a per-skill
   settings source and is a follow-up. The isolation hard-check still asserts
   `aqg-full` actually loaded AQG (`require_aqg_present`).

2. **Statistical power.** A meaningful baseline↔arm comparison needs N>1 runs per
   cell (and more tasks); a single run per cell is a plumbing check, not a result.

## Live smoke — DONE (2026-06-22, 3 arms × 1 run, actual $0.17)

The Owner-authorized 3-arm `--execute` smoke validated the runner end-to-end and
confirmed the init-event schema (previously fixture-assumed):

- **init event lists capabilities** in `slash_commands`, `tools`, `mcp_servers`,
  `skills`, `agents` — exactly the fields the isolation scan checks.
- **isolation is correct on real data:** `baseline` + `claude-md-lite` saw **0
  `aqg-*`** (skills=14, all built-in; `--setting-sources project --strict-mcp-config`
  isolates AQG out); `aqg-full` saw the **15 real AQG skills** (skills=244,
  slash_commands=330 — the full user env) → `require_aqg_present` holds.
- **scan tightened:** the smoke showed the sandbox cell path (`…__aqg-full__r0`)
  leaks into the `memory_paths` list, so `aqg_skill_signals` now scans ONLY the
  capability fields above (not every list, and never scalar/path fields) — precise,
  and still fail-closed when no capability field is present.
- cost scaled as expected: `aqg-full` ($0.104, 4 turns) ≈ 3× `baseline` ($0.033,
  3 turns), reflecting the injected 12 KB rules + larger context.

All three arms passed the (trivial) task at n=1 — **not a result**, just proof the
instrument runs, scores, and isolates correctly. Real numbers need N>1 (limitation 2)
and the clean `baseline ↔ claude-md-lite` framing (limitation 1).

## WS-7 protocol follow-up — pending primary collection

The historical smoke above remains a three-arm plumbing observation. The
authorized WS-7 protocol removes its acknowledged `aqg-full` user-environment
confound by loading AQG through a temporary explicit plugin wrapper under the
same project-only settings and strict-MCP flags as every other arm. It adds an
explicit third-party active control, ten held-out instruments, twenty repeats,
and a frozen analysis/budget contract. See [PREREGISTRATION.md](PREREGISTRATION.md).
No new outcome is implied here until that primary matrix completes its validity
gates and the frozen analyzer renders its result.
