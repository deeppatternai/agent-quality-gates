# WS-8 R3 Owner helper behavior-lock audit adjudication

Date: 2026-07-16
Audit ID: `0a0805cb`
Scope: zero-cost TDD behavior-lock tests for the one-command Owner capability helper.
Result: all seven panel voices completed. Convergent security and completeness gaps were accepted and added to the test contract before the RED run.

| finding | decision | action | verification |
|---|---|---|---|
| Published JSON could include raw Owner context because the test asserted only selected fields. | accepted | Assert the exact authorization field set and explicitly exclude raw proof, raw protocol, and serialized proof content. | Focused success test inspects complete ready-envelope JSON. |
| Main-to-preparer wiring could bypass the real zero-cost verifier while unit seams still passed. | accepted | Drive the real main-to-prepare path while mocking only the existing verifier boundary and require verifier failure to prevent publication. | Focused main integration test asserts invocation and no files after rejection. |
| Verifier output checks were not a strict allowlist and omitted malformed or missing values. | accepted | Require exact status, exact source-preflight value, and integer-not-boolean zero turns; reject missing, arbitrary, malformed, list, and multi-object output. | Table-driven verifier-output tests. |
| Directory safety covered only symlinks, not ownership or group/world write. | accepted | Test symlink, wrong owner, and unsafe write bits; apply the same production check to the fixed authorization root and account root. | Focused directory metadata tests. |
| Account traversal and lower path safety were not locked. | accepted | Reject empty, dot, separator, absolute, and traversal account identifiers; delegate account-proof validation to the existing fixed-root root-owned loader. | Account-identifier and context-loader tests plus existing proof-loader tests. |
| No-clobber publication could be implemented as a racy check then write. | accepted | Use an exclusive pending file and an atomic no-clobber publication primitive; preserve existing ready bytes on collision. | Collision test plus implementation-level security review of the publication primitive. |
| Most preparation tests stubbed the real authorization payload validator. | accepted | Add a dedicated test that builds the final payload and passes it through the real validator with only Git state isolated. | Real-validator payload test. |
| Production nonce freshness and CSPRNG use were untested. | accepted | Use and assert `secrets.token_hex(16)` when no test nonce is injected. | Focused default-nonce test. |
| Old pending files should be deleted or consumed at startup. | rejected | Pending files are deliberately never authority and may be forensic remnants. Each invocation has a unique nonce, ignores unrelated pending orphans, and never deletes Owner state it did not create. | Stale-orphan test asserts it remains untouched and does not block a fresh ready envelope. |
| `launchctl asuser` creates an impossible unprivileged process that cannot read root-owned files. | rejected | The existing macOS contract explicitly retains root authority while selecting the logged-in bootstrap namespace; the helper rechecks effective uid zero after re-entry. | Bootstrap command and root-precondition tests, grounded in `WS8_RUNBOOK.md` section 3. |
| The verifier wrapper should require a particular `next_gate` prose string and an exact full output schema. | rejected | The hard gate is the existing three-field zero-cost contract. Additional safe fields are allowed so verifier diagnostics and prose can evolve without weakening the paid boundary. | Tests require the three exact typed gate values and reject ambiguous/non-JSON output. |
| The helper's platform assumption was implicit. | accepted | Fail closed outside macOS because the Owner flow depends on launchd bootstrap and Keychain behavior. | Unsupported-platform test. |

No finding changes the Owner-only or no-spend boundary. The test audit itself made no benchmark model call.
