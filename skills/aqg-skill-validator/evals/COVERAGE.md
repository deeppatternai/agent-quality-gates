# aqg-skill-validator Coverage Matrix

Scope: sidecar, frontmatter, boundary, exit-code, registration, and advisory/strict validation.

| Requirement | Behavior | Existing Case | New Case | Oracle | Status | Risk |
|---|---|---|---|---|---|---|
| Valid Skill is accepted | Hermetic legal fixture returns valid | 001-valid | — | structured result + exit 0 | COVERED | high |
| Key structural violations are found | Invalid frontmatter/sidecar/boundary is classified invalid | 002-invalid, bench-001 | — | violation buckets + exit 1 | COVERED | high |
| Validator is read-only | Checked fixture hash remains unchanged | 001-valid, 002-invalid | — | before/after SHA-256 | COVERED | high |
| Cross-cutting registration is checked | Trigger, doctor, install, wrapper, and interface registrations matter | 001-valid, 002-invalid | — | fixture result buckets | PARTIAL | high |
| Exit-code contract is checked | Entry script documents 0/1/2/3/70 | 002-invalid indirectly | 003-contract-and-strict | explicit result evidence | ADDED | high |
| Strict mode promotes warnings | Advisory warning is non-strict but fails under `--strict` | — | 003-contract-and-strict | paired exit codes and warning list | ADDED | medium |
| Missing registration is distinguished | Missing cross-cutting files are reported without mutation | — | 003-contract-and-strict | named violation categories | ADDED | high |
| JSON output remains machine-readable | Valid and invalid results serialize deterministically | 001-valid, 002-invalid | 003-contract-and-strict | JSON parse/schema | COVERED | medium |

## Coverage Gaps

- The validator's policy semantics are represented by deterministic fixture facts; this suite does not benchmark human remediation choices.
- Future reserved exit codes 3/70 are documented but not currently emitted by the validator.

## Case Taxonomy

- Existing cases: `baseline`, `negative`, `read-only`, `integration`.
- `003-contract-and-strict`: `negative`, `adversarial`, `state`, `read-only`.

## Suspected Defects

None from static audit.
