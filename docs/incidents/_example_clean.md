<!--
AQG_INCIDENT_RECORD
schema_version: 1
slug: example-clean
date: 2026-05-04
actor: ci-bot
severity: P3
detection_source: scheduled_check
impact_scope: dev
audit_id: 
pr_url: 
marker: auto-generated-by-aqg_incident_index
-->

# Incident Record: Example clean incident for index validation

- date: 2026-05-04
- severity: P3
- detection source: scheduled_check
- impact scope: dev

## Summary

Example clean incident record used by CI validate-path to ensure the AQG_INCIDENT_RECORD metadata block parses and INDEX.md generation works.

## Root Cause

Filename starts with underscore so the indexer skips this sample by design.

## Resolution

No action required.

## Followups

- (none)

## Boundaries

- production/deploy/restart: not touched
- secrets/private/raw data: not touched
- Owner/admin action: none
