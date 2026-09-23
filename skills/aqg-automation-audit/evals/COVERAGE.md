# aqg-automation-audit Coverage Matrix

Scope: deterministic host inventory, overlap classification, liveness, redaction, and advisory/read-only behavior.

| Requirement | Behavior | Existing Case | New Case | Oracle | Status | Risk |
|---|---|---|---|---|---|---|
| Inventory captures automation sources | Hooks, plugins, skills, env, and MCP shape are represented | 001, 002, 004, 006 | — | inventory schema | COVERED | high |
| Conflict rules are applied | Pre/Post/UserPrompt and routing overlaps receive expected severity | 002, 003, 004, 006 | — | structured findings | COVERED | high |
| Normal coexistence is not over-reported | Unrelated helper is not flagged | 004, 006 | — | absence of unsupported finding | COVERED | high |
| Malformed input fails safely | Wrong-shaped JSON exits 3 and reports schema evidence | 005 | — | exit code + parse errors | COVERED | high |
| Secret values are redacted | Hook command/env/path free text never leaks canary | 002 + self-test | 007-redaction-liveness | no-leak and placeholder checks | ADDED | critical |
| Disabled/stale hooks are downgraded | Positive liveness evidence yields cosmetic/not-live finding | — | 007-redaction-liveness | liveness/severity/rule fields | ADDED | critical |
| Missing data is fail-safe | Empty enabled/current-version data does not silence a real conflict | — | 007-redaction-liveness | live conflict remains | ADDED | critical |
| Advisory/read-only boundary | Inventory and overlap do not mutate fixture | all existing | 007-redaction-liveness | snapshot equality | COVERED | high |

## Coverage Gaps

- Live MCP discovery depends on host CLI availability; fixtures set `AQG_SKIP_MCP=1`, so MCP transport failures remain environment evidence rather than Skill evidence.
- Human verdict application (`keep-as-tool`, `migrate`, `disable`, `fix`) is outside the read-only scripts and is not executed.

## Case Taxonomy

- Existing cases: `baseline`, `boundary`, `security`, `environment`, `integration`.
- `007-redaction-liveness`: `security`, `adversarial`, `state`, `read-only`.

## Suspected Defects

None from static audit.
