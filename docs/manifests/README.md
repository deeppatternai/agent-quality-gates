# AQG Manifest Family

AQG ships **schema + validator** for three manifest families. AQG validates structure + redaction; **never executes** the simulation / orchestration / handoff itself (project owns the actual harness / spawn).

| Manifest family | Schema location | Validator | Sample |
|---|---|---|---|
| **handoff** (sub-agent handoff evidence, Wave 1-0) | `scripts/_secret_patterns.py` + builtin in `validate_handoff_manifest.py` | `scripts/validate_handoff_manifest.py` | `docs/handoff-manifests/_example_clean.json` |
| **simulation** (synthetic chaos / failure / latency, Wave 2 #4) | `scripts/_simulation_redaction.py` | `scripts/validate_simulation_manifest.py` | `docs/manifests/simulation/_example_clean.json` |
| **orchestration** (multi-step LLM workflow, Wave 2 #5) | `scripts/_orchestration_redaction.py` | `scripts/validate_orchestration_manifest.py` | `docs/manifests/orchestration/_example_clean.json` |

## Boundary

- **AQG own**: schema definition + redaction guard + CLI validator + sample reference manifests
- **Project own**: actual harness (simulation runner, orchestration spawn, handoff actor)
- AQG **never** runs simulations, spawns agents, or executes handoffs — only validates that the manifest description meets the schema and contains no secret/path/PII leak

## Common pattern

All three families:
- JSON manifest file (one record per file)
- `schema_version: 1` with forward-compat unknown-version skip
- Closed actor enum (claude / codex / gpt-5.5 / gemini / o3 / human / ci-bot / other)
- `boundaries` field documenting deliberate non-actions
- Strict redaction: no raw secrets / paths / customer data
- Sample manifest in `_example_*.json` to bootstrap and assert validate-path works

## Adding a new manifest family

1. Write `_<family>_redaction.py` with allowlist + secret pattern reuse via `_secret_patterns.secret_counts()`
2. Write `validate_<family>_manifest.py` CLI (mirror existing validators)
3. Add `_example_clean.json` under `docs/manifests/<family>/`
4. Add tests covering schema + validator + sample
5. Add to `aqg_doctor.py` CRITICAL_SCRIPTS
6. Update this README
7. (Optional) Add CI workflow scanning `docs/manifests/<family>/*.json`

## Validators are read-only

Validators print `OK <name> valid` to stdout when manifest passes; print `FAIL <name> invalid` + violation list to stderr otherwise. Exit 0/1/2 (valid/invalid/usage). They never mutate the manifest or write side effects.
