# AQG rules for Claude Code

`cat` the block below into your Claude Code global rules file so that Claude
**proactively** invokes AQG's 16 skills across all projects:

```bash
bash -lc 'sed "s|<AQG_ROOT>|$HOME/.deeppattern/agent-quality-gates|g" "$HOME/.deeppattern/agent-quality-gates/examples/aqg-claude-rules.example.md" >> ~/.claude/CLAUDE.md'
```

> The `bash -lc '...'` wrapper is there so PowerShell / Windows Terminal users
> can paste it directly too: PowerShell does not expand the `$HOME` path.

Only want AQG to apply in a single project? Paste this block into
`<project>/CLAUDE.md` (the project root or `.claude/CLAUDE.md`) instead; the
rules then apply only to that project's Claude Code sessions.

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
