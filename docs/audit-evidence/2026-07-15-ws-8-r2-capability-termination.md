# WS-8 R2 capability termination evidence

- Date: 2026-07-15
- Repository: `deeppatternai/agent-quality-gates`
- Study: `ws-8-aqg-cross-host-2026-07-r2`
- Attempt phase: capability setup
- State: terminal and immutable
- Bound repair source commit: `6d5e5b26da6c80043c6c26763aa7ae2fdd88c939`

## Redaction-safe terminal record

The Owner/admin read-only hash capture reported the following root-owned
terminal-marker digests:

- R2 study termination SHA-256:
  `7b72f3d7099906004339ee2413f704c482c7b8d93f2981ec86422328bc33aa2b`.
- R2 attempt termination SHA-256:
  `7b72f3d7099906004339ee2413f704c482c7b8d93f2981ec86422328bc33aa2b`.
- Matching attempt markers: `1`.

The helper independently hashed the study and attempt terminal paths and
observed byte-identical contents.  The equal digest is an observation about
the two immutable records, not a replacement of either record.  No nonce,
terminal JSON, raw stream, account credential, or protected root path is
committed here.

The corresponding redaction-safe state summary was:

- expected cells: `2`; completed cells: `0`;
- ledger rows: `0`; ledger hosts: `NONE`; ledger digest: absent;
- accounted model calls: `0`; incomplete-usage flag: `false`.

Therefore this attempt provides no benchmark observations, outcomes, or
cross-host comparison.  It must not be interpreted as a model result or a
partial benchmark result.

## Failure boundary and repair

The R2 authorization and source-snapshot preflight succeeded before the
terminal failure.  A safe metadata probe then found the private root and the
read-only source snapshot present, while the later workspaces, controls, and
raw-output directories were absent.  This places the failure before transport
initialization, ledger creation, and every model subprocess.

The root cause was Python module identity: direct execution loaded
`ws8_execute.py` as `__main__`, while `ws8_transport` imported the same file as
`ws8_execute`.  The resulting duplicate `AuthorizationLease` classes caused
the transport's type check to reject the otherwise valid lease.

Merged repair [PR #35](https://github.com/deeppatternai/agent-quality-gates/pull/35)
uses the canonical script-entry guard and aliases the fully initialized
`__main__` module under `ws8_execute` before `main()` imports the transport.
The repair is bound to the source commit above.  Its focused execute and
transport suites passed before merge, including direct-entry identity and
non-canonical-copy rejection regressions.

## Finality and next authorization boundary

R2 cannot be resumed, retried, or supplied with a replacement nonce.  The
attempt and study terminal records remain immutable even though no paid model
call was accounted.  No raw terminal record was read or copied to make this
determination.

Any future execution requires a distinct study identity (for example, R3), a
fresh Owner-issued authorization envelope, and an explicit new spend decision.
This R2 record neither grants that authorization nor carries forward its nonce,
cap, or result state.
