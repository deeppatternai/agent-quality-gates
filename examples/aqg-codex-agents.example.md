# AQG rules for Codex

Codex's rules file is called `AGENTS.md` (not `CLAUDE.md`). The mechanism is symmetric with Claude Code:

- **Global / user level**: `~/.codex/AGENTS.md` — auto-loaded for all projects
- **Project level**: `<project>/AGENTS.md` — applies only to that project, and can be committed to git for the team to share

Just `cat` the block below into either location:

```bash
# Global: let Codex proactively use AQG across all projects
bash -lc 'sed "s|<AQG_ROOT>|$HOME/.deeppattern/agent-quality-gates|g" "$HOME/.deeppattern/agent-quality-gates/examples/aqg-codex-agents.example.md" >> ~/.codex/AGENTS.md'

# Or project level: run in the target project's root directory
bash -lc 'sed "s|<AQG_ROOT>|$HOME/.deeppattern/agent-quality-gates|g" "$HOME/.deeppattern/agent-quality-gates/examples/aqg-codex-agents.example.md" >> AGENTS.md'
```

> The `bash -lc '...'` wrapper is there so PowerShell / Windows Terminal users
> can paste it directly too: PowerShell does not expand the `$HOME` path.

Installed skills auto-match by description —— this rule block exists so the
critical junctures are not missed. See the README for skill installation.

Codex also gets a managed `UserPromptSubmit` handoff-routing hook from
`scripts/install_aqg_codex_hooks.py`, so a handoff request reaches the
`aqg-session-handoff` skill without this file having to say so. That is hook
wiring, not a decision criterion, which is why it is documented here rather
than inside the block below: everything under the heading is copied verbatim
into a user's rules file and is resident in the model's context for every
session on that machine, so it carries only what must be present BEFORE a
decision is made.

---

## Agent Quality Gates (AQG) engineering discipline

- Before writing, modifying, or refactoring code, invoke the `aqg-code-construction`
  skill and follow its 6 steps. Its step 5 is the audit-before-commit gate.

- **Blast radius outranks size.** A change touching auth, permissions, crypto,
  secrets, trust-boundary input, data model, CI/deploy, install integrity,
  anything irreversible, or cross-repo contracts is `deep` — no matter how few
  lines it touches.

- **Trivial and non-sensitive** — a rename, a reformat, a comment, or anything a
  test / type-check / lint fully settles — is **NOT audited**. Audit is the
  exception, not the reflex. At most ONE audit per logical change.

- The other `aqg-*` skills announce their own triggers; invoke them by those.

- When delegating to a cold-started agent (sub-task, cron, remote, headless), write
  the discipline into the prompt: it inherits none of this context, and where
  `AQG_ROOT` is unset every AQG hook silently no-ops.

- Full ladder and the authoritative sensitivity list:
  <AQG_ROOT>/docs/policies/audit-trigger.md
