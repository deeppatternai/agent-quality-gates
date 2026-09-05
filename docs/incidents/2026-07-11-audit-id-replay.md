<!--
AQG_INCIDENT_RECORD
schema_version: 1
slug: audit-id-replay
date: 2026-07-11
actor: claude
severity: P2
detection_source: audit
impact_scope: dev
audit_id: 
pr_url: 
marker: auto-generated-by-aqg_incident_index
-->

# Incident Record: Verification-gate audit id could be replayed onto unrelated content

- date: 2026-07-11
- severity: P2
- detection source: audit
- impact scope: dev

## Summary

A closeout gate accepted an audit id as proof that content had been reviewed, but the id was not bound to the content, so a stale id could be replayed to mark unrelated changes as audited.

## Root Cause

The gate treated the presence of an 8-hex audit id as sufficient evidence that the current artifact had been through external review. The id was never cross-checked against the review engine's results directory nor bound to a content hash, so the same id passed every replay attempt against different content, an audited-claim forgery vector.

## Resolution

The gate now cross-checks the audit id against the review-engine results directory via an env-configured path before honoring it. Binding the id to a content hash is tracked as a follow-up, and the remaining replay residue is documented honestly rather than claimed fully closed. Surfaced by cross-vendor audit during the same work; it never reached a shared branch.

## Followups

- Bind the audit id to a content hash on the review-engine side so a stale id cannot be replayed against different content (follow-up, not yet closed).

## Boundaries

production/deploy/restart: not touched; secrets/private/raw data: not touched; Owner/admin action: none.
