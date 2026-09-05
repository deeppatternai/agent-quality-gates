<!--
AQG_BUGFIX_RECORD
schema_version: 1
slug: <slug>
actor: <claude / codex / gpt-5.5 / gemini / o3 / human / ci-bot>
taxonomy: []
regression: false
audit_id: <8-hex or empty>
pr_url: <https://github.com/... or empty>
marker: auto-generated-by-bugfix_record
-->

# Bug Fix Record: <short title>

- date: YYYY-MM-DD
- affected area: <script / skill / CI adapter / config / docs>
- severity: <low / medium / high>
- backward compatible: <yes / no / migration needed>

## Symptom

<What failed, including command, PR, fixture, or user-visible behavior.>

## Root Cause

<Why it failed. Include code path or parsing rule when relevant.>

## Fix

<What changed.>

## Verification

```bash
<command>
```

Result: <exit/result>

## Regression Coverage

<Fixture, self-test, or follow-up needed.>

## Boundaries

- production/deploy/restart: <not touched / authorized evidence>
- secrets/private/raw data: <not touched / redacted evidence>
- Owner/admin action: <none / exact blocker>

