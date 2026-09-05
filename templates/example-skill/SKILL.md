---
name: aqg-example
description: Demonstrate the AQG skill authoring conventions in any repository for engineers learning the SKILL_AUTHORING_GUIDE; runs a no-op example check that always succeeds. Use when a new author needs a copy-pasteable starting point. Not for production work; not registered in install/doctor.
---

# AQG Example Skill

This is the worked example referenced by [docs/SKILL_AUTHORING_GUIDE.md](../../docs/SKILL_AUTHORING_GUIDE.md).
It exercises every section of the guide so a new author can copy this
directory, rename the entrypoint, and walk through the §4 checklist.

## How To Run

This is a **source skill** (lives in `templates/`, modeled after
`skills/aqg-*/`). Per GUIDE §3.1 source-variant: `AQG_ROOT` is required,
no `CLAUDE_SKILL_DIR` fallback.

```bash
aqg_root="${AQG_ROOT:-}"
script="$aqg_root/templates/example-skill/scripts/example_check.py"
if [ -z "$aqg_root" ] || [ ! -f "$aqg_root/VERSION" ] || [ ! -f "$script" ]; then
  echo "ERROR: cannot resolve Agent Quality Gates root." >&2
  echo "  Fix: export AQG_ROOT=/path/to/agent-quality-gates checkout." >&2
  exit 1
fi
python3 "$script" --message "hello from aqg-example"
```

When you copy this template into `skills/aqg-<your-name>/`, the source
skill keeps the same strict AQG_ROOT requirement; the Claude wrapper
you create at `agent-packs/claude-code/skills/aqg-<your-name>/SKILL.md`
should use the longer `CLAUDE_SKILL_DIR` fallback (GUIDE §3.1 wrapper
variant).

## Workflow

The example check has a single step:

1. Print the message argument and exit 0 (success).

A real skill would replace this with a structured workflow (preflight
check, ledger generation, validation table, etc.) and then exit 0 for
success or 1 for an expected check failure. See §1.4 of the GUIDE for
the full exit-code contract.

## Boundaries

This example skill follows the same boundary rules as the 5 production
skills:

- does not write to production systems, secrets, or Owner-admin paths
- does not modify state outside its own stdout
- does not enforce anything (just demonstrates the surface)

## Notes

This skill is intentionally **not** registered in `scripts/install.sh`,
`agent-packs/claude-code/install.sh`, or `scripts/aqg_doctor.py`. It
lives in `templates/` so it remains a clean reference without affecting
the real skill index. A new author copies it into `skills/aqg-<name>`
and then walks through GUIDE §4 to register the real skill.

For the touchpoint mapping (which line of this skill satisfies which
checklist item), see [`NOTES.md`](NOTES.md).
