# Recommended Complementary Tool Stack

English | [中文](ECOSYSTEM_MUST_INSTALL.zh-CN.md)

> Scope: every AQG-supported host agent (see the supported-client matrix in the README)
>
> Agent Quality Gates (AQG) is the engineering-discipline layer, not a full toolchain. This document lists the tools that **complement** AQG well — deterministic checks, production feedback, and a PR/CI-level evidence chain. The principle is **few but solid**: don't chase installing the most skills / plugins; lock quality capabilities into the construction path (constraints before acting → verification during implementation → PR/CI/production feedback closed loop). Install only what your workflow needs.

---

## Table of Contents

- [Background and Principles](#background-and-principles)
- [Complementary Tools (Cross-Agent)](#complementary-tools-cross-agent)
- [Complementary Tools (Claude Code)](#complementary-tools-claude-code)
- [Complementary Tools (Codex)](#complementary-tools-codex)
- [Harness Features (No Install Needed but Worth Knowing)](#harness-features-no-install-needed-but-worth-knowing)
- [Worth Evaluating (Try Before Deciding)](#worth-evaluating-try-before-deciding)
- [Explicitly Not Recommended](#explicitly-not-recommended)
- [Reference Sources (Borrow, Not Adopt)](#reference-sources-borrow-not-adopt)

---

## Background and Principles

### Selection Principles

1. **Don't pile up quantity**: driven by capability gaps, not by star count
2. **Don't duplicate AQG**: these tools are complementary, not a replacement
3. **Don't introduce whole methodologies that conflict with AQG** (superpowers / spec-kit / BMAD are all references, not replacements)
4. **Priority**: security + reproducible verification > production feedback > collaboration/knowledge flow
5. **Cross-agent consistency**: an item is either useful across the supported host agents (cross-agent), or explicitly marked with the host(s) it is limited to

### What AQG Deliberately Leaves to Complementary Tools

AQG provides the discipline gates (preflight, code construction, systematic debugging, audit adjudication, evidence closeout, skill validation) but deliberately does not ship:

- **deterministic checks** (lint / SAST / typing) — LLM audits miss low-level rules
- **PR / CI-level evidence chain** — the local closeout ledger stops at the local machine
- **production anomaly signals** (raw Actions logs / error tracking) — during debugging you often only see status, not logs

The tools below fill those gaps.

---

## Complementary Tools (Cross-Agent)

> Beneficial across the supported host agents.

### 1. Sentry (Production Anomalies → Feedback to Debugging)

**Value**: pulls real production errors back to the input side of `aqg-systematic-debugging`, avoiding judging quality based solely on local tests. Sentry's remote MCP comes with Seer AI root-cause analysis, serving as an **independent signal source**.

**Claude Code side**:
```bash
claude mcp add --transport http sentry https://mcp.sentry.dev/mcp
# First connection goes through OAuth, no manual token configuration needed
```

**Codex side**: install the Sentry plugin (OpenAI marketplace).

**When to enable**: enable when any service reaches staging / prod; can be deferred during the pure local script phase.

**Risk**: the remote MCP requires external network access; self-hosting Sentry requires switching to sentry-mcp-stdio.

---

### 2. Security Three Layers (threat model + SAST + PR-level review)

A generic LLM security-review skill (a within-session manual pass) is a good start, but the durable setup adds three layers:

**Layer A — Before writing code (threat model thinking)**: on the Codex side, install OpenAI's official security three-piece set
```
codex-security or install separately:
  - security-threat-model
  - security-best-practices
  - security-ownership-map
```

**Layer B — During writing code (deterministic SAST)**: install semgrep on the Claude Code side
```bash
/plugin install semgrep@claude-plugins-official
```
Function: scans OWASP / injection / hardcoded secrets. **Complementary to LLM audits, not overlapping** — semgrep is rule-deterministic, LLM audits are semantic. Run them in two stages before closeout. **No account needed**: semgrep runs fully locally against local / OSS rulesets (`semgrep --config p/owasp-top-ten`); a `SEMGREP_APP_TOKEN` is only for the optional hosted Semgrep platform.

**Layer C — PR-level (CI automated gate)**: see the `claude-code-security-review` GitHub Action in [Worth Evaluating](#worth-evaluating-try-before-deciding).

---

### 3. Playwright (Reproducible Frontend Verification)

**Value**: upgrades visual checks from "screenshot impressions" to replayable scripts. When you do UI work, it directly produces scripts + traces that can enter the evidence ledger.

**Claude Code side**: install the Playwright MCP (or a plugin bundle that includes it) and it is available session-wide.

**Codex side**: install the OpenAI curated `playwright` skill.

**When to enable**: when any task involves frontend / dashboard rendering.

---

## Complementary Tools (Claude Code)

### 4. agent-sdk-dev + mcp-server-dev + hookify (Anthropic Official)

When you write SDK-level subagents / your own MCP server / complex hooks, these three are authoritative official references, with zero API keys and zero prod risk:

```bash
/plugin install agent-sdk-dev@claude-plugins-official
/plugin install mcp-server-dev@claude-plugins-official
/plugin install hookify@claude-plugins-official
```

**hookify is especially useful** if your rules mandate "automated behaviors must land in hooks" — hookify does exactly this.

---

### 5. pyright-lsp (Python Type Feedback)

The AQG skill set includes a large number of Python helpers (`aqg_preflight.py` / `debug_case.py` / `validate_*.py` / `self_test.py`). **An after-the-fact LLM review catches semantics, while pyright gives deterministic write-time type feedback**; the two are complementary.

```bash
/plugin install pyright-lsp@claude-plugins-official
npm i -g pyright   # the local machine needs pyright-langserver
```

---

### 6. GitHub Official MCP (Replacing the Plugin Version)

**Key finding**: the plugin version of GitHub cannot get Actions raw job logs. The official `github/github-mcp-server` additionally provides `get_workflow_run_logs` / `list_workflow_runs` / `get_workflow_run` — precisely the **most scarce capability** for `aqg-systematic-debugging` when CI fails.

```bash
claude mcp remove github   # uninstall the plugin version to avoid tool-name conflicts
claude mcp add --transport http github-official https://api.githubcopilot.com/mcp
```

The `mcp__plugin_*__github__*` namespace and `mcp__github-official__*` do not conflict and can coexist, so you can also keep both if you prefer.

---

### 7. agnix (Claude Config lint / LSP)

416 rules covering SKILL.md / CLAUDE.md / hooks / MCP config (Claude Code 53 + Agent Skills 31 + MCP 12), with three-level auto-fix. **Complementary to `aqg-skill-validator`**: agnix manages syntax/formatting, aqg-skill-validator manages AQG business boundaries.

```bash
brew install agent-sh/tap/agnix
```

Rollout: in pre-commit, run agnix first → then aqg-skill-validator.

---

## Complementary Tools (Codex)

### 8. Codex Security Three-Piece Set
See Layer A in [Cross-Agent #2](#2-security-three-layers-threat-model--sast--pr-level-review).

### 9. Figma plugin (If Doing UI)

The Codex side ships `frontend-skill` and `figma-implement-design`. For high-quality UI, add:
- `figma-use`
- `figma-generate-library`
- `figma-create-design-system-rules`

**When to enable**: when a project needs a design system / multi-surface UI. Defer for non-UI projects.

### 10. Playwright (curated)
See [Cross-Agent #3](#3-playwright-reproducible-frontend-verification).

---

## Harness Features (No Install Needed but Worth Knowing)

New things in the Claude Code CLI itself (not plugins), directly relevant to a gated workflow:

### F1. New Hook Types
| Hook | Trigger | Usage |
|---|---|---|
| `PreCompact` | before context compaction | force writing the evidence ledger before compacting |
| `SubagentStart` / `SubagentStop` | subagent start/stop | subtask boundary auditing |
| `TaskCreated` / `TaskCompleted` | triggered by TodoWrite | automatic follow-up on task closure |
| `PermissionDenied` | after auto mode is denied | audit before retry |
| `InstructionsLoaded` | CLAUDE.md / rule loaded | detect successful rule injection |
| `FileChanged` / `CwdChanged` | file / cwd change | run preflight on project switch |

### F2. `/reload-plugins` Live Hot-Reload
Writing a new AQG skill doesn't require restarting the session; just `/reload-plugins` and it takes effect.

### F3. `SLASH_COMMAND_TOOL_CHAR_BUDGET`
A large installed skill set can exceed the default 1% truncation threshold; if slash-command descriptions get truncated, raise the budget in `~/.zshrc`:
```bash
export SLASH_COMMAND_TOOL_CHAR_BUDGET=20000
```

### F4. Subagent `preload-skills` + `isolation: worktree`
Writing `skills: [aqg-startup-preflight, aqg-evidence-closeout]` in the subagent frontmatter directly pre-injects them, more deterministic than relying on description auto-matching.

---

## Worth Evaluating (Try Before Deciding)

### claude-code-security-review (Anthropic Official PR Security GitHub Action)
- **Value**: CI-level automated PR security gate, differential scanning + 10 major vulnerability categories + automatic false-positive filtering + PR line-level comments → directly enters the evidence ledger
- **Limitation**: billed by token (not via subscription), and only for reviewing trusted PRs (no prompt injection protection)
- **Trial**: first dry-run for one week on a single repo

### anthropic-skills:example-skills
- The ones genuinely worth installing: `doc-coauthoring` (writing specs), `internal-comms` (status report templates), `brand-guidelines`
- Skip: canvas-design / slack-gif-creator / algorithmic-art

```bash
/plugin marketplace add anthropics/skills
/plugin install example-skills@anthropic-agent-skills
```

### promptfoo (Programmatic eval + red team)
- Write 1-2 evals for the accept/reject decision accuracy of `aqg-audit-adjudication`
- The evidence ledger directly consumes promptfoo JSON as evidence
- Used by both OpenAI and Anthropic

```bash
brew install promptfoo
```

### trailofbits/skills (Deep Security Audit)
- **Don't install all of it**; selectively adapt
- Only bring in the corresponding skill when actually doing cryptographic timing analysis / CodeQL/Semgrep rule writing / smart contract auditing

---

## Explicitly Not Recommended

| Item | Reason |
|---|---|
| obra/superpowers full suite | the complete brainstorm → plan → subagent → TDD methodology would clash with the AQG discipline gates. **Only borrow the ideas, don't adopt** |
| github/spec-kit, BMAD-METHOD | large-slice / heavy-spec methodologies; not needed by default; reference temporarily if a new system calls for it |
| CodeRabbit | overlaps with an LLM audit layer on review tasks; adding another layer is noise |
| letta-code / claude-mem | letta is a standalone harness (high migration cost); a persistent-memory plugin typically overlaps whatever memory layer you already run |
| filesystem MCP / archived postgres MCP | CVE-2025-53109/53110 symlink privilege escalation + SQL injection are known vulnerabilities |
| Datadog / Vercel / Cloudflare / Snowflake / Pinecone connectors | for a local-first dev tooling project with no corresponding SaaS path |
| any high-star skill randomly installed from GitHub | you must first read the `SKILL.md` / scripts / hooks / permission boundaries. **The Trail of Bits curated marketplace has explicitly warned of malicious hooks/scripts risk** |

---

## Reference Sources (Borrow, Not Adopt)

These are sources **worth reading the code / patterns of** when designing subsequent AQG skills, but are not directly installed:

| Repository | Borrowing Point |
|---|---|
| [openai/skills](https://github.com/openai/skills) curated | OpenAI's official skill authoring conventions; the `.curated` directory is high quality |
| [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) | spec / incremental implementation / TDD / debugging / review / security / performance / ADR — provides reference structures for future AQG extensions |
| [trailofbits/skills](https://github.com/trailofbits/skills) + [skills-curated](https://github.com/trailofbits/skills-curated) | differential review, static analysis, supply-chain risk, property/mutation testing — selectively adapt |
| [obra/superpowers](https://github.com/obra/superpowers) | TDD / worktree / verification-before-completion / review flow ideas (**borrow only**, don't adopt the full suite) |
| [anthropics/skills](https://github.com/anthropics/skills) | official skill authoring standards + frontmatter conventions |
| [anthropics/claude-cookbooks](https://github.com/anthropics/claude-cookbooks) `patterns/agents/` | basic_workflows / evaluator_optimizer / orchestrator_workers notebooks, for understanding classic multi-agent patterns |

---

## Maintenance

- Review this list periodically
- A new tool should run for a trial period in the "Worth Evaluating" section before entering the recommended set
- Before installing any GitHub third-party skill, you must read `SKILL.md` + `scripts/` + `hooks/`; blind installation is prohibited
