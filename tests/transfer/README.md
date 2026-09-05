# Transfer Test Pack v1

Operationalizes the §1.2 北极星 invariant ("换 Owner / 换 Claude / 换 Codex / 换上下文") via a fixed task set + per-task pass/fail metrics + strict-all-hard threshold.


## Quick start

```bash
# Run all 6 tasks, emit run summary YAML to stdout
python3 tests/transfer/runner.py

# Subset (Tasks 1-4 only; missing tasks emit fail placeholders)
python3 tests/transfer/runner.py --tasks 1,2,3,4

# Skip Task 5 entirely (advisory; CI uses this when audit-mcp unavailable)
python3 tests/transfer/runner.py --skip-task5

# JSON output + write to file
python3 tests/transfer/runner.py --json --output run_summary.json

# A2 — also emit an aqg_metrics record summarizing this run (tool=transfer);
# provenance.metric_event_id will be set to "transfer-<run_id>".
python3 tests/transfer/runner.py --record-metric --actor ci-bot

# A2 — on threshold_met=false, write an incident record to docs/incidents/;
# provenance.incident_event_id will be set to "<date>-transfer-pack-fail-...".
python3 tests/transfer/runner.py --raise-incident --actor ci-bot

# Validate an existing record / summary file against §4 schema
python3 tests/transfer/validate_transfer_record_v1.py path/to/record.yaml --kind task
python3 tests/transfer/validate_transfer_record_v1.py path/to/summary.yaml --kind summary
```

Exit code 0 = `threshold_met=true` (all hard tasks pass + 0 boundary violations + Task 5 advisory != fail).

## The 6 tasks (sketch §3)

| id | name | gate | what |
|---|---|---|---|
| 1 | `fresh_checkout` | hard | install.sh + aqg_doctor.py: exit 0 + 0 WARN/FAIL + PASS ≥ 40 floor |
| 2 | `handoff_schema` | hard | validate_handoff_manifest.py against frozen v1/v2 fixtures, hermetic mode |
| 3 | `cross_env` | hard | aqg_chaos.py run-all (curated chaos scenarios) |
| 4 | `behavior_trigger` | hard | 5 pinned case_ids from triggers.yaml (4 desc + 1 explicit ≈ 70/30) drift-hash match |
| 5 | `audit_reproducibility` | **advisory** | gpt_audit verdict ⊇ golden expected canonical findings (warn-only unless fail) |
| 6 | `owner_cold_transfer` | hard | extract `next_action` + `blockers[]` from frontmatter; equality vs Owner-curated golden |

## Threshold (§5.4)

```
threshold_met = (strict_pass_rate == 1.0) AND (boundary_violations == 0)
                AND (task5_advisory != fail)
```

- Tasks 1, 2, 3, 4, 6 = strict-all-pass (no `warn` allowed)
- Task 5 = advisory (`warn` OK; `fail` blocks because gross audit failure is itself a transfer signal)
- `boundary_violations == 0` is zero-tolerance: production write / branch protection bypass / secret leak / Owner-only action without statement

## Schema v2 — explicit `skipped: bool` field (B1)

Task records carry an optional `skipped: bool` field that decouples the "audit ran" semantic from the `result` enum:

- `skipped: true` is the canonical advisory-skip path: only valid when `task_id == 5` AND `result == "warn"`. Provenance `task5_model_id` / `task5_provider` are NOT required (audit didn't run). The Task 5 module sets this explicitly; the runner copies the value without inference.
- `skipped: false` means the audit really ran. For Task 5 in this case, provenance `task5_model_id` + `task5_provider` ARE required, even when `result == "warn"` (a real warning from minor findings).

Why: pre-B1 (schema v1) conflated "audit ran with only minor findings" (real warn) with "advisory-skip because no result file" (skipped warn). With v2, a real warning emits `skipped: false` + populated provenance; an advisory skip emits `skipped: true` + null provenance.

**Schema version bump (v1 → v2)** per audit 73619b3e #1: any old strict v1 consumer that rejects unknown fields will now early-detect that records carry the new field and need an upgrade. The validator accepts both v1 and v2 — pre-B1 records still validate via the implicit `result in {pass, fail}` fallback inference.

**Cross-field invariant** per audit #3: summary `task5_advisory` MUST match the Task 5 record's `result`. Drift between summary and per-task list is rejected by the validator.

## Gating model (A3 — structural fix per audit 297dccac gpt #3)

GHA tag-push triggers run AFTER the tag exists, so they cannot retroactively block a release. The structural fix is two layers:

### Layer 1 — pre-merge `pull_request` gate (the real block)

GHA runs `transfer-test-pack.yml` on **every PR** to `main` and **fails the check on threshold-not-met**. Owner sets this as a **required check** in repo branch protection settings — at that point the PR cannot be merged until the pack passes.

This is the canonical "release block": a release tag is created from `main` HEAD, and `main` HEAD only ever advances after a PR with a passing transfer pack. Tag time is then a foregone conclusion.

**No `paths:` filter on the PR trigger** (audit b2211ad2 #1): GitHub treats path-skipped required checks as stuck-Pending, which would block unrelated PRs. The trade-off is ~3-5 min CI cost per PR; acceptable until cost becomes a real bottleneck. Optimize later via the dorny/paths-filter pattern (lightweight always-run gate job + heavy pack on relevant paths).

### Layer 2 — `workflow_call` reusable from release/publish workflow

A future `release.yml` (or any publish workflow) can include:

```yaml
jobs:
  transfer-pack:
    uses: ./.github/workflows/transfer-test-pack.yml
    with:
      is_release_callee: true   # CRITICAL: this is how the callee detects callee-context
      skip_task5: 'true'
  publish:
    needs: transfer-pack
    if: success()
    runs-on: ubuntu-latest
    steps:
      - …  # actual publish step (npm, PyPI, GitHub Release …)
```

The `needs: transfer-pack` + `if: success()` combo blocks the publish step on threshold failure. The pack runs in `posture=callee` (because `inputs.is_release_callee == true`) and surfaces the threshold-not-met as a job failure to the caller. **This is how a release publish gets blocked.**

**Why `is_release_callee` instead of detecting via `github.event_name == 'workflow_call'`** (audit b2211ad2 #2): inside a reusable workflow, `github.event_name` reflects the **caller's** trigger (e.g. `workflow_dispatch` from the release.yml side), not `workflow_call`. The explicit input is the only reliable way to tell the callee it's running in callee context.

### Other triggers (unchanged)

- **Nightly warn-only**: GHA cron (UTC 13:00) runs the pack with `posture=warn` — annotates but never fails the workflow. Owner toggle: repo variable `AQG_TRANSFER_NIGHTLY_ENABLED=true`.
- **Tag push (`v*`)**: still runs as a visibility signal in `posture=tag_visibility`. Failure annotates loudly but is **not** a real block (the tag already exists). Layer 1 + Layer 2 are the structural blocks.
- **Owner-manual on-demand**: `gh workflow run transfer-test-pack.yml` — `posture=manual`, exit code is the gate.

CI workflow lives at `.github/workflows/transfer-test-pack.yml` (canonical GHA location, NOT under `tests/transfer/`).

### Operational note

The PR-gate is only a real block once the check is **marked required** in branch protection. Until then it is informational. Owner action: enable "transfer-test-pack / run-pack" as a required status check on the protected branch (`main`).

## Local-vs-CI: Task 1 in a git worktree

If you run the pack from a `git worktree`, Task 1 may **fail** because `aqg_doctor.py` finds that `~/.claude/skills/aqg-*` and `~/.codex/skills/aqg-*` symlinks resolve into the **main repo path**, not into your worktree. Doctor reports those as `WARN` and Task 1 enforces 0 WARN.

This is the correct behavior for the pack's purpose: a fresh checkout (CI runner, new operator workstation) installs symlinks pointing to its own checkout, and doctor reports 0 WARN. Worktree dev installations are intentionally not the ground truth — run the pack against the canonical checkout (or CI) for reliable Task 1 results.

```bash
# CI / fresh-checkout posture (canonical):
python3 tests/transfer/runner.py
# threshold_met: true (assuming Tasks 2-6 fixtures present + audit not regressed)

# Worktree dev posture (Task 1 expected fail):
python3 tests/transfer/runner.py
# threshold_met: false; Task 1 fail 3/4; Tasks 2-6 still validate the rest of infra
```

## Task 5 — providing an audit result

The runner does NOT call `gpt_audit` directly (audit-mcp lives outside the test process). To exercise Task 5, place a saved audit result at:

```
tests/transfer/_task5_run_result.json
```

The file MUST embed a `bound_to` block to prevent stale-result false-pass (audit 297dccac gpt #2):

```jsonc
{
  "bound_to": {
    "artifact_sha256": "<sha256 of fixtures/audit_repro_artifact.md at audit time>",
    "fixture_version": "v1.0.0"
  },
  "issues": [
    { "issue": "...", "severity": "..." }
  ]
}
```

Task 5 recomputes the artifact sha256 at runtime and rejects the result file unless `bound_to.artifact_sha256` matches and `bound_to.fixture_version` agrees with the golden file's `fixture_version`. Without the file at all, Task 5 returns `warn` and threshold is not blocked. With a bound result file, Task 5 compares findings' canonical keys against `fixtures/audit_repro_golden.yaml`.

## End-to-end self-test (post-merge / fresh-checkout posture)

Audit 297dccac gpt #4 flagged that pre-merge verification cannot demonstrate a `threshold_met=true` run from a single checkout (worktree fails Task 1; main repo lacks fixtures until merge). After this PR merges, on a fresh CI runner or a clean operator workstation:

```bash
git clone https://github.com/deeppatternai/agent-quality-gates
cd agent-quality-gates
bash scripts/install.sh --force
python3 tests/transfer/runner.py --skip-task5
# expected: threshold_met: true (Tasks 1-4+6 pass; Task 5 warn = advisory skip)
```

The CI workflow `.github/workflows/transfer-test-pack.yml` exercises exactly this path on every run. Local-worktree dev runs are NOT the ground truth — see "Local-vs-CI" below.

## Fixture freeze

All fixtures are pinned at `fixture_version: v1.0.0`. Any intentional bump must be a separate PR with Owner sign-off + concurrent recompute of:

- `audit_repro_artifact_sha256` in `audit_repro_golden.yaml`
- pinned `case_ids` in Task 4 if `triggers.yaml` rotation changes the curated set
- `owner_transfer_golden.yaml` next_action + blockers if the cold-operator state changes

## Failure handling (§8 Q6, Q7 default)

- Per-task records + summary uploaded as GHA artifact (90d retention)
- `metric_event_id` field in provenance links to `aqg_metrics` ledger
- `incident_event_id` field links to `docs/incidents/<date>-<slug>.md` if failure raises an incident
- Failure adds 7th line to closeout ledger ("Transfer Test Pack run id X — pass rate Y / threshold Z")

## What v1 deliberately does NOT do (§9)

- Live LLM cold-transfer (no chat history → fresh decision) — deferred to v1.1 / Continuity Drill
- Cross-client production (Codex emit → Claude consume in same run) — deferred to v1.1
- 7-day Continuity Drill — separate Wave 3+ work
- Real chaos / docker — `aqg_chaos.py` synthetic mocks only
- Modify existing `tests/behavior/` or `scripts/aqg_chaos.py` infra
