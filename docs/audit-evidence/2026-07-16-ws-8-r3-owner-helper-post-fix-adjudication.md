# WS-8 R3 Owner helper post-fix audit adjudication

Date: 2026-07-16
Audit ID: `d3cb1daa`
Scope: post-fix helper, tests, execution guard, runbook, and first implementation adjudication.
Result: all seven panel voices completed. Every helper-scoped blocking finding was accepted, fixed, and covered by a focused regression.

| finding | decision | action | verification |
|---|---|---|---|
| SIGINT or SIGTERM could arrive after a filesystem side effect but before its cleanup-ownership flag. | accepted | Block cleanup signals around pending-create-plus-flag and publish-plus-flags using `pthread_sigmask`; a pending signal is delivered only after cleanup state is accurate. | Signal conversion and two-critical-section tests; helper suite 33 tests. |
| The runbook echoed the full helper JSON containing the ready path and nonce. | accepted | Capture the result silently, extract `$ENVELOPE` locally, and print only status, source snapshot preflight, and paid model turns. | Runbook test rejects full-result echo and requires redaction-safe gate wording. |
| The non-Darwin test fallback could leave a ready link if pending unlink failed. | accepted | Mark the ready path as owned immediately after `os.link`, before pending unlink, so the exception path removes it. | Focused publication tests plus source inspection. |
| Immutable execution-state JSON ignored short writes and retained partial files after I/O failure. | accepted | Add a full-write loop and remove invocation-created immutable state after any write, fsync, or close failure. | `test_exclusive_json_retries_short_writes` and `test_exclusive_json_removes_state_after_fsync_failure`. |
| Pending-write I/O errors escaped the stable helper error gate. | accepted | Wrap non-collision `OSError` as `PreparationError` after the low-level helper has removed its file. | `test_pending_write_io_failure_is_wrapped_for_main_error_gate`. |
| ACL `errno` handling could observe stale thread-local state. | accepted | Clear ctypes errno immediately before `acl_get_file` and `acl_get_entry`. | Focused no-ACL and injected-ACL tests on macOS. |
| The Darwin EEXIST path was claimed not to produce `FileExistsError`. | rejected | Python 3.9 dynamically constructs `FileExistsError` for `OSError(errno.EEXIST, ...)`, and the real Darwin collision test already passes the dedicated `already exists` assertion. | `/usr/bin/python3` constructor probe and collision test. |
| Successful verifier stderr should be tolerated as availability noise. | rejected | Success is a strict machine gate and the real verifier contract test proves one stdout JSON object with empty stderr. Unexpected success diagnostics remain fail-closed. | Real-main wrapper contract test. |
| `launchctl manageruid` should fall back on older macOS. | rejected | The target host supports `manageruid`; silently falling back would weaken proof that euid-zero execution is inside the dedicated account bootstrap. Unsupported hosts fail closed. | Local `launchctl manageruid` probe and wrong-namespace test. |
| `ws-7tester` and UID 502 are accidental WS-7 copy-paste values. | rejected | They are the current proof-bound dedicated account and UID supplied for this WS-8 R3 target; the study identity is independent of the local account name. | Existing account-proof/runtime gate and the Owner-provided environment contract. |
| Root execution from the ordinary-account-owned checkout remains an assumption. | rejected | Repeated systemic observation; it is the frozen cross-phase WS-8 source-trust architecture, not a helper regression. A root-owned runner distribution is a separate architecture project. | Clean proof-bound source and live Owner zero-cost gates remain mandatory. |
| An unused standalone prerequisite hash helper should be deleted. | rejected | Unrelated cleanup would broaden this security thin slice and remove an independently tested API without affecting the helper. | Existing prerequisite tests stay green. |
| Live launchd, ACL ABI, Keychain, Seatbelt, and root ownership require target-host proof. | accepted | Preserve this as the explicit Owner-only post-merge zero-cost validation gate; do not claim it from unit tests. | Owner must report only the three safe gate values before any paid execution. |

The post-fix audit leaves no unaddressed accepted finding. No test or audit step reserved a nonce or executed a benchmark model turn.
