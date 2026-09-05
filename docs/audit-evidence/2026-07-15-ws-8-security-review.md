# WS-8 in-session security review

```yaml
security_review:
  surfaces_audited: [auth, input, secrets, injection, serialization, logging]
  owasp_findings: []
  cwe_findings: []
  secure_defaults:
    - layer: authorization
      library: stdlib lstat fstat O_NOFOLLOW exclusive open plus fcntl flock
      anti_pattern_avoided: repository string or decision-log reference as spending authority
    - layer: serialization
      library: stdlib json with exact field sets and type/range validation
      anti_pattern_avoided: unsafe object deserialization
    - layer: process
      library: argv lists only; no shell interpolation
      anti_pattern_avoided: shell=True or dynamic code execution
  defense_in_depth:
    authorization: [root_owner, safe_mode_bits, exact_source_commit, clean_source_checkout, exact_protocol_bytes_and_sha, phase_and_tier_binding, single_read_prerequisite_hash_and_parse, semantic_power_freeze_gate, finite_caps, expiry, unique_nonce, call_and_resource_caps, preparation_collection_lock]
    attempts: [root_owned_state_tree, exclusive_account_lock, exclusive_nonce_file, immutable_reservation, study_level_termination, phase_tier_completion, mutually_exclusive_completion_or_termination, no_resume]
    credentials: [dedicated_account, account_uid, narrow_cross_host_auth_roots, normalized_paths, canonical_executable_path_and_digest, executable_not_account_writable, exact_preflight_executable_paths, host_invocation_cwd, external_settings, claude_read_edit_write_deny_settings, complete_plugin_root_digest, semantic_capability_receipts, recursive_clean_cell_gate, os_cell_only_read_only_treatment_mount_required, paid_transport_locked]
  decision: accept
  decision_reason: preparation code reads no credential content and exposes no model-calling command; all authority and state helpers fail closed under behavioral tests
  audit_ids: [3689c383, 95e27c5b, e8a6341a, b34eac46]
```

Deterministic SAST:

- The remote Semgrep registry configs could not be downloaded (`HTTP 401`), so that scan is not claimed.
- The available local model-isolation ruleset ran three Python rules over all five WS-8 modules with zero findings: no `shell=True`, dynamic execution, or unsafe YAML load.
- Initial Deep external audit `3689c383` and post-fix Deep audit `95e27c5b` each completed 7/7 voices with no failures. Their accepted security findings are integrated and recorded in `2026-07-15-ws-8-code-deep-adjudication.md`; this in-session review does not replace those audits.
- Re-audit `ff6f5eac` failed at attachment delivery and supplied no artifact-grounded verdict; it is not counted as security evidence.
- Narrowed Deep audit `e8a6341a` completed 7/7 voices but received a stale cached-diff attachment. Its concrete security findings were still adjudicated conservatively: terminal-state overwrite, bare executable lookup, semantic receipt binding, source-commit binding, and collection-lock gaps are fixed; OS-level model isolation remains a locked PR 5 gate.
- Actual-working-tree Deep audit `b34eac46` completed 6/6 voices. Its accepted security findings are now covered by finite-number checks, exact protocol-byte binding, clean-checkout authority, both-root `Read`/`Edit`/`Write` denial, complete plugin-root hashing, exact public coordinate sets, proof-bound preflight executables, and study-level terminal state. The separate OS-level read-only treatment mount remains a locked PR 5 gate.
