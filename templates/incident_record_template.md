<!--
AQG_INCIDENT_RECORD
schema_version: 1
slug: <slug>
date: <YYYY-MM-DD>
actor: <claude / codex / gpt-5.5 / gemini / o3 / human / ci-bot / other>
severity: <P1 / P2 / P3 / P4>
detection_source: <monitoring / user_report / scheduled_check / audit / manual>
impact_scope: <production / staging / internal / dev>
audit_id: <8-hex or empty>
pr_url: <https://github.com/... or empty>
marker: auto-generated-by-aqg_incident_index
-->

# Incident Record: <short title>

- date: YYYY-MM-DD
- severity: <P1 / P2 / P3 / P4>
- detection source: <monitoring / user_report / scheduled_check / audit / manual>
- impact scope: <production / staging / internal / dev>

## Summary

<One-sentence description of what happened and what was affected.>

## Root Cause

<Why it happened. Include code path / config / external dependency when relevant. Reference commit/PR if root cause is in code.>

## Resolution

<What was done to resolve. Reference fix commit/PR.>

## Followups

- <action item 1 (assignee + target date)>
- <action item 2>

## Boundaries

- production/deploy/restart: <not touched / authorized evidence>
- secrets/private/raw data: <not touched / redacted evidence>
- Owner/admin action: <none / exact blocker>
