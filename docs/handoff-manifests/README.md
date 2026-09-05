# Handoff Manifests

A collection directory for cross-agent handoff manifests, per `docs/ENGINEERING_FRAMEWORK.md`
v2.1 §4 (sub-agent contract) + §10 (MPLP L2-inspired naming).

## Purpose

When a sub-agent (Codex / Claude sub-task) hands high-stakes work back to the main agent,
it should attach a manifest describing key evidence: actor / commands / CI run / artifact / trace.
The main agent validates schema + redaction with `scripts/validate_handoff_manifest.py`, and in
high-stakes mode also runs a triple `gh api` cross-check (to guard against a hallucinated run_id).

## CI validation

`.github/workflows/handoff-manifest-warn.yml` scans this directory on PRs for
`*.json` / `*.yaml` / `*.yml` and runs `validate_handoff_manifest --no-remote-check` on each file.

- **warn-only**: failures are warning annotations only, they do not block PR merge
- **no remote cross-check**: in a CI context the high-stakes cross-check's
  expected_commit_sha / expected_workflow_name cannot be determined statically at PR-time,
  so that is left for the main agent to do locally at handoff time

## File naming

- Real handoff records: `<date>-<task-slug>.json`
  e.g. `2026-05-03-wave1-redaction-handoff.json`
- Examples / docs: `_example_*.json` (the `_` prefix marks a non-real record)
  CI also validates these files (to ensure the happy path runs in CI), but a human reading them
  can immediately tell "this is a doc, not real evidence"

## Schema overview

See the docstring at the top of `scripts/validate_handoff_manifest.py` + `_make_clean_manifest()`.

### v1 (original schema, forward-compat)

No `schema_version` field (or `schema_version: 1`), no `aqg_review` field.

Required nodes:
- `aqg_context` — actor / parent_session_id / task
- `aqg_confirm` — env_fingerprint must run redaction; more fields required when high-stakes
- `aqg_trace` — exit_code / result (pass | warn | fail)

Optional node:
- `aqg_plan` — intended_action / scope

### v2 (2026-05-04 ADR — sub-agent review fields)

`schema_version: 2` + a new optional top-level node `aqg_review` that raises the sub-agent
review flow from prompt-level to schema-level verifiable fields
(inspired by superpowers-zh `subagent-driven-development`).

```yaml
schema_version: 2
aqg_review:
  implementation:
    status: pending|in_progress|done|blocked
    implementer_model: <str, required when status != pending>
    commit_shas: ["<7-40 hex>", ...]
  spec_review:
    status: pending|passed|findings|re_review
    reviewer_model: <str, required when status != pending>
    findings_count: <int >= 0>
    loop_iter: <int >= 0>
  quality_review:
    status: pending|passed|findings|re_review
    reviewer_model / findings_count / loop_iter: same shape
  review_loop_count: <int >= 0>
  accepted_findings: ["<finding-id>", ...]
  fixed_before_next_task: true|false
```

**6 invariants** enforced when `aqg_review` present:

1. `quality_review.status != pending` requires `spec_review.status == passed`
2. `implementation.status == done` requires `commit_shas` non-empty
3. `review_loop_count >= max(spec_review.loop_iter, quality_review.loop_iter)`
4. `fixed_before_next_task` field must be explicit (true/false; not missing)
5. `spec_review.status != pending` requires `implementation.status == done` + non-empty `commit_shas`
6. `aqg_trace.result == "pass"` AND `accepted_findings` non-empty → `fixed_before_next_task` must be `true` (final handoff cannot leave findings unfixed)

**Version-aqg_review lock-in**:

| `schema_version` | `aqg_review` | Result |
|---|---|---|
| absent or `1` | absent | ✅ valid v1 (legacy) |
| `2` | present | ✅ valid v2 (full review validation) |
| `2` | absent | ✅ valid v2 (review optional) |
| absent or `1` | **present** | ❌ fail — `aqg_review` requires `schema_version: 2` |
| any | unknown `aqg_*` typo (e.g. `aqg_reviwe`) | ❌ fail — typo guard |

**v2's new fields are optional**: not every v2 manifest is required to contain `aqg_review`. A trivial sub-agent task can skip it.

Reference sample: `_example_clean_v2.json`.

## Boundary

- A manifest is not a secrets store — `env_fingerprint` must pass the
  `_surface_redaction` gate (which rejects any field that looks like a token / key / secret)
- Do not stuff real customer / production data into a manifest
- This directory is git-tracked + PR-visible — assume anything placed here is public
