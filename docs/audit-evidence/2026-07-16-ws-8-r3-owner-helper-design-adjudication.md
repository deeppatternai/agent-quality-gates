# WS-8 R3 Owner helper design audit adjudication

Date: 2026-07-16
Audit ID: `20b324d9`
Scope: proposed one-command Owner-only capability-envelope preparation and zero-cost validation helper.
Result: six panel voices completed and one voice was unparsable. The completed findings were adjudicated below; no accepted finding is left without a planned implementation or verification.

| finding | decision | action | verification |
|---|---|---|---|
| The proposal needs an explicit no-envelope to pending to validated to ready state machine. | accepted | Use a unique hidden exclusive pending file and publish a ready envelope only after the existing zero-cost verifier succeeds. | Focused success and verifier-failure tests assert ready and pending path states. |
| Stale pending files, concurrent calls, ready-path collisions, and publication failure were underspecified. | accepted | Give every invocation a fresh nonce, never consume pending files, use no-clobber publication, remove only the current invocation's pending file on catchable failure, and document that an uncatchable termination may leave a harmless orphan. | Focused collision, repeat, verifier-failure, and publisher-failure tests. |
| The ready-envelope ownership and mode contract was not explicit. | accepted | Require root execution, create pending mode 0600, preserve root ownership, and require the published ready file to be a single-link regular file with mode 0600. | Metadata tests plus the existing authorization-envelope loader tests. |
| The helper needs a precise command interface, exit behavior, output schema, and repeat behavior. | accepted | Expose one required account argument, no execute option, exit zero only after publication, emit fixed machine-readable zero-cost fields and the Owner-local ready path, and create a distinct fresh envelope on each successful call. | Parser, output-schema, repeat, and error-path tests. |
| Omitting an execute flag from argv is not enough to prove zero paid execution. | accepted | Delegate to the existing verification-default `ws8_execute.main`, require its exact zero-turn status fields, and reject any unexpected result. | Delegation test asserts complete argv and zero-turn output; existing verifier regression asserts reservation is never called in verification mode. |
| Canonical checkout, authorization root, proof metadata, and bootstrap checks must all occur before file creation. | accepted | Reuse the canonical source and authorization validators, reject copied entry and unsafe roots, and validate the active dedicated-account bootstrap before opening the pending file. | Invalid-root, copied-entry, bootstrap, and create-order tests. |
| `launchctl asuser` may make the root-owned mode-0600 proof and envelope unreadable. | rejected | The runbook's existing macOS contract states that `launchctl asuser` supplies the logged-in bootstrap without dropping root authority; the helper preserves that contract and rechecks effective uid after re-entry. | Focused bootstrap argv test and root precondition test, grounded in `WS8_RUNBOOK.md` section 3. |
| A signal handler should guarantee cleanup after every interruption, including SIGKILL or power loss. | rejected | SIGKILL and power loss are not catchable. Safety comes from never treating pending files as ready and from using a unique pending name so an orphan cannot block later work; catchable failures still clean up in `finally`. | Failure cleanup tests and runbook recovery text. |
| The helper could bypass the existing authorization semantics by reimplementing validation. | accepted | Build the minimal envelope locally but call `validate_authorization_payload` and the existing full verification entry as the semantic source of truth. | Payload-delegation and full-verifier-delegation tests. |

No finding requires a new Owner decision. This change does not authorize paid execution, credential changes, account-proof changes, or access to Owner-only state by Codex.
