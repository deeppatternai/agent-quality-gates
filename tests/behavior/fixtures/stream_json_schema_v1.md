# Claude Code stream-json Schema for Skill Activation Detection

> **schema_version:** 1
> **trigger:** Q3 #10 sketch v2 §2 PR-0 spike (extractor hypothesis confirmed)
> **claude_code_version:** `2.1.119` (at the time of this spike)
> **current schema scope**: AQG behavior tests v0 (Sub-agent Review Manifest extraction; Q3 #10 Wave 3 implementation)

## 0. Why this doc exists

Sketch v2 §2 three-auditor review (audit_id `53e08672`) finding #10: the extractor hypothesis needed confirming first — "is Claude Code skill activation represented as a parseable tool_use event?". This PR-0 spike captured the schema by actually running `claude -p --output-format stream-json`, documenting + versioning it against future Claude Code upgrades.

Schema mismatch → the extractor must bump version (add `stream_json_schema_v2.md`) + behavior tests use the extractor for the matching schema version.

## 1. Top-level event types (6 kinds, spike 1 + spike 2 converged)

The JSONL emitted by `claude -p --output-format stream-json --include-partial-messages`, one event per line. Top-level `type` field:

| type | frequency (spike 2) | purpose |
|---|---|---|
| `stream_event` | most (~70%) | real-time partial message chunks (token-level streaming) |
| `assistant` | ~15 events | complete assistant message (contains tool_use block — **key for skill extraction**) |
| `user` | ~10 events | user input + tool_result (the latter triggered by sub-agent or tool feedback) |
| `system` | ~18 events | init / hook lifecycle / status / task_progress / post_turn_summary |
| `rate_limit_event` | occasional | provider rate-limit notification |
| `result` | 1 (last) | terminal state: cost / duration / num_turns / stop_reason / usage breakdown |

## 2. Skill activation extraction path (CONFIRMED via spike)

### 2.1 Full event shape (raw, from spike 2)

```json
{
  "type": "assistant",
  "parent_tool_use_id": null,
  "session_id": "<uuid>",
  "uuid": "<event uuid>",
  "message": {
    "model": "claude-sonnet-4-6",
    "id": "msg_<id>",
    "type": "message",
    "role": "assistant",
    "content": [
      {
        "type": "tool_use",
        "id": "toolu_<id>",
        "name": "Skill",
        "input": {
          "skill": "aqg-startup-preflight"
        },
        "caller": {
          "type": "direct"
        }
      }
    ],
    "stop_reason": "tool_use",
    "usage": { "...": "..." }
  }
}
```

### 2.2 Extraction algorithm (pseudo-code, version 1)

**Top-level vs delegated boundary** (audit `717918e8` finding #2): the extractor explicitly distinguishes `parent_tool_use_id is None` (top-level direct call) vs sub-agent delegated. The v1 baseline only counts top-level (to avoid sub-agent spawn false positives), but surfaces delegated for diagnostics.

**Diagnostics return** (audit `717918e8` finding #7): returns not only skill_calls, but also parse stats so the §3 status decision is executable.

```python
SCHEMA_VERSION = 1  # bump on Claude Code stream-json schema change

def extract_skill_calls(jsonl_path: Path) -> tuple[list[dict], dict]:
    """
    Returns (top_level_skill_calls, diagnostics).

    top_level_skill_calls: ordered list of {skill_name, tool_use_id, caller_type, args}
      — only includes calls where parent_tool_use_id is None (top-level, not sub-agent delegated)
    diagnostics: {
      "total_nonempty_lines": int,
      "json_decode_errors": int,
      "parse_error_rate": float,  # for PARSE_ERROR status (>5% threshold)
      "delegated_skill_calls": list[dict],  # surfaced separately, not gated
      "init_event_present": bool,
      "init_skills_loaded": list[str] | None,  # from system.init.skills
      "result_event_present": bool,
      "result_is_error": bool | None,
      "result_api_error_status": str | None,
      "result_total_cost_usd": float | None,
    }
    """
    top_level_calls = []
    delegated_calls = []
    diag = {
        "total_nonempty_lines": 0,
        "json_decode_errors": 0,
        "parse_error_rate": 0.0,
        "delegated_skill_calls": [],
        "init_event_present": False,
        "init_skills_loaded": None,
        "result_event_present": False,
        "result_is_error": None,
        "result_api_error_status": None,
        "result_total_cost_usd": None,
    }

    for line in jsonl_path.read_text().splitlines():
        if not line.strip():
            continue
        diag["total_nonempty_lines"] += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            diag["json_decode_errors"] += 1
            continue

        # init event — capture skill load list for INFRA_ERROR detection
        if event.get("type") == "system" and event.get("subtype") == "init":
            diag["init_event_present"] = True
            diag["init_skills_loaded"] = event.get("skills", [])
            continue

        # result event — capture for status decision (auth / cost / infra)
        if event.get("type") == "result":
            diag["result_event_present"] = True
            diag["result_is_error"] = event.get("is_error", False)
            diag["result_api_error_status"] = event.get("api_error_status")
            diag["result_total_cost_usd"] = event.get("total_cost_usd")
            continue

        if event.get("type") != "assistant":
            continue

        # Sub-agent boundary: parent_tool_use_id is None for top-level direct calls.
        # Delegated calls have parent_tool_use_id set (e.g. spawned via Agent tool).
        is_delegated = event.get("parent_tool_use_id") is not None

        message = event.get("message", {})
        content = message.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "Skill":
                continue

            input_obj = block.get("input", {})
            skill_name = input_obj.get("skill")
            if not skill_name:
                continue  # malformed skill block

            call_record = {
                "skill_name": skill_name,
                "tool_use_id": block.get("id"),
                "caller_type": block.get("caller", {}).get("type"),  # e.g. "direct"
                "args": input_obj.get("args"),  # often None
            }
            if is_delegated:
                delegated_calls.append(call_record)
            else:
                top_level_calls.append(call_record)

    diag["delegated_skill_calls"] = delegated_calls
    if diag["total_nonempty_lines"] > 0:
        diag["parse_error_rate"] = diag["json_decode_errors"] / diag["total_nonempty_lines"]

    return top_level_calls, diag
```

### 2.3 Normalization (sketch v2 §10 three-auditor #16: skill name case/separator)

```python
def normalize_skill_name(name: str) -> str:
    """
    Normalize for comparison against expected_skill in fixture.
    - lowercase
    - underscore → hyphen
    - whitespace → hyphen
    - strip leading/trailing whitespace + punctuation
    """
    if not name:
        return ""
    return (
        name.strip()
        .lower()
        .replace("_", "-")
        .replace(" ", "-")
    )

# Example: "AQG_Startup_Preflight" → "aqg-startup-preflight"
# Example: "aqg startup preflight" → "aqg-startup-preflight"
```

## 3. Failure-mode disambiguation — ordered decision tree (sketch v2 §4.3 status enum)

**Order matters** (audit `717918e8` finding #3): several statuses overlap, so they must be decided in a fixed order to guarantee AUTH_ERROR / OVER_TRIGGER etc. are not swallowed by INFRA_ERROR / BEHAVIOR_PASS.

```
def decide_status(diagnostics, top_level_calls, expected, allowed_extras):
    # 1. Schema/parse class (JSONL itself broken)
    if diagnostics["parse_error_rate"] > 0.05:
        return PARSE_ERROR  # JSONL lines cannot be parsed
    if not diagnostics["init_event_present"]:
        return SCHEMA_ERROR  # no init event is a schema-breakage signal

    # 2. Schema version mismatch (canary fixture check, see §7)
    if not canary_skill_in_canary_run():
        return SCHEMA_VERSION_MISMATCH  # extractor cannot find a known Skill = upstream schema drift

    # 3. Config / infra (init present but AQG skills not loaded)
    if expected and expected not in (diagnostics["init_skills_loaded"] or []):
        return INFRA_ERROR  # AQG skill not installed into ~/.claude/skills

    # 4. Auth — must come before INFRA_ERROR (when api_error_status indicates auth)
    if diagnostics["result_api_error_status"] in {"unauthorized", "invalid_api_key", "auth_failed"}:
        return AUTH_ERROR

    # 5. Cost guard
    if diagnostics["result_total_cost_usd"] is not None and \
       diagnostics["result_total_cost_usd"] > PER_RUN_BUDGET_USD:
        return COST_EXCEEDED

    # 6. Timeout / infra fallback (no result event, or result is_error not auth/cost)
    if not diagnostics["result_event_present"]:
        return INFRA_ERROR  # subprocess did not complete normally (timeout / crash / max-turns cutoff)
    if diagnostics["result_is_error"]:
        return INFRA_ERROR

    # 7. Behavior decision (result normal + into gating)
    aqg_skills_called = {c["skill_name"] for c in top_level_calls if c["skill_name"].startswith("aqg-")}

    # 7a. Negative case (expected_skills empty, should not trigger any AQG skill)
    if not expected:
        if not aqg_skills_called:
            return BEHAVIOR_PASS  # negative correctly no-trigger
        else:
            return OVER_TRIGGER  # negative case should have no AQG skill but did

    # 7b. Positive case
    if expected not in aqg_skills_called:
        return BEHAVIOR_FAIL  # expected did not trigger
    unexpected = aqg_skills_called - {expected} - set(allowed_extras)
    if unexpected:
        return OVER_TRIGGER  # expected triggered but so did other unauthorized AQG skills
    return BEHAVIOR_PASS

# Sub-agent delegated does not enter gating, but is surfaced to the maintainer:
# delegated_skill_calls is in diagnostics; a long-term share > 0 → evaluate whether to disable sub-agents (Q-Spike-4)
```

**Status enum (10 items, aligned with sketch v2 §4.3 + adding SCHEMA_VERSION_MISMATCH / SCHEMA_ERROR / DELEGATED_TRIGGER)**:

| Status | Meaning | Gating |
|---|---|---|
| `BEHAVIOR_PASS` | expected triggered + no unauthorized AQG skill | counts as PASS |
| `BEHAVIOR_FAIL` | positive: expected did not trigger; negative: actually triggered but unexpected | counts as FAIL |
| `OVER_TRIGGER` | positive: expected triggered + other AQG skills also triggered; negative: any AQG skill triggered | counts as FAIL |
| `INFRA_ERROR` | subprocess anomaly (timeout / crash / max-turns cutoff / AQG not installed) | excluded from gating |
| `PARSE_ERROR` | JSONL >5% of lines cannot be parsed (actual stream corruption) | excluded; version-bump alert |
| `SCHEMA_ERROR` | JSONL parses but is missing a key event (e.g. init missing) | excluded; version-bump alert |
| `SCHEMA_VERSION_MISMATCH` | canary fixture failed — upstream silent schema drift | excluded; version-bump alert |
| `AUTH_ERROR` | provider auth failed (CLI auth expired) | excluded |
| `COST_EXCEEDED` | per-run cost over guard | excluded; alert maintainer |
| `MODEL_UNAVAILABLE` | provider rate-limited / model deprecated | excluded (transient) |
| `DELEGATED_TRIGGER` | only sub-agent delegated triggered expected (top-level did not) | excluded; surface to maintainer (Q-Spike-4) |
| `SKIPPED` | explicit skip / dependency not met | excluded |

## 4. spike 1 vs spike 2 comparison (trigger condition matters)

### Spike 1: plan mode + empty cwd → 0 skill triggered
- `--permission-mode plan`
- cwd: `/private/tmp/aqg-spike-pr0` (empty directory, not a git repo)
- no `--add-dir`
- result: Claude directly spawned an Explore subagent + Bash + Read; **0 Skill calls**
- cost: $0.249, 4 turns, 42s

### Spike 2: default mode + AQG repo via --add-dir → ✓ skill triggered
- (no `--permission-mode`, default = ask)
- cwd: `/private/tmp/aqg-spike-pr0`
- `--add-dir /Users/example/.local/share/aqg/agent-quality-gates`
- result: Claude called `Skill { name: "aqg-startup-preflight" }` + Bash + Read; **1 Skill call** ✓
- cost: $0.231, 11 turns, 56s

### Hypotheses (NOT yet confirmed via ablation — audit `717918e8` finding #5)

⚠️ Spike 1 and spike 2 changed 4 variables at once (permission-mode + implicit cwd + --add-dir + the actual turn path), so single-variable attribution is not possible. The following 4 items are hypotheses awaiting the ablation matrix to confirm:

1. **H1**: `--permission-mode plan` may interfere with the trigger (forced read-only → biased toward an Explore subagent)
2. **H2**: an empty cwd may make the model feel "there is no project to preflight"
3. **H3**: `--add-dir` providing CLAUDE.md context may let the model know AQG is relevant
4. **H4**: the trigger condition is a combination of prompt + cwd + permission-mode + context (a single variable is not visible)

### Required ablation matrix (Q-Spike-7, must run before v0 implementation)

```
            | empty cwd | repo cwd
plan mode   |  spike 1  |    A
default     |     B     | spike 2
+--add-dir  |     C     |    D
+--bare     |     E     |    F
```

Running 6 spikes (A-F) is required to distinguish: plan-mode-only effect / cwd-only effect / add-dir-only effect / bare-mode effect — only then can a credible baseline config recommendation be given. The current spike 1+2 only confirm "the spike 2 config works", not "other configs don't work".

## 5. Cost data (sketch v2 §4.6 cost model correction)

### Sketch v2 estimate (caching ON, 60 case × 5 runs × 3 turns)
- $1.80/nightly = $657/year (assuming 5000 token tool schema + 200 token user prompt + 1500 token response × 3 turns)

### Spike measured (sonnet-4-6, no `--bare`, full plugin context)
- Spike 1: $0.249/run (4 turns, plan mode + Explore subagent overhead)
- Spike 2: $0.231/run (11 turns, default mode + Skill invocation + thorough preflight)
- Average ≈ $0.24/run

### Re-projection (spike measured → v0 estimate)

| scenario | per-run | nightly (60 case × 5 runs) | yearly |
|---|---|---|---|
| sketch v2 estimate (caching ON, no Explore overhead) | $0.003 | $0.90 | $329 |
| **spike measured** (cold context + thorough turns) | $0.24 | **$72/nightly** | **$26k/year** ⚠️ |
| spike measured with `--bare` (TBD verify, Q-Spike-1) | TBD | TBD | TBD |
| with N=1 run/case (sample size 60 only) | $0.24 | $14.40 | $5256 |

**⚠️ Key finding**: measured cost is far higher than the sketch v2 estimate — **80x** ($0.24 / $0.003 = 80; $72 / $0.90 = 80; $26,280 / $329 ≈ 80x). The main cost drivers:
- (a) cache_creation 32k tokens (the sub session loads 150 skills + 33 agents + 219 slash commands + 7 MCP servers + 91 tools — the entire system prompt context)
- (b) once the skill actually triggers, running the preflight (Bash + Read + multiple turns) is more complex than a single trigger-detect
- (c) `--include-partial-messages` increases the stream-event count (but does not itself cost money)

**Must verify during v0 implementation**:
1. whether `--bare` mode reduces system prompt context (skips hook + memory + CLAUDE.md auto-discovery — may save 50%+)
2. whether `--system-prompt` can be used to explicitly minimize (only load the AQG 4 skill descriptions, not everything-claude-code's 100+ skills)
3. lowering `max_turns` (e.g. detect the trigger within 2 turns and stop, rather than letting the skill run to completion)
4. cache reuse from the second case onward — measure amortized cost at N=10 cases

**Short-term measures (before the v0 implementation PR)**: the sketch v2 cost-guard numbers + Q-Open-7 threshold need updating with real cost data; it may be necessary to lower the sample size (60 → 20-30) or reduce runs/case (5 → 3) to stay within budget.

## 6. Sub-process configuration recommendations (for v0 implementation)

### 6.1 Baseline (spike 2 known to work, audit `717918e8` finding #1: do not use an unverified flag in the baseline)

```bash
claude -p "<trigger prompt>" \
  --output-format stream-json \
  --no-session-persistence \
  --max-budget-usd 0.50 \                        # per-run cost guard
  --add-dir <fixture_aqg_clone> \                # sanitized AQG fixture, NOT real repo
  --permission-mode auto \                       # NOT plan (spike H1 hypothesis); auto avoids ask blocking
  --model claude-sonnet-4-6                      # allowlist (Q3 #10 §9 provider boundary)
```

### 6.2 Experimental flags (add only after Q-Spike-1/2 ablation)

⚠️ **Do not use in the v0 baseline** — these flags may disable the very path under test (`--bare` skips skill auto-discovery; `--system-prompt-file` replaces the default prompt containing skill descriptions). Spike-verify first that AQG skills still load + still trigger, then decide whether to add them to the baseline.

```bash
# Q-Spike-1: --bare reduces context, but do AQG skills still load?
--bare

# Q-Spike-2: does a custom minimal system prompt still trigger AQG skills?
--system-prompt-file <minimal_system_prompt>

# performance/isolation flags (relatively safe, but still need spike verification)
--no-include-hook-events                         # test isolation (hook side effects)
--no-include-partial-messages                    # reduce stream-event noise (extractor does not need it)
```

**isolated workspace** (sketch v2 §9.4 workspace_isolator.py):
```bash
ISOLATED_WORKSPACE=$(mktemp -d /tmp/aqg-behavior-isolated.XXXXXX)
cp -r <fixture_aqg_clone> "$ISOLATED_WORKSPACE/"
cd "$ISOLATED_WORKSPACE"
chmod -R a-w "$ISOLATED_WORKSPACE/aqg_clone"  # read-only mount
# run claude -p with --add-dir "$ISOLATED_WORKSPACE/aqg_clone"
```

## 7. Schema versioning + maintenance protocol

### Canary fixture (audit `717918e8` finding #6: guard against silent schema drift)

Stream-json may be syntactically valid but semantically changed — the extractor returning 0 skills cannot distinguish "the model genuinely did not trigger" vs "Skill activation changed and is no longer in tool_use". A **canary fixture** is needed to actively check:

- `tests/behavior/fixtures/canary/known_skill_call.jsonl` — a historical stream-json known to contain a Skill call (extracted from spike 2)
- the nightly run starts by running the extractor against the canary → should return 1 known skill call
- if the canary returns 0 → `SCHEMA_VERSION_MISMATCH` (alert the maintainer; upstream silent drift)
- the canary fixture is updated together with each schema version bump

### Bump schema version when
- Claude Code stream-json event types change (add/remove/rename a top-level type)
- `assistant.message.content[].type == "tool_use"` is replaced or the nested form changes
- Skill activation no longer goes through the `Skill` tool name (e.g. changed to native invocation)
- the `result` event field schema changes (cost / usage / api_error_status renamed)
- the `parent_tool_use_id` field semantics change (sub-agent boundary detection breaks)

### Schema bump procedure
1. Create `tests/behavior/fixtures/stream_json_schema_v<N+1>.md`
2. Document the diff (new field / deprecated / breaking change)
3. Add to the extractor `if SCHEMA_VERSION == N: <v_N path>; elif SCHEMA_VERSION == N+1: <new path>`
4. Add `expected_schema_version: <N+1>` to the fixture YAML; an old fixture then explicitly fails with `SCHEMA_VERSION_MISMATCH` (not `PARSE_ERROR` — the JSONL parsed fine)
5. Update the canary fixture in sync at `tests/behavior/fixtures/canary/`
6. PR commit + single-auditor review

### When Claude Code is upgraded (silent-drift catch)
1. the nightly canary runs → fails → maintainer alert
2. run 1 new spike with the same prompt as the canary → diff the new JSONL vs the canary → find the schema change point
3. follow the schema bump procedure

## 8. Open questions (must resolve before v0 implementation)

| ID | question | impact |
|---|---|---|
| Q-Spike-1 | Does `--bare` mode still load AQG skills + actually fire the trigger? | key to cost optimization |
| Q-Spike-2 | Can a custom minimal `--system-prompt` strip everything-claude-code's 100+ skills but keep the AQG 4 skills? | key to cost + signal-to-noise |
| Q-Spike-3 | After a sub-process Skill call, does the skill run to completion (e.g. does preflight actually run aqg_preflight.py)? Or does it only invoke, then abort at max_turns? | affects per-run cost + isolation |
| Q-Spike-4 | Spike 1's sub session spawned an Explore subagent — does the Q3 #10 test need to disable sub-agents (--no-agents flag)? How is a sub-agent's stream-json `parent_tool_use_id` set? Is the extractor's `is_delegated` judgment accurate? | trigger interference + double cost + extractor correctness |
| Q-Spike-5 | spike 2 cost $0.24/run × 60 case × 5 runs = $72/nightly = **80x** the sketch v2 estimate — sample size / runs must be redesigned or the budget blows up | sketch v3 correction |
| Q-Spike-6 | The Skill call's caller.type is currently `"direct"` — what other caller types exist? Can we get the distinction between `"auto-invoked"` (description match) and `"explicit"` (slash command)? | trigger-authenticity check |
| **Q-Spike-7** (new, audit `717918e8` finding #5) | **Ablation matrix, 6 spikes** (default vs plan × empty cwd vs repo cwd × +/- --add-dir × +/- --bare) — distinguish single-variable effects, give a credible baseline | §4 hypothesis → fact conversion |
| **Q-Spike-8** (new, audit `717918e8` finding #9) | **Subprocess termination shapes**: run wall-clock timeout / `--max-turns` cutoff / `--max-budget-usd` exceeded / auth fail / permission denial scenarios, collecting (a) exit code (b) stderr (c) result event shape — prevent v0 from conflating these fail modes into INFRA_ERROR | §3 status enum accuracy |

These 8 items each need a spike + documentation expansion, or to be folded into sketch v3, before the v0 implementation PR.

## 9. Audit Trail

- 2026-05-03 sketch v2 §2 mandates the PR-0 spike (three-auditor audit_id `53e08672` finding #10 motivation)
- 2026-05-03 spike 1: plan mode + empty cwd → 0 Skill triggered (overturns the trigger hypothesis)
- 2026-05-03 spike 2: default mode + `--add-dir AQG repo` → ✓ 1 Skill triggered (`aqg-startup-preflight`); schema shape confirmed
- 2026-05-03 this schema doc v1 drafted (this PR-0)
- 2026-05-03 commit-time single-auditor review audit_id `717918e8` (gpt-5.5, 3m15s) — 9 issues all accepted (6 major + 3 minor): extractor adds sub-agent boundary + diagnostics tuple / status enum changed to an ordered decision tree + added SCHEMA_VERSION_MISMATCH/SCHEMA_ERROR/DELEGATED_TRIGGER + negative-case rules / cost math 80x consistent / §4 lessons changed to hypotheses + ablation matrix outline / §6 baseline removed unverified --bare/--system-prompt-file / §7 added canary fixture / Q-Spike +2 (ablation Q-Spike-7, termination shapes Q-Spike-8); fix applied
- TBD: follow-up spikes for Q-Spike-1~8 + documentation expansion / sketch v3 cost redesign
- TBD: v0 implementation PR (extractor + runner + fixture YAML + CI)
