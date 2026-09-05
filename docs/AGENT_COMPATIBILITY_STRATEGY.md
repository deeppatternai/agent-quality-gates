# Agent Compatibility Strategy

## Purpose

Agent Quality Gates should work as a quality layer across coding agents, not as a Codex-only skill package. Codex skills are the first adapter, not the product boundary.

This document defines the compatibility model for:

- Codex skills
- Claude Code skills
- CI adapter and per-project config
- future agent packs
- shared strategy and policy documents

## Design Principle

Keep the enforceable logic agent-neutral. Put agent-specific instructions in thin adapters.

| layer | ownership | examples |
|---|---|---|
| Core policy | agent-neutral | config schema, gate semantics, warning/blocking policy, evidence rules |
| Core scripts | agent-neutral | worktree gate, audit adjudication gate, evidence closeout gate, CI adapter |
| Agent pack | agent-specific | Codex skills, Claude Code skills, hooks, commands |
| Project config | target-repo-specific | `quality-gates.json`, required context files, boundary policy |
| CI integration | target-repo-specific | GitHub Actions workflow, credentials, artifact retention |

Agent-specific packs must call the same core scripts and read the same config shape. They should not fork gate semantics.

## Names

| concept | current value | status |
|---|---|---|
| Product name | `Agent Quality Gates` | canonical |
| Product abbreviation | `AQG` | canonical |
| GitHub repository | `deeppatternai/agent-quality-gates` | canonical repository |
| Codex skill prefix | `aqg-*` | unified canonical prefix |
| Cross-agent skill prefix | `aqg-*` | same as Codex; Claude Code agent pack uses identical names |
| Project config | `quality-gates.json` | the per-project policy file |

Records and CI output should refer to stable gate IDs such as `startup_preflight`, `audit_adjudication`, and `evidence_closeout` instead of depending on agent-specific skill names.

## Supported Agent Targets

### Codex

Current state:

- Codex skills live under `skills/<skill-name>/SKILL.md`.
- `scripts/install.sh` installs those skills into `${CODEX_HOME:-$HOME/.codex}/skills`.
- Existing skills should remain supported while the product expands.

The `skills/` layout is the stable Codex install surface. Any move of it would only ship with mandatory compatibility shims or aliases for existing installs.

**Codex hooks are generated, not packaged** (the WS-6 `agent-packs/codex/` pilot is
retired). `scripts/install_aqg_codex_hooks.py` reads the agent-neutral hook bodies from
`agent-packs/claude-code/hooks/` and writes the Codex config itself, so there is no
Codex-specific pack to keep in sync — the pilot only ever registered one gate and owned
no hook scripts of its own. Codex *skills* remain at `skills/` → `~/.codex/skills/`
(above) — the two are independent. The pilot's **live Codex 0.144.1 smoke** for the Bash
secret-scan gate and the `UserPromptSubmit` handoff reminder confirmed both deliver.
The smoke also found that a PostToolUse stderr-only reminder is not model-visible,
so that error-only hook is deliberately unwired rather than silently ineffective. Codex
edits files via one `apply_patch` tool whose `tool_input` has no `file_path`; the
file-write enforcement gates (memory-write-guard / tamper-guard) still need a patch
parser. The shared secret-scan's added-line parser is unit-tested for `apply_patch`, but
its live payload and deny delivery remain unverified.

The user-level Codex hook installer is `scripts/install_aqg_codex_hooks.py`. It
merges the pilot handlers into `${CODEX_HOME:-$HOME/.codex}/hooks.json`, backs
up only before a changed write, preserves non-AQG handlers, embeds a reviewed
root in the installed commands for desktop launches, and supports `--verify`
and `--uninstall`. New users normally receive it through `scripts/install_aqg.sh`;
they must restart Codex and review/trust the command hooks before activation.

### Claude Code

Claude Code supports filesystem skills with `SKILL.md`, personal skills under `~/.claude/skills/<skill-name>/SKILL.md`, project skills under `.claude/skills/<skill-name>/SKILL.md`, and skills can include bundled scripts and supporting files. Claude Code skills follow the Agent Skills standard, so AQG should keep skill bodies portable where possible and isolate Claude Code-specific extensions. Claude Code also supports project hooks in `.claude/settings.json`, where hook commands can refer to `$CLAUDE_PROJECT_DIR`.

Claude Code compatibility is implemented as a separate agent pack at `agent-packs/claude-code/`:

- it installs `.claude/skills/<skill-name>/SKILL.md` entries (all 16 `aqg-*` skills) that point to the shared core scripts
- user-level installs default to the managed Hook set through
  `scripts/install_aqg.sh`; the never-blocking Claude example remains an
  explicit `--warn-only-hooks` choice, and Codex has no verified equivalent
- it does not assume Claude Code loads Codex `agents/openai.yaml`; that metadata is Codex-specific

Pack layout:

```text
agent-packs/claude-code/
├── skills/          # all 16 aqg-* skills (SKILL.md each)
├── hooks/           # managed + example hook scripts + settings examples
└── install.sh
```

Claude Code adapter rules:

- skills should use `AQG` names to avoid conflict with existing generic `/debug` or `/review` workflows
- skills should call scripts via `$CLAUDE_SKILL_DIR` or a configured AQG root, not absolute machine paths
- hook examples must use `$CLAUDE_PROJECT_DIR` for project-relative paths
- hook examples must document blocking behavior explicitly; Claude Code command hooks generally use exit code `2` or structured deny/block JSON for blocking, so warn-only examples must not accidentally return either signal
- project hooks must not be installed automatically into target repos without explicit user action

### Future Agent Packs

Future agents should be added only when they can consume the same core contracts:

- read `quality-gates.json`
- run the same gate scripts
- produce the same evidence ledger shape
- preserve redaction and private-data rules
- keep warn-only as the default onboarding mode

## Packaging Layout

The current product layout:

```text
agent-quality-gates/
├── skills/               # Codex skills (aqg-*)
├── agent-packs/
│   ├── claude-code/      # Claude Code skills + hooks
│   └── codex/            # Codex hooks only; shared bodies remain under claude-code/hooks
├── scripts/              # agent-neutral core scripts
├── examples/ · docs/ · templates/
└── quality-gates.json    # AQG's own self-adopted config
```

Compatibility rules:

- the `skills/` path stays the stable Codex install surface
- one-line install commands stay stable; any change ships with a migration note

## Compatibility Acceptance Criteria

Agent compatibility is not complete until:

- Codex install still works
- Claude Code install can place skills under a chosen personal or project `.claude/skills` directory
- every supported host invokes the same core scripts for equivalent tasks, whether through an agent pack or a thin adapter
- CI adapter behavior is independent of which agent authored the PR
- `quality-gates.json` remains the source of project policy truth
- docs explain which files are agent-neutral and which are agent-specific

## Non-Goals

- Do not implement separate gate semantics per agent.
- Do not auto-install Claude Code hooks into a project without explicit user action.
- Do not depend on production credentials or target-repo branch protection.

## References

- Claude Code skills: https://code.claude.com/docs/en/skills
- Claude Code hooks: https://code.claude.com/docs/en/hooks
