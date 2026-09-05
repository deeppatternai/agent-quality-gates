<!--
AQG_INCIDENT_RECORD
schema_version: 1
slug: hook-apostrophe-quote-break
date: 2026-07-10
actor: claude
severity: P3
detection_source: audit
impact_scope: dev
audit_id: 
pr_url: 
marker: auto-generated-by-aqg_incident_index
-->

# Incident Record: Embedded python in a shell hook crashed on an apostrophe

- date: 2026-07-10
- severity: P3
- detection source: audit
- impact scope: dev

## Summary

A hook embedded python inside single quotes; an apostrophe in the embedded code closed the surrounding shell quote, crashing the hook at runtime and blocking later tool calls.

## Root Cause

Several hooks wrapped inline python in a single-quoted shell string. An apostrophe inside a python comment or string literal prematurely closed that surrounding single quote, so the shell mis-parsed the remainder. The hook failed at runtime, and because it ran as a pre-tool gate it blocked subsequent tool calls until noticed.

## Resolution

A regression guard now lint-checks the shell and parses the embedded python of every hook, so an apostrophe-broken hook fails CI instead of at runtime. Existing hooks were reviewed for the pattern. Surfaced by the audit gate before merge.

## Followups

- Prefer a heredoc or a separate script file over inline single-quoted python for any non-trivial embedded python in a hook.

## Boundaries

production/deploy/restart: not touched; secrets/private/raw data: not touched; Owner/admin action: none.
