# AQG de_audit — interface contract (contracts/audit/)

> **What this is**: the **executable interface contract** for `de_audit`, the
> external cross-vendor audit that AQG's skills route to (the `aqg-code-construction`
> audit-before-commit gate, `aqg-multi-review`, `aqg-security-review`). It ships the
> **request/response event shape + a non-implementing stub** so an adopter knows
> exactly what to call and what comes back.
>
> **What this deliberately is NOT**: the engine. Vendor selection / per-mode
> routing, panel-convergence weighting, and any `ground_truth` are **engine-internal**
> (the external cloud decision-engine) and are intentionally **absent** from this
> MIT-public package. Convergence adjudication — weighing agreeing vs diverging
> voices into a verdict — is the **caller's** job, not a field in this contract.
> (This boundary is additionally guarded at release time by WS-0 / G0-④.)

## Files

| File | Role | Ships the moat? |
|---|---|---|
| `audit-event.schema.json` | JSON Schema (draft-07) for `AuditRequest` + `AuditResponse` — the machine-readable contract doc | No — interface shape only |
| `conformance.py` | **Pure stdlib** validator `validate_audit_event(event) -> (ok, errors)`, mirrors the schema, accumulates all errors, never raises | No |
| `stub.py` | `de_audit(...)` interface seam: validates the request, then raises `EngineNotAvailable` (never fabricates a verdict) + `describe_contract()` moat-safe metadata | No — the seam, not the engine |
| `fixtures/valid\|invalid/*.json` | golden samples covering every constraint | No |

Tests: `tests/behavior/test_audit_contract.py` (runs in the standard behavior CI step; includes a **moat guard** asserting no vendor/model identifier leaks into the shipped contract).

## The boundary, concretely

**Shipped here (interface):**
- `AuditRequest` — `artifact` (material under review only), `context`, `focus`,
  `mode` ∈ {fast, standard, deep}, `stakes`, `caller_family`.
- `AuditResponse` — `audit_id`, `status`, `mode_effective`, `panel` (opaque voice
  labels), `verdicts[]` (per-voice `overall_verdict` + `findings[]`), `note`.

**NOT shipped (engine-internal moat):**
- Which auditor vendors/models a `mode` maps to (routing).
- How panel agreement becomes a confidence signal (convergence weighting).
- Any `ground_truth` / calibration data.

The `panel` and `verdicts` carry **opaque labels + free-text findings** — the
shape a caller renders and adjudicates. There is **no convergence score** in the
response: the caller (the adjudicating session) weighs convergent vs divergent
voices itself. That is by design, not an omission.

## How an engine wires in

`de_audit` is a **seam**. Out of the box it validates the request and raises
`EngineNotAvailable`:

```python
import sys; sys.path.insert(0, "<aqg_repo>/contracts")   # or install as a package
from audit.stub import de_audit, EngineNotAvailable
from audit.conformance import validate_audit_event

try:
    result = de_audit(artifact=diff, context="intent + acceptance", mode="standard")
except EngineNotAvailable:
    # No engine wired. Either point de_audit at your engine (below), or run
    # `aqg-multi-review new --fallback-session-llm` for a single-model degraded eval.
    ...
```

An adopter supplies the engine behind the seam (an MCP server, a cloud endpoint,
or a local panel) that:
1. accepts an `AuditRequest`-shaped payload (validate it with `validate_audit_event`);
2. runs whatever routing/convergence it owns — **that logic is yours, not this repo's**;
3. returns an `AuditResponse`-shaped payload (validate it before returning).

The AQG↔engine coupling surface is **this contract alone**. The reference engine
is the sibling **decision-engine** (EAF); its integration is documented as a
*method* ("wire an `AuditRequest` in, return an `AuditResponse`"), not shipped as
implementation here.

## No engine? Use the labeled degraded fallback

When no engine is wired, `aqg-multi-review new --fallback-session-llm` lets the
current session model do a **single-model rough self-eval** — machine-labeled
`single-model / same-vendor / non-independent / rough-eval-only` so it can never
be presented as a cross-vendor panel. See `skills/aqg-multi-review/SKILL.md`.

## Contract invariants (do not violate)

- `artifact` is **material only** — never fold review instructions / framing into
  it (they belong in `context` / `focus`).
- The response `panel` / `verdicts` labels are **opaque** — do not read vendor
  identity or routing into them.
- The contract carries **no convergence score / ground_truth** — adjudication is
  the caller's.
- Contract changes require **mutual agreement with the engine side** — bump
  `schema_version`, sync both sides; never change silently.
