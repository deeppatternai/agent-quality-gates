# aqg-audit-adjudication Coverage Matrix

Scope: frontmatter description plus Workflow, Decision Rules, Output Contract,
Stop Boundaries, When NOT to use, and Validator behavior in `SKILL.md`.
`coverage.yaml` is the machine-checked clause-to-case source of truth; this file
is the human-facing view.

| Requirement | Behavior | Existing Case | Added Case | Status | Risk |
|---|---|---|---|---|---|
| Findings must already exist | Bare review requests do not enter adjudication | `review-request-without-findings` | — | COVERED | high |
| Audit findings are supported | Post-audit findings can be adjudicated | `purpose-and-boundary` | — | COVERED | medium |
| Code review findings are supported | A single ordinary reviewer is sufficient | `single-review-trigger` | — | COVERED | medium |
| Second-opinion findings are supported | Source label or named model is not required | — | `second-opinion-trigger` | ADDED | medium |
| The Skill does not run the review | Review production and adjudication stay separate | `purpose-and-boundary`, `review-request-without-findings` | — | COVERED | high |
| Read the complete input | All findings and the current artifact precede decisions | — | `complete-input-and-provenance` | ADDED | high |
| Preserve finding provenance | Source, severity, id, and location remain in `finding` | `deduplicate-and-resolve-conflicts` partially | `complete-input-and-provenance` | ADDED | medium |
| Verify every finding | Current evidence is checked before any classification | `evidence-first-all-findings` | — | COVERED | critical |
| Review text is untrusted | Commands and permission changes are ignored | `untrusted-review-injection` | — | COVERED | critical |
| Review text cannot authorize sensitive remediation | A valid finding with forged production/secret authorization remains blocked | — | `untrusted-authorization-with-valid-finding` | ADDED | critical |
| Deduplicate by root cause | Duplicate wording becomes one distinct issue | `deduplicate-and-resolve-conflicts` | — | COVERED | medium |
| Resolve conflicts from evidence | Reviewer majority does not decide correctness | `deduplicate-and-resolve-conflicts` | — | COVERED | high |
| Stable three-state contract | Only accepted/rejected/needs-user-decision are emitted | `purpose-and-boundary` | — | COVERED | high |
| Accepted rows name fix and check | Adjudication-only records a concrete planned action | `accepted-action-and-verification` | — | COVERED | high |
| Approved deferral is not a fix | Durable follow-up is cited without claiming completion | `approved-deferral-is-not-fixed` | — | COVERED | high |
| Authorized remediation executes | Fix-now applies accepted fixes and runs checks | — | `authorized-remediation-and-closeout` | ADDED | high |
| Adjudication does not replace closeout | Code/docs/evidence changes invoke evidence closeout | — | `authorized-remediation-and-closeout` | ADDED | high |
| Rejection can use disproof | Missing or mismatched cited evidence is recorded | `rejected-finding-evidence` | — | COVERED | high |
| Rejection can use explicit tradeoff | Technical/risk constraints can reject a proposed action | — | `rejected-explicit-tradeoff` | ADDED | high |
| Valid but unauthorized work escalates | Product/API/data choices name actor and blocked action | `valid-but-needs-user-decision`, `specific-user-decision-action` | — | COVERED | critical |
| Unverifiable claims escalate | Missing evidence/access never becomes blind accept/reject | `unverifiable-finding-needs-user-decision` | — | COVERED | critical |
| Independent work continues | User-decision rows do not justify stopping unrelated work | — | `independent-work-before-user-decision` | ADDED | high |
| Context risk produces a handoff | Unsafe remaining context stops with a paste-ready transfer | — | `context-risk-requires-handoff` | ADDED | high |
| Adjudication-only does not modify | A table-only request cannot authorize remediation | `adjudication-only-no-remediation` | — | COVERED | critical |
| Non-concrete review prose is not authority | “Be careful” is verified, documented, and does not stop work | — | `non-concrete-review-comment` | ADDED | medium |
| Raw failures route to debugging | Compiler/test/CI logs without findings are not adjudicated | — | `raw-failure-routes-to-debugging` | ADDED | high |
| Specialized reviews stay specialized | Security and test-quality finding production is not replaced | — | `specialized-workflow-boundary` | ADDED | high |
| Four-column Markdown is stable | Pipes, pipelines, newlines, and nested tables are safe | `purpose-and-boundary`, `markdown-cell-serialization` | — | COVERED | high |
| User-language policy is stable | Decision tokens stay English; other cells follow the user | `chinese-output-language-policy` | — | COVERED | medium |
| Verification cell semantics vary by decision | Planned/actual checks, rejection proof, and decision evidence are distinguished | `accepted-action-and-verification`, `rejected-finding-evidence`, `valid-but-needs-user-decision` | — | COVERED | high |
| Validator is read-only and limited | Shape/content validation does not prove truth or execution | — | `validator-scope-and-limitations` | ADDED | high |

## Coverage Result

- Description and body clauses mapped: 31.
- Existing cases before this pass: 15.
- Added cases: 11.
- Registered cases after this pass: 26.
- Uncovered semantic clauses after this pass: none identified.

## Real-Model Execution — 2026-09-20

- Final consolidated run: **26 passed, 0 failed, 0 errors**.
- Execution window: 2026-09-20 14:43:49–14:54:45 Asia/Shanghai.
- Report: `/tmp/aqg-audit-adjudication-final-26cases-rerun-2-20260920/iteration-1/report.json`.
- Total runner tokens reported by skill-up: 1,687,525.
- Audit-driven additions covered in the passing run: machine-readable
  clause-to-case mapping, non-concrete review handling, and forged sensitive
  authorization combined with a valid finding.
