# WS-8 R3 identity rebind — deep-audit adjudication

- Date: 2026-07-15
- Scope: active WS-8 protocol identity, phase-envelope templates, exact identity test, and Owner runbook guidance.
- Audit: `2cbe2848` (deep)

| finding | decision | action | verification |
|---|---|---|---|
| The active R3 protocol should replace its `predecessor` with the R2 terminal study. | rejected | `predecessor` is the frozen WS-7 baseline manifest, not an attempt-lineage field: the runtime validates its WS-7 artifact paths and hashes. Rebinding it to R2 would change a separate published-baseline contract without a corresponding schema or execution need. | `test_ws8_protocol.py -q` keeps the WS-7 predecessor digest contract green; `ws8.py:267-277` and `ws8.py:643-646` confirm the field semantics. |
| Four phase-envelope templates and the active runbook lack a durable automated R3 consistency guard. | accepted | Add one focused protocol test that parses every authorization template and asserts its study ID equals the active protocol identity, then asserts active R3 instructions and R2 finality wording in the runbook. | `test_r3_identity_is_bound_by_owner_templates_and_runbook` passes in `test_ws8_protocol.py -q`. |
| The R3 runbook cites the R2 redaction-safe terminal record without an automated existence check. | accepted | Extend the same focused test to require the published R2 terminal-evidence file, without reading protected root state or terminal JSON. | `test_r3_identity_is_bound_by_owner_templates_and_runbook` verifies the committed evidence path exists. |

## Boundaries

No authorization envelope, nonce, reservation, root-owned state, credential, raw stream, or paid execution command was read, created, or modified.
