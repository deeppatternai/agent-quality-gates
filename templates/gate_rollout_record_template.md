# Gate Rollout Record: <target repo>

- date: YYYY-MM-DD
- target repo: <owner/repo>
- mode: <warn-only / blocking>
- rollout owner: <person or role>

## Gates Enabled

| gate | mode | config |
|---|---|---|
| worktree | <warn/blocking/off> | <config path> |
| audit adjudication | <warn/blocking/off> | <config path> |
| evidence closeout | <warn/blocking/off> | <config path> |

## Expected Failures

<Known gaps before first run.>

## First-Run Evidence

```bash
<command or CI run URL>
```

Result: <summary>

## Override Path

<Who can override and how it is recorded.>

## Rollback Plan

<How to disable or downgrade the gate.>

## Owner/Admin Actions Needed

<None, or exact settings/permissions required.>

