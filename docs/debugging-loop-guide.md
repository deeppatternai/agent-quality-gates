# Debugging Feedback-Loop Guide (companion to aqg-systematic-debugging)

This guide holds the detailed Phase 0 methodology for the `aqg-systematic-debugging` skill. The skill's SKILL.md keeps the core directive (build a fast, deterministic pass/fail loop FIRST; do not hypothesize or fix without one) and the cannot-build-loop stop rule; the strategy detail lives here so the skill body stays under the agent-context guide limit.

## Why the loop is the whole game

If you have a fast, deterministic, agent-runnable pass/fail signal for the bug, you will find the cause — bisection, hypothesis-testing, and instrumentation all just consume that signal. If you don't have one, no amount of staring at code will save you. Spend disproportionate effort here. **Be aggressive. Be creative. Refuse to give up.** Do not proceed to a hypothesis or a fix until the loop distinguishes the failure.

## 10 ways to construct a loop (try in this order)

**Note**: if static analysis (type / lint / undefined-name detection — tier 1 below) is enough to distinguish the failure, use it first before constructing any runtime loop.

1. **Failing test** at whatever seam reaches the bug — unit, integration, e2e.
2. **Curl / HTTP script** against a running dev server.
3. **CLI invocation** with a fixture input, diffing stdout against a known-good snapshot.
4. **Headless browser script** (Playwright / Puppeteer) — drives the UI, asserts on DOM / console / network.
5. **Replay a captured trace.** Save a real request / payload / event log to disk; replay it through the code path in isolation.
6. **Throwaway harness.** Spin up a minimal subset of the system (one service, mocked deps) that exercises the bug code path with a single function call.
7. **Property / fuzz loop.** If the bug is "sometimes wrong output", run 1000 random inputs and look for the failure mode.
8. **Bisection harness.** If the bug appeared between two known states (commit / dataset / version), automate "boot at state X, check, repeat" so you can `git bisect run` it.
9. **Differential loop.** Run the same input through old-version vs new-version (or two configs) and diff outputs.
10. **Structured manual fallback (HITL).** Last resort when no automated loop is feasible. A *structured* script can still drive the human, capture stdout/stderr/timestamps, and emit explicit pass/fail. Document the manual steps + the captured-output schema + the pass/fail criteria so each run is comparable across iterations.

Build the right feedback loop, and the bug is 90% fixed.

## Fidelity-cost tiers — pick the cheapest that still distinguishes the failure

Iterate cheap-to-expensive; do not jump to slow tiers while a faster one still has signal.

| rank | loop | typical latency | use when |
|---|---|---|---|
| 1 | type / lint / static analysis | < 5 s | type mismatch, unused import, undefined name |
| 2 | failing unit test in isolation | < 30 s | logic error inside one function or module |
| 3 | small integration test (real deps, no network) | < 2 min | wiring between modules, fixture setup |
| 4 | targeted reproduction script | < 5 min | data shape mismatch, manual repro of CI failure |
| 5 | targeted smoke run / dry-run script | < 5 min | side effects without committing state, narrower than full suite |
| 6 | full test suite | minutes | regression suspected across modules |
| 7 | E2E or real-environment probe | 10+ min | only when 1-6 cannot distinguish |
| 8 | production observation (logs / metrics / traces) | hours | only after read-only access is authorized; offline/sanitized or non-read-only replay needs separate authorization |

If a tier returns ambiguous evidence, first look for a faster tier that can actually distinguish the hypothesis. If none applies (the failure depends on env / network / deployment / production-only behavior), escalate to the cheapest higher-fidelity tier and record why faster tiers were insufficient.

## Iterate on the loop itself

Treat the loop as a product. Once you have *a* loop, ask:

- Can I make it **faster**? (Cache setup, skip unrelated init, narrow the test scope.)
- Can I make the **signal sharper**? (Assert on the specific symptom, not "didn't crash".)
- Can I make it **more deterministic**? (Pin time, seed RNG, isolate filesystem, freeze network.)

A 30-second flaky loop is barely better than no loop. A 2-second deterministic loop is a debugging superpower.

## Non-deterministic bugs — raise the reproduction rate

The goal is not a clean repro but a **higher reproduction rate**. Loop the trigger 100×, parallelise, add stress, narrow timing windows, inject sleeps. A 50%-flake bug is debuggable; 1% is not — keep raising the rate until it's debuggable.

## Hypotheses — rank before you test

Once the loop distinguishes the failure, don't fixate on the first plausible cause. Generate **3-5 ranked, falsifiable hypotheses** before running any of them, each in the form:

> "If [X] is the cause, then [Y] will make the bug disappear (or get worse)."

Test the cheapest-to-falsify first and let the loop's evidence re-rank the list. Surfacing the ranked list (to the user or a reviewer) before testing lets domain knowledge re-order it and guards against confirmation bias — fixating on the first plausible cause. Still test **one hypothesis at a time** (per the skill workflow); ranking is about *generation*, not parallel fixing. (Source: mattpocock/skills `diagnose`.)

## Rules — apply throughout the loop

- **Bisect when you can**: `git bisect`, binary-search the diff, halve the reproduction input — beats linear search at cheap cost.
- **Print before debugger**: a `print` / `logging.debug` line is faster to add and remove than wiring up an interactive debugger; reach for the debugger only when state is hard to inspect statically.
- **Minimal repro before fix**: shrink to the smallest input that still fails before changing code — confirms what you are actually fixing.
- **One variable per iteration**: change exactly one thing per loop run so the result is interpretable.
- **Record what you tried**: append "tried: <change> → <observed>" to the debug case so the next loop avoids re-running the same experiment.
- **Tag throwaway instrumentation**: prefix every debug log / probe you add with a unique marker (e.g. `[DEBUG-a4f2]`) so cleanup is a single `grep`, and confirm the marker is gone before closing the case — stray debug output leaking into production is a real failure mode. (mattpocock/skills `diagnose`.)
- **Patch-accumulation circuit-breaker**: after ~3 failed fix attempts on the *same* root cause, stop patching — the cause is likely structural (no test seam, tangled callers, hidden coupling). Escalate to an architecture review instead of stacking more patches. (obra/superpowers `systematic-debugging`.)

## After the fix — convert the root cause into prevention

Verifying the fix is not the end. Ask one more question: **"what would have prevented this bug?"** If the honest answer is structural — no good test seam, tangled callers, a missing invariant, hidden coupling — hand that off to an architecture review with specifics, rather than marking the case closed. This is the difference between fixing a symptom and improving the system. (Distinct from `aqg-evidence-closeout`, which records *what changed*, not *what would have prevented it*; source: mattpocock/skills `diagnose`.)
