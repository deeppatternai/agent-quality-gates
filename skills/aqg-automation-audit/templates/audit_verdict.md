# Automation Audit Verdict — <date> <project>

> Filled by reviewer after running `inventory.py` + `overlap_check.py`.
> Output is import-able into `aqg-evidence-closeout` ledger Evidence section.

## Inventory snapshot

- **hooks** — N (L live / B broken / R redundant / D disabled)
- **mcp** — N (L live / B broken)
- **plugins** — N (E enabled / D disabled)
- **skills** — N (L live / D disabled / R redundant)
- **env** — N keys present / M missing required

## Verdicts

| # | item | category | state | overlap with | severity | verdict | action |
|---|---|---|---|---|---|---|---|
| 1 | `example-plugin:construction-gate` | skill | disabled | aqg-code-construction | blocking | disable | recommend the human turn it off |
| 2 | `example-plugin:verification-loop` | skill | disabled | aqg-evidence-closeout | silent | disable | recommend the human turn it off |
| 3 | `some-plugin:debug` | skill | disabled | aqg-systematic-debugging | silent | disable | recommend the human turn it off |
| 4 | `claude-plugins-official:semgrep` PostToolUse | hook | live | aqg-evidence-closeout | noise | keep-as-tool | SAST gate is useful; SEMGREP_APP_TOKEN set |
| 5 | `github-official` | mcp | broken | (GitHub Actions raw logs path) | blocking | fix | complete OAuth on next session |
| 6 | (env: `AQG_ROOT`) | env | present | aqg-startup-preflight | - | - | OK |
| 7 | (env: `SEMGREP_APP_TOKEN`) | env | present | semgrep PostToolUse | - | - | OK |

### State legend
- `live` — installed + connected + not disabled
- `broken` — installed but fails (auth pending / env missing)
- `redundant` — duplicate capability across sources
- `missing` — expected but absent
- `dormant` — installed but trigger never fires
- `disabled` — explicitly turned off

### Severity legend
- `blocking` — halts AQG flow until fixed
- `noise` — pollutes context but does not block
- `silent` — hijacks routing without obvious signal
- `cosmetic` — no behavioral collision: a description-only overlap, OR a plugin hook present in cache but unable to fire (disabled plugin / superseded cache version, carrying a `liveness` field)

### Verdict legend
- `disable` — recommend disabling the conflicting item (the human renames or turns it off)
- `migrate` — vendor the capability into AQG (file a follow-up task)
- `keep-as-tool` — retain as domain tool library
- `fix` — repair config (env / OAuth / version)

## Decision

- Overall: `clean` | `needs-action` | `needs-owner-decision`
- Owner-required items (Owner-only 5 项): _list any items needing Owner decision_
- Next AQG move: _exact skill / hook / workflow lane to strengthen_

## Hand-off to closeout (YAML — paste into `.aqg/current_ledger.md` Evidence)

```yaml
automation_audit:
  audited_at: <UTC ISO 8601>
  inventory_snapshot: .aqg/automation_audit_<date>.json
  counts:
    hooks: { live: N, broken: N, redundant: N, disabled: N }
    mcp: { live: N, broken: N }
    plugins: { enabled: N, disabled: N }
    skills: { live: N, disabled: N, redundant: N }
    env: { present: N, missing: N }
  verdicts:
    - item: <plugin_key:skill_name OR hook_id OR mcp_name OR env_key>
      category: skill | hook | mcp | env | plugin
      state: live | broken | redundant | missing | dormant | disabled
      overlap_with: <AQG layer>
      severity: blocking | noise | silent | cosmetic
      verdict: disable | migrate | keep-as-tool | fix
      action: <what to do>
  decision: clean | needs-action | needs-owner-decision
```
