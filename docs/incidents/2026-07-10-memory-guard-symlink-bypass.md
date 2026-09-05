<!--
AQG_INCIDENT_RECORD
schema_version: 1
slug: memory-guard-symlink-bypass
date: 2026-07-10
actor: claude
severity: P2
detection_source: audit
impact_scope: dev
audit_id: 
pr_url: 
marker: auto-generated-by-aqg_incident_index
-->

# Incident Record: Memory-write guard bypassable via symlink and case-folding

- date: 2026-07-10
- severity: P2
- detection source: audit
- impact scope: dev

## Summary

The pre-tool memory-write guard could be steered to write outside the memory root through a symlink, and a case-insensitive filesystem let a differently-cased path slip the allowlist.

## Root Cause

The guard compared the requested write path against the memory root by prefix on the un-resolved path. A symlink placed at or above the target let a write land outside the intended directory (CWE-59, link following). Separately, the prefix compare was case-sensitive, so on a case-insensitive filesystem a path differing only in letter case (CWE-178) could slip the allowlist while resolving to a protected or out-of-root location.

## Resolution

The guard now canonicalizes the target with realpath and enforces containment against the resolved memory root, and compares case-folded so a case-insensitive filesystem cannot bypass the allowlist. A regression test drives both the symlink-escape and the mixed-case vector. Caught by the audit gate before the vulnerable change merged; the fix and its regression test landed in the same pre-merge cycle, and nothing left the dev environment.

## Followups

- The session-dedup marker write was hardened separately with O_NOFOLLOW plus lstat under a user-private cache dir (same CWE-59 class on the marker path).

## Boundaries

production/deploy/restart: not touched; secrets/private/raw data: not touched (guard is a local hook); Owner/admin action: none.
