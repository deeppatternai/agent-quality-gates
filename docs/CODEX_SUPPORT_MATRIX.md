# Codex support matrix

Verified against the current Codex hooks documentation on 2026-07-23:

- [Hooks](https://learn.chatgpt.com/docs/hooks)
- [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Memories](https://learn.chatgpt.com/docs/customization/memories)
- [Model Context Protocol](https://developers.openai.com/codex/mcp)

## Client capability discovery

| Field | Finding | Evidence |
|---|---|---|
| Client identity | OpenAI Codex local clients (CLI, IDE extension, and desktop app local agent runtime) | Codex manual; hooks documentation |
| Official documentation | Codex documents lifecycle hooks, `AGENTS.md`, skills, and MCP | Official links above |
| User configuration | `${CODEX_HOME:-~/.codex}/hooks.json`, `config.toml`, `AGENTS.md`, `skills/`, and generated `memories/` state | Hooks and Memories docs; `scripts/install.sh`; `scripts/install_aqg_codex_hooks.py` |
| Project configuration | `<repo>/.codex/hooks.json`, `<repo>/.codex/config.toml`, and project `AGENTS.md`; project `.codex` layers require trust | Hooks docs |
| Skills | yes | `scripts/install.sh` installs `skills/aqg-*` into `$CODEX_HOME/skills` |
| Rules / instructions | yes | `examples/aqg-codex-agents.example.md` targets `~/.codex/AGENTS.md` |
| MCP / connectors | yes at the Codex platform; not modified by the lifecycle-hook installer | Codex MCP docs; the installer owns only `hooks.json` |
| Lifecycle hooks | yes | Codex hooks docs; `scripts/install_aqg_codex_hooks.py` |
| Hook events used | `SessionStart`, `PreToolUse`, `PostToolUse`, `PreCompact`, `UserPromptSubmit`, `Stop` | `_aqg_hook_specs()` |
| Hook input schema | JSON on stdin with common fields plus event-specific `tool_name`, `tool_input`, `tool_response`, `prompt`, or `stop_hook_active` | Hooks docs; `scripts/run_aqg_codex_hook.py` |
| Hook output schema | `hookSpecificOutput`, `permissionDecision: deny`, `additionalContext`, and one-shot Stop `decision: block` | Hooks docs; `scripts/run_aqg_codex_hook.py` |
| Block shell / tool calls | yes for supported local tool paths | Codex `PreToolUse` deny contract; `tests/test_codex_hooks.py` |
| Inject model-visible context | yes | Codex `SessionStart`, `PostToolUse`, and `UserPromptSubmit` contracts |
| Trust / enable review | yes | Restart Codex and use `/hooks`; trust is recorded against the exact definition hash; managed commands include a runner+policy content digest |
| Startup events | yes | `SessionStart` sources include startup, resume, clear, and compact |
| File-edit hooks | yes for `apply_patch` (also matches `Edit` / `Write` aliases) | Codex hook tool-coverage table |
| Shell hooks | yes; shell and unified exec match as `Bash` | Codex hook tool-coverage table |
| Disable / uninstall | yes | `/hooks`, `[features].hooks = false`, and installer `--uninstall` |
| Merge strategy | backup-first, atomic write, preserve unrelated hook blocks, converge AQG-owned entries | `scripts/install_aqg_codex_hooks.py`; focused tests |
| Managed identity | exact runner/script argv plus `aqg-codex-v1` marker and content digest | `_owned_script()`, `_bundle_digest()`, and focused tests |
| Test fixture | temporary `hooks.json`, HOME, and CODEX_HOME only; never the real user home | `tests/test_codex_hooks.py` |

## AQG feature evidence

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| AQG skill discovery | supported | `scripts/install.sh` | Installs the Codex skill source into `$CODEX_HOME/skills`. |
| Persistent rules / instructions | supported | `examples/aqg-codex-agents.example.md`; `AI_SETUP.md` | Uses global or project `AGENTS.md`. |
| MCP / equivalent connectors | partial | Codex MCP docs | Codex supports MCP, but this adapter intentionally does not overwrite `config.toml`; the audit hub MCP remains a separate opt-in integration. |
| Session startup preflight | supported | Codex `SessionStart` schema; `sessionstart_preflight.sh`; adapter tests | Emits official `hookSpecificOutput.additionalContext` JSON. |
| WIP checkpoint recovery | supported | `wip_checkpoint_recover.sh` | Surfaces local checkpoint metadata without changing the worktree. |
| PreToolUse shell skill validator | supported | `pretooluse_bash_skill_validator.sh` | Can deny a supported `Bash` call. |
| PreToolUse shell / file secret scan | supported | `pretooluse_secret_scan.sh`; translation tests | Scans literal Bash commands and added `apply_patch` content; deliberately not an obfuscation detector. |
| PostToolUse shell failure debugging reminder | supported | `posttooluse_bash_error_debugging_reminder.sh`; result normalization test | Non-zero exit codes become the legacy `is_error` contract. |
| PostToolUse code-construction reminder | supported | `posttooluse_code_construction_reminder.sh` | Each changed `apply_patch` path is translated separately. |
| PostToolUse test-quality reminder | supported | `posttooluse_test_quality_reminder.sh` | Model-visible `additionalContext`. |
| PostToolUse security-review reminder | supported | `posttooluse_security_review_reminder.sh` | Model-visible `additionalContext`. |
| AQG skill-edit validator reminder | supported | `posttooluse_skill_edit_reminder.sh` | Model-visible `additionalContext`. |
| PreCompact / Stop closeout reminder | supported | `precompact_closeout_reminder.sh`; Stop one-shot test | Only the closeout reminder can continue Stop once; `stop_hook_active` prevents a loop. |
| WIP checkpoint save | supported | `wip_checkpoint_save.sh`; Stop routing test | Saves to local `refs/aqg-wip/*`; status text never forces a continuation. |
| User-prompt handoff routing | supported | `userpromptsubmit_handoff_mandate.sh` | Selectively injects only for handoff intent. |
| Trust / enable / disable | supported | Codex `/hooks`; hooks docs | Non-managed hooks do not run until the exact definitions are trusted. |
| Verify / uninstall / is-installed | supported | `scripts/install_aqg_codex_hooks.py` | Read-only verification and owned-entry removal are covered by tests. |

## Support level and hard boundaries

AQG has **full Codex lifecycle support (`full`) for the documented local hook pipeline** after the user reviews and trusts the installed definitions. The adapter is backup-first, idempotent, uninstallable, and preserves unrelated hooks.

Repository verification uses temporary `HOME` / `CODEX_HOME` fixtures and the published Codex hook schemas. It does not install into or trust hooks in the operator's real Codex home.

The following boundaries remain explicit:

- Hosted tools such as `WebSearch` do not enter the local function-tool hook path.
- Codex documents that specialized tool paths can opt out; hooks are a guardrail, not a complete security boundary.
- Static secret scanning detects literal values, not deliberately obfuscated or indirect writes.
- The memory-placement guard covers `apply_patch`; shell-mediated writes receive literal-secret scanning but not memory-path semantic enforcement.
- Runtime infrastructure degradation (missing Bash, timeout, stale digest) warns without denying every local shell/edit tool; policy-script exit `2` remains the only deny signal.
- User-level hooks can be disabled. Organization-enforced hooks require managed `requirements.toml` policy and external script distribution.
- MCP configuration is supported by Codex but remains a separate opt-in surface; this installer does not modify `config.toml`.
