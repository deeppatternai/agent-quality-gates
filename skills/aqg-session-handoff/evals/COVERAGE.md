# aqg-session-handoff Coverage Matrix

Scope: `SKILL.md` contract, the `new`/`validate` workflow, and the read-only handoff gate.

| Requirement | Behavior | Existing Case | New Case | Oracle | Status | Risk |
|---|---|---|---|---|---|---|
| `new` emits the full contract | Eight exact section headers are emitted | 001, 003, 004, 005, 007 | — | skeleton artifact + headers | COVERED | low |
| `validate` blocks placeholders | Unfilled `<FILL: ...>` content fails before paste | 005 | — | exit code + JSON `valid` | COVERED | low |
| Required sections must be real | Unknown facts are explicit, not invented | 002, 005 | — | handoff structure + scenario facts | COVERED | medium |
| Secret scan is fail-closed | Synthetic secret-shaped content fails, then safe replacement passes | 002, 007 | — | initial/final JSON and no-leak scan | COVERED | high |
| Minimal mode remains gated | Three-section degraded handoff still validates and scans secrets | 006 | — | minimal headers + real CLI rerun | COVERED | medium |
| Inner fences are forbidden | Handoff body remains safe inside one outer copy fence | 004 | — | body fence scan + final fenced block | COVERED | medium |
| Multi-turn context is retained | Earlier turns contribute to the final handoff | 003 | — | required scenario facts | COVERED | medium |
| Done-so-far records audit state | Audit or explicit no-audit is present | 001-007 | — | validation JSON + semantic keywords | COVERED | medium |
| Open decisions name an actor | Pending decisions are actionable and attributed | 001-005, 007 | — | actor token check | COVERED | medium |
| `validate --json` and structural edge gates | Duplicate/missing headers, wrong none-token, missing actor, and missing audit state fail without echoing sensitive input | — | 008-validation-edge-gates | deterministic script matrix | ADDED | high |
| Read-only boundary | Running `new`/`validate` does not mutate a probe file or repository state | existing checker indirectly | 008-validation-edge-gates | SHA-256 before/after | ADDED | medium |

## Coverage Gaps

- EAF routing is documented but intentionally belongs to `eaf-session-handoff`; this suite verifies only the generic Skill boundary.
- The semantic quality of a handoff is only partially machine-checkable; reader simulation remains a human review concern.
- The soft line-count advisory is not yet asserted as a dedicated case.

## Case Taxonomy

- Existing cases cover `baseline`, `boundary`, `security`, `minimal`, `multi-turn`, and `recovery`.
- `008-validation-edge-gates` adds `negative`, `adversarial`, `state`, and `read-only`.

## Suspected Defects

None from static audit. Any failure in 008 must first be classified as Eval/Engine/Environment noise before attributing it to the Skill.
