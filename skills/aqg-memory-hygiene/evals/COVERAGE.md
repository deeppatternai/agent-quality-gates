# aqg-memory-hygiene Coverage Matrix

Scope: strict frontmatter validation, staleness signal, pointer integrity, and read-only behavior.

| Requirement | Behavior | Existing Case | New Case | Oracle | Status | Risk |
|---|---|---|---|---|---|---|
| Valid schema passes | Legal nested metadata is accepted | 001 | — | JSON `ok` + exit 0 | COVERED | high |
| Schema defects fail validate | Missing/flat/type/enum/frontmatter errors are surfaced | 002, 004 | — | exit 1 + violation fields | COVERED | high |
| Staleness boundary is deterministic | Exact threshold, over threshold, missing/malformed date are distinguished | 005, bench-001 | — | stale/skipped sets | COVERED | high |
| Body is untrusted data | Injection-like content is not executed | 006, bench-002 | — | no side effect + hash equality | COVERED | critical |
| Read-only invariant | Memory files are never modified | 001, 002, 004-006, benchmarks | — | before/after hashes | COVERED | critical |
| Future dates are invalid | `last_verified` in the future fails schema validation | bench-001 | — | invalid classification | COVERED | high |
| Superseded pointer integrity | Bare slug, null retirement, and broken pointer rules are enforced | — | 007-superseded-pointer | structured violations + read-only hash | ADDED | high |
| Durable/superseded staleness semantics | Durable is never stale; superseded is skipped | 001 partially | 007-superseded-pointer | stale/skipped/fresh sets | ADDED | medium |
| Dependency/empty-dir degradation | Missing memory dir and staleness graceful behavior are explicit | self-test only | — | documented gap | PARTIAL | medium |

## Coverage Gaps

- PyYAML absence is an environment/dependency condition and is not run in the normal regression path.
- Network/audit-mcp non-use is static and process-observable, not a direct network packet assertion.

## Case Taxonomy

- Existing cases: `baseline`, `boundary`, `security`, `read-only`, `benchmark`.
- `007-superseded-pointer`: `negative`, `state`, `read-only`, `idempotency`.

## Suspected Defects

None from static audit.
