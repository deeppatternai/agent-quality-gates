# WS-8 R4 root-boundary audit adjudication

- Audit ID: `d95c9a5e`
- Mode: deep
- Scope: R3 NameError repair and the root account/chown/process boundary
- Owner decision: create a fresh R4 study; never resume terminal R3

| finding | decision | action | verification |
|---|---|---|---|
| Chown and child privilege drop used identities resolved at different times. | accepted | Prepare and validate each cell first, then use the prepared UID/GID for chown and child privilege drop. | Call-order and identity-equality regression passes. |
| UID zero lacked an executor-local defense-in-depth rejection. | accepted | Require a positive non-root UID before ownership transfer. | R4 account-proof and runtime identity tests pass. |
| The exact primary GID was not proof-bound. | accepted | Account-proof schema 2 binds a positive exact `account_gid`; source snapshot, runtime preflight, and child preparation require equality. | Schema/GID and drift-before-chown tests pass. |
| Recursive chown could expose mutable descendants during traversal. | accepted | Require a caller-owned non-mutable tree, reject symlinks, transfer bottom-up, and use no-follow chown. | Symlink rejection, root-last ordering, and no-follow assertions pass. |
| A leader exit could leave a model process group alive across cells. | accepted | Clean the whole process group unconditionally in both child transports. | Normal-leader-exit and app-server-timeout cleanup regressions pass. |
| Keychain sentinel existence was checked after the denial probe. | accepted | Prove the non-secret sentinels exist before the denial boundary probe. | Capability boundary tests pass. |
| Every equivalent guard requires an exhaustive duplicate test. | rejected | Security-critical schema, identity, ordering, no-follow, and cleanup behaviors are covered; duplicate predicate-shape tests add little signal. | Test-quality review reports zero shape-only tests. |
| The original minimal patch was already solid. | rejected | Direct code inspection and convergent findings proved the identity-source, GID, chown, and cleanup gaps. | R4 implementation replaces the minimal patch. |

This evidence contains no credential, Keychain value, envelope, nonce, raw
stream, terminal JSON, account-proof content, or protected local path.

## Final R4 audit adjudication

- Audit ID: `7b283d88`
- Mode: deep
- Scope: complete R4 patch across logic, edge cases, security, performance, and concurrency

| finding | decision | action | verification |
|---|---|---|---|
| Read-root protection rejected only exact checkout equality. | accepted | Reject an exact checkout, any checkout ancestor, and any checkout descendant as a model read root. | Read-root regression covers all three relationships. |
| The ITT endpoint should count infrastructure-invalid rows as endpoint failures. | rejected | The preregistered endpoint applies only to valid sealed rows; infrastructure failure terminates and invalidates the attempt instead of becoming a measured defect. | Protocol endpoint and non-endpoint termination tests pass. |
| Paid cells should be retried after infrastructure failures. | rejected | R4 deliberately preserves immutable non-resumable research attempts; retrying within the same study would violate the frozen protocol and Owner R4 ruling. | Termination and no-resume regressions pass. |
| The live executor had an unsafe default source snapshot. | accepted | Make the snapshot explicit and require a canonical caller-owned directory disjoint from the dedicated checkout. | Snapshot isolation and source-materialization tests pass. |
| A Codex cleanup timeout could skip bounded raw evidence writes. | accepted | Preserve cleanup failure as `drain_error`, then close and persist both bounded streams before raising. | Codex timeout and bounded-stream regressions pass. |
| The aggregate 200 USD ceiling was procedural rather than enforced. | accepted | Under the root account lock, sum all immutable prior WS-8 terminal USD and reject unknown usage or `prior + requested maximum > 200`. | Exact-ceiling, over-ceiling, and incomplete-usage reservation tests pass. |
| The capability design assumes the pinned CLIs can authenticate while protected roots stay unreadable. | rejected | This is the empirical purpose of the paid two-call capability gate, not a claimed static fact; any mismatch terminates R4 before pilot. | Runtime and credential-boundary capability gates remain fail-closed. |
| Root loaded Python from the dedicated-account checkout. | accepted | Execute only from a clean exact Owner-controlled checkout while using the proof-bound dedicated checkout solely as the Git snapshot source. | Owner-code trust and source-checkout split tests pass. |
| Runtime version probes executed the CLI as root. | accepted | Drop to the proof-bound UID/GID and use a scrubbed dedicated-account environment for version probes. | Runtime preflight identity and version tests pass. |
| Immutable ledger writes did not handle short writes. | accepted | Route ledger and private raw writes through the same no-progress-detecting full-write loop. | Short-write and zero-progress regressions pass. |
| The process-group cleanup test asserted mock shape rather than descendant behavior. | accepted | Replace it with a real leader that leaves a descendant and assert the descendant is gone after cleanup. | Descendant cleanup regression passes. |
| Uniform aggregate allocation could violate asymmetric frozen host USD caps. | accepted | Allocate the envelope total proportionally across frozen host caps, then exactly across each host schedule. | Asymmetric 25/75 host-budget regression passes. |
| Root removed a probe artifact from a benchmark-account-owned directory. | accepted | Run fixed `/bin/rm -f` under the proof-bound UID/GID and reject cleanup failure. | Capability boundary cleanup regression passes. |
| Recursive ownership transfer did not reject regular-file hard links. | accepted | Require every regular descendant to have exactly one hard link before no-follow bottom-up chown. | Hard-link rejection regression passes. |
| Task contract coverage did not assert each `checks.py` file. | accepted | Require `task.md`, `seed.py`, and `checks.py` for every frozen pilot and primary task. | Protocol task-contract regression passes. |
| The audit attachment omitted two supporting modules. | rejected | This was an audit packaging limitation rather than a repository defect; both modules were inspected locally and are exercised by the full WS-8 suites. | Full zero-cost WS-8 groups cover the supporting modules. |
| A test patched a different `os` module than the implementation uses. | rejected | Both modules reference the same imported Python `os` module object; the test now also asserts that identity before exercising short writes. | Short-write regression passes with the explicit identity assertion. |

This final adjudication exposes no credential, Keychain value, envelope, nonce,
raw stream, terminal JSON, account-proof content, or protected local path.
