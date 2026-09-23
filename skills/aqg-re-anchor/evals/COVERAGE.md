# aqg-re-anchor Coverage Matrix

Scope: compact emit-only rendering, caller-data sanitization, graceful degradation, and CLI contract.

| Requirement | Behavior | Existing Case | New Case | Oracle | Status | Risk |
|---|---|---|---|---|---|---|
| Preserve goal, gates, and progress | Known structured state is restated | 001, bench-001 | — | structural output fields | COVERED | medium |
| Preserve unknown state | Sparse input does not fabricate gates or progress | 002, bench-002 | — | output facts + absence checks | COVERED | high |
| Minimal valid output | Short payload remains compact and stable | 003 | — | output shape/length | COVERED | low |
| Emit-only/stateless | No audit call, self-injection, or file mutation | 001, 002, bench cases | — | probe hashes and command trace | COVERED | high |
| Graceful malformed fields | Missing/sparse/malformed values do not raise | 002, 003 | — | exit code + output | PARTIAL | high |
| Terminal/semantic injection defense | C0/C1, bidi, line separators, and spoofing text are sanitized | — | 004-sanitization-bounds | byte/character assertions | ADDED | critical |
| Length bounds | Goal, gates, progress count, and labels stay bounded while current beyond the display cap remains visible | — | 004-sanitization-bounds | deterministic bounds and current marker | ADDED | medium |
| CLI JSON mode | `--json` returns an object with a `restatement` string | — | 004-sanitization-bounds | JSON schema | ADDED | medium |
| CLI invalid input contract | Non-object and malformed JSON exit 2 without a traceback | — | 004-sanitization-bounds | exit code/stderr | ADDED | medium |

## Coverage Gaps

- Boundary cadence is caller-owned and cannot be proven by the pure renderer.
- Exact token-budget quality is measured by bounds, not model context effects.

## Case Taxonomy

- Existing cases: `baseline`, `boundary`, `security`, `read-only`.
- `004-sanitization-bounds`: `negative`, `adversarial`, `security`, `environment`, `idempotency`.

## Suspected Defects

None from static audit.
