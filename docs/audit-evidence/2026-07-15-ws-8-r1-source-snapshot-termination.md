# WS-8 R1 source-snapshot termination evidence

- Date: 2026-07-15
- Repository: `deeppatternai/agent-quality-gates`
- Terminated study: `ws-8-aqg-cross-host-2026-07`
- Failed phase: capability setup
- User-visible terminal error: `commit-addressed source snapshot is unavailable`
- Bound source commit: `d6743e116d8998e137b2209a54e63019bd1ebab8`
- Bound R1 protocol SHA-256:
  `a59ba5dbff6251708e6371dde1120605d2f02f5ea73c1b139a8593948bf51683`
- Nonce SHA-256 (nonce value not committed):
  `8d81129a676dfa3bba9ea151e22065757a8eeb5a50d817cdf5ee02a31449b3be`
- Operator envelope-source SHA-256:
  `989dcffe2a995f1a965ef666df15318385b4bb5bf74c79a382c9b75f13619a24`
- Paid boundary: authorization was validated and reserved; failure occurred in
  `materialize_source_snapshot` before `make_live_cell_executor` and before any
  Claude or Codex subprocess could be constructed.
- Accounted usage from the reached code path: 0 model calls, 0 Owner-cap USD.
- Root cause: `materialize_source_snapshot` intentionally supplied a scrubbed
  subprocess environment, which discarded the wrapper's `GIT_CONFIG_*`
  `safe.directory` override. Root Git therefore rejected the checkout owned by
  the dedicated non-admin benchmark account.
- Reproduction: under a clean environment, root/foreign-owner `git archive`
  fails with dubious-ownership status; the same command succeeds with the
  exact-path `-c safe.directory=<checkout>` override. That override is
  diagnostic evidence only, not the accepted fix.
- Accepted fix: execute only the commit-addressed `git archive` subprocess as
  the proof-bound dedicated account, after checking its live UID against the
  root-owned account proof. Root never parses the foreign-owned repository as
  a trusted Git directory. Global/system Git config and replacement objects are
  disabled in the scrubbed child environment.
- Evidence boundary: do not delete, alter, resume, or reuse the R1 nonce or
  terminal state. Raw streams and credentials remain private and are not part
  of this artifact.
- Root-owned terminal references (hash read-only before R2 execution):
  `/var/db/aqg-ws8/ws-7tester/state/attempts/<R1-nonce>/termination.json` and
  `/var/db/aqg-ws8/ws-7tester/state/studies/ws-8-aqg-cross-host-2026-07/study-termination.json`.
  Owner/admin read-only capture after the R2 repair target was updated emitted
  only labeled digests; it did not print a nonce, raw terminal content, or
  credentials. The following immutable bindings were recorded:
  - R1 attempt termination SHA-256:
    `b7de6e9b6afec6954ce2eff60fd6f00d277b2a47712f1e1b3b0a412f452fa7d4`.
  - R1 study termination SHA-256:
    `b7de6e9b6afec6954ce2eff60fd6f00d277b2a47712f1e1b3b0a412f452fa7d4`.
    The approved helper separately hashed the two terminal paths and observed
    this same digest for both; it is an observed byte-identical result, not a
    copied substitute for either record.
  - Current root-owned R2 account-proof SHA-256:
    `1fe8f8074641141dc3b918abe1e6ca0cdcc4a5854c628b4629ac357931693fc7`.
  - Current R2 protocol SHA-256:
    `857f7ab0e4b3104b9adb83c1c5eea22f8e99cdda7eb7f825e44f38eb42a72213`.
  The final R2 envelope must bind these current R2 proof/protocol digests and
  the latest merged B `main` source commit; it must never reuse the R1 nonce.
  R1 recorded reservation before its pre-fix source-snapshot failure; the
  merged R2 runner reverses that boundary by preflighting the source snapshot
  before nonce reservation.
- Existing mechanical finality checks:
  `test_nonce_and_account_lock_reject_replay_or_concurrency`,
  `test_partial_attempt_cannot_resume`, and
  `test_terminal_markers_are_single_assignment_and_reservation_is_immutable`.
- Superseding execution identity: `ws-8-aqg-cross-host-2026-07-r2`, using a
  fresh nonce and an envelope bound to the merged fix commit and new protocol
  digest.
