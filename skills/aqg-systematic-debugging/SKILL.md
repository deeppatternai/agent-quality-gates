---
name: aqg-systematic-debugging
description: Root-cause debugging workflow for unresolved project failures or contradictory evidence, including failing checks, CI errors, and preflight failures. Use it to build a reproducible feedback loop, test one hypothesis at a time, and verify a minimal fix before proposing changes; do not use it for ordinary implementation, review, or already-understood small fixes.
---

# AQG Systematic Debugging

Purpose: turn a failing or contradictory project state into an evidence-backed
root cause. The core rule is: **no root-cause evidence, no fix proposal.**

Use when an unresolved failure or evidence conflict blocks a safe fix: failing
commands/checks/CI/tests/probes/preflight, disagreement between local/CI/logs/
status/model/audit evidence, or a recurring failure after prior fixes. Do not
use for ordinary implementation/review, already-confirmed small fixes, external
audits, product/architecture decisions, or production-write boundaries.

Expected output: a reproducible loop or explicit blocker, the evidence tested,
the confirmed root cause or current best non-confirmation, and the minimal next
action.

## Phase 0 — Build the Feedback Loop FIRST

**This is the skill.** Everything else is mechanical. If you have a fast,
deterministic, agent-runnable pass/fail signal for the bug, you will find the
cause — bisection, hypothesis-testing, and instrumentation all just consume
that signal. Spend disproportionate effort here. **Be aggressive. Be creative.
Refuse to give up.** **Do not proceed to a hypothesis (step 5) or a fix
(step 6) until the loop distinguishes the failure.**

Try loop strategies cheapest-first — static analysis → failing unit test →
integration test → targeted reproduction script → smoke/dry-run → full suite →
E2E → (authorized) production observation. Pick the cheapest tier that still
distinguishes the failure, then iterate on the loop to make it faster, sharper,
and more deterministic. For non-deterministic bugs the goal is a higher
reproduction rate (loop 100×, parallelise, add stress) until it is debuggable.

The full method — the 10 ways to construct a loop, the fidelity-cost tier
table, loop-iteration tips, and non-deterministic strategies — is in
[`docs/debugging-loop-guide.md`](../../docs/debugging-loop-guide.md).

### When you genuinely cannot build a loop

Stop and say so explicitly. List what you tried. Ask the user for: access to
the environment that reproduces it, OR a captured artifact (HAR file, log dump,
core dump, timestamped screen recording), OR permission to add temporary
production instrumentation. Do **not** proceed to the hypothesis steps without
a loop.

## Workflow

1. Define the symptom in one sentence.
   - Include exact command, check, PR/issue, probe, artifact, or file path.
   - Use absolute dates for time-sensitive state.
2. Capture a fresh reproduction (this is your first iteration of the Phase 0 loop).
   - Rerun the smallest command that shows the failure.
   - Save the exact error, exit code, and relevant lines.
   - If it cannot be reproduced, state what evidence is missing and collect more data before fixing.
3. Run project preflight when repo state may matter:

   ```bash
   source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
     echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
     exit 2
   }
   python3 "$aqg_root/skills/aqg-startup-preflight/scripts/aqg_preflight.py" --no-fetch
   ```

   Use fetch only when live remote tracking is needed and allowed.
4. Trace the failure to a boundary.
   - Git/GitHub state: branch, upstream, PR, issue, diff, dirty files.
   - Code path: caller -> callee -> storage/network boundary.
   - Evidence path: corpus/split/manifest -> scoring -> report -> status doc.
   - Production/read-only path: local script -> SSH/probe -> service endpoint -> logs.
5. Form falsifiable hypotheses.
   - If the cause is still ambiguous, generate 3-5 ranked candidates before testing.
   - State: "I think X is the cause because Y evidence."
   - Test it with the smallest check.
   - Test one hypothesis per loop iteration; do not stack speculative fixes.
6. Only after evidence supports a root cause, choose the minimal fix.
   - Keep repo-only/offline, read-only production checks, production authorization, and runtime writes separate.
   - Do not cross an Owner-only or production boundary while debugging.
7. Verify the fix against the original symptom and a regression check **scaled to blast radius**.
   - Trivial: one focused check can be enough.
   - High-stakes (security / concurrency / multi-file / irreversible): derive the adjacent regression surface from the changed paths and nearest tests/modules.
   - Run the smallest targeted subset (e.g. `pytest <those paths>` / `-k`) that covers the risk, and record the scope + rationale.
   - Stay focused: not the full suite; the authoritative clean-env run is CI's job or the project's gate.
8. Close the loop.
   - Remove temporary instrumentation and confirm any debug marker is gone.
   - Ask what would have prevented the bug; if the answer is structural, hand it off as an architecture/testability follow-up.
   - Use `aqg-evidence-closeout` before final answer.

If the referenced startup-preflight or evidence-closeout skill is unavailable, record that tool gap in `Boundaries` and continue with equivalent manual checks.

## Debug Case Helper

Create a debug case when the investigation spans more than one command or
hypothesis, crosses a repo/CI/production-read-only boundary, or may need to be
resumed later. A short inline report is enough for a single local failure solved
in one loop.

Resolve the AQG root, then generate or validate a debug case skeleton:

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script="$aqg_root/skills/aqg-systematic-debugging/scripts/debug_case.py"

# Generate a skeleton:
python3 "$script" new --title "<short failure name>"

# Validate a filled debug case:
python3 "$script" validate path/to/debug-case.md
```

The validator requires sections for symptom, fresh reproduction, evidence, hypotheses, root cause, fix, verification, and boundaries.

By default `debug_case.py new` writes the skeleton under `$CODEX_HOME/aqg-debug-cases/` (`$CODEX_HOME` defaults to `~/.codex`). This is the skill's `writes-evidence` boundary — local debug-case evidence only; no production, source, or secret write.

## Stop Rules

Stop and ask only when the next step requires:

- production write/deploy/restart/rollback
- secrets, credentials, private/raw data, or Owner-only permissions
- architecture/repository topology/data-model direction
- label-policy or product semantics decision
- another expensive external audit

Otherwise continue collecting evidence or applying accepted minimal fixes.

## Reporting Shape

```markdown
Debug status:
- symptom: <one sentence>
- reproduction: `<command>` -> <exit/error>
- evidence: <key lines/facts>
- hypothesis tested: <result>
- root cause: <confirmed / not confirmed>
- cleanup/prevention: <debug instrumentation removed / structural follow-up>
- next action: <minimal fix or blocker>
```
