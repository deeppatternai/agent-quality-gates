# AQG Automated Setup Guide (hand this to your AI agent to execute)

English | [中文](AI_SETUP.zh-CN.md)

> **How to use**: paste **this entire file** into your current AQG-supported AI coding client, then say
> "set up AQG according to this." The AI will autonomously install Agent Quality Gates (AQG) and configure the skills,
> hooks, and rule surfaces for every locally installed AQG-supported desktop client. This guide is **installed-supported by default**:
> it detects supported local client configuration roots and installs each supported / full / partial adapter it finds. You don't need to manually edit any file.
>
> Applies to: **first-time install** and **idempotent re-sync after a prior install** (after a template update, just feed it again to align, without stacking duplicates).

---

## Execution instructions for the AI agent

> AI: you are configuring AQG for the user. **Execute step by step, verify each step passes before moving to the next**; if any step fails, **stop and report** — do not force ahead.
> The user's request to install or re-sync AQG authorizes the selected managed skills, supported hooks, and AQG rule blocks. Install these silently without asking for additional confirmation, including for blocking hooks. Use `--no-hooks` only when the user explicitly requests it and the selected profiles support it.
> Silent means no extra installation prompts: retain dry-run checks, backups, verification, failure reporting, and client-required trust steps. Resolve source and scope prerequisites below before applying.
> Follow the [Boundaries] at the end throughout.

### 0. Prerequisites

- `git --version` and `python3 --version`. **python3 must be ≥ 3.9**; if only `python` exists or the version is too low → stop and have the user install python3 ≥3.9.
- Record `CURRENT_CLIENT` from the current AI runtime / host context for reporting and fallback, **not** for narrowing the default install target:
  - When running in Codex, set `CURRENT_CLIENT=codex`.
  - When running in Claude Code, set `CURRENT_CLIENT=claude-code`.
  - When running in Cursor, set `CURRENT_CLIENT=cursor`.
  - When running in a Qoder-family host, set `CURRENT_CLIENT` to the exact runtime profile only if it is reliably known: `qoder-cli` / `qoder-cli-cn` are full profiles; `qoder` / `qoder-cn` are partial IDE profiles.
  - When running in CodeBuddy, WorkBuddy AI, Kimi Code, WorkBuddy, QoderWork, or QoderWake, set `CURRENT_CLIENT` to the exact registry `client_id` only if it is reliably known: `codebuddy`, `workbuddy-ai`, `kimi-code`, `workbuddy`, `qoderwork`, or `qoderwake`. WorkBuddy AI (`workbuddy-ai`) is an independent product from legacy WorkBuddy and from CodeBuddy Agent CLI: its root is `~/.workbuddy-ai`, never `~/.workbuddy` or `~/.codebuddy`. TRAE Work is not a work-client profile: use `trae-work` with the agent-client installer.
  - When running in Pi, set `CURRENT_CLIENT=pi`.
  - When running in a Trae-family IDE host, set `CURRENT_CLIENT` only if the exact profile is known: `trae` / `trae-cn` are partial IDE profiles; `trae-work` / `trae-work-cn` are partial Work profiles.
  - When running in Zed, set `CURRENT_CLIENT=zed`.
  - When running in Devin CLI, set `CURRENT_CLIENT=devin`.
  - If you cannot reliably identify the current runtime / host context or Qoder profile → **stop and ask the user** which currently running client/profile should be configured. Do not guess from `~/.claude`, `~/.codex`, `~/.cursor`, `~/.qoder`, `~/.lingma`, `~/.qoder-cn`, or any other directory.

### 1. Locate / obtain the AQG checkout (call it `AQG_ROOT`)

Use a persistent checkout outside any project or task worktree. The recommended
location is `$HOME/.deeppattern/agent-quality-gates` on Unix/macOS/Linux and
`C:\Users\<user>\.deeppattern\agent-quality-gates` on Windows. The directory is
named `.deeppattern`, **not `.deeppatternai`**.

Probe in order; use the first **existing directory** that hits:

1. `$AQG_ROOT` (environment variable, if already set and the directory exists)
2. `$HOME/.deeppattern/agent-quality-gates` (development source repo)
3. `${XDG_DATA_HOME:-$HOME/.local/share}/aqg/the cloud backend` (release repo, if present)

**If none hit** → you need to obtain the source. If the user already supplied an
accessible AQG repository or documentation link that identifies the repository,
resolve the clone URL, show it to the user, and obtain confirmation before
cloning to the persistent path below. Otherwise ask
for the local path of an existing checkout or the git address of the AQG
distribution repo (private repos require the user to have access).

#### 1.1 Resolve the install source / checkout ref before clone, fetch, checkout, or pull

If the user gives a GitHub document URL, parse the repository source and the
document URL ref before touching git state. Preserve branch names with slashes:
for `https://github.com/deeppatternai/agent-quality-gates/blob/feature/v0.0.1/AI_SETUP.zh-CN.md`,
the `doc_ref` is `feature/v0.0.1` (everything between `/blob/` and the document
path), not the bare suffix `v0.0.1`.

Use these names consistently:

- `doc_ref`: the full ref from the document URL, for example `feature/v0.0.1`.
- `requested_version`: the short version explicitly requested by the user, for
  example `v0.0.1` or `v0.0.2`; leave it empty if the user did not request a
  separate version.
- `selected_checkout_ref`: the exact ref to use for `git clone --branch`,
  `git fetch`, `git checkout`, and `git pull --ff-only`.

Selection rules:

1. If `requested_version` is empty, `doc_ref` is authoritative:
   `selected_checkout_ref=doc_ref`.
2. If `requested_version` equals the version suffix of `doc_ref`,
   `doc_ref` is still authoritative. Do not degrade `feature/v0.0.1` to bare
   `v0.0.1` just because the user said "install branch: v0.0.1".
3. If `requested_version` is explicit and differs from the version suffix of
   `doc_ref`, treat the URL only as the repository source and document-path
   template. Prefer `feature/<requested_version>`.
4. Fallback is fail-closed:
   - If `feature/<requested_version>` exists, choose it.
   - If `feature/<requested_version>` does not exist but bare
     `<requested_version>` exists as a branch or tag, stop and ask the user to
     confirm before using it.
   - If neither exists, stop and report no matching install ref.
5. Use a bare version ref only when the user explicitly says not to use
   `feature/<version>` and to checkout bare `<version>`.

Before any `git clone`, `git fetch`, `git checkout`, or `git pull`, report this
resolution:

```text
doc_ref=<full URL ref or empty>
requested_version=<short user-requested version or empty>
selected_checkout_ref=<exact ref or empty if blocked>
selection_reason=<why this ref was selected or why the workflow stopped>
attempted_candidate_refs=[<refs checked in order>]
```

Examples:

| example | user input | resolved result / behavior |
|---|---|---|
| A | "Reinstall AQG; link is `blob/feature/v0.0.1/AI_SETUP.zh-CN.md`; install branch: `v0.0.1`." | `doc_ref=feature/v0.0.1`; `requested_version=v0.0.1`; `selected_checkout_ref=feature/v0.0.1`; reason: the short requested version matches the `doc_ref` version suffix, so the full URL ref wins. |
| B | "Install `v0.0.2`; link is `blob/feature/v0.0.1/AI_SETUP.zh-CN.md`." The repository has `feature/v0.0.2`. | `doc_ref=feature/v0.0.1`; `requested_version=v0.0.2`; `selected_checkout_ref=feature/v0.0.2`; reason: the user explicitly requested a newer version, so the URL is only the repository source and document-path template. |
| C | Same as B, but the repository lacks `feature/v0.0.2` and has bare `v0.0.2`. | Stop and ask: `feature/v0.0.2 was not found, but v0.0.2 exists. Confirm checkout of bare v0.0.2?` |
| D | Same as B, but neither `feature/v0.0.2` nor `v0.0.2` exists. | Stop and report: `attempted_candidate_refs=[feature/v0.0.2, v0.0.2]`; `result=no matching install ref found`. |

After checkout, strongly validate the selected ref before running installer
commands:

```bash
git -C "$AQG_ROOT" rev-parse --abbrev-ref HEAD
git -C "$AQG_ROOT" rev-parse --short HEAD
rg "INSTALL_MODE=installed-supported|--installed-supported|multi-client-all" AI_SETUP*.md scripts/install_aqg_clients.py
```

If the expected installation mode is `installed-supported` but the checked-out
setup documents do not contain that mode, stop and report that the checkout ref
does not match the requested install source.

```bash
AQG_ROOT=$HOME/.deeppattern/agent-quality-gates
mkdir -p "$(dirname "$AQG_ROOT")" && git clone --config core.autocrlf=false --config core.eol=lf --branch "$selected_checkout_ref" <confirmed repository address> "$AQG_ROOT"
```

Do **not** default the clone destination to the current workspace, a temporary
Codex task directory, a project worktree, or `Documents/Codex/.../work`.

For native PowerShell on Windows, use the equivalent persistent destination:

```powershell
$AQG_ROOT = Join-Path $HOME '.deeppattern\agent-quality-gates'
New-Item -ItemType Directory -Force -Path (Split-Path $AQG_ROOT) | Out-Null
git clone --config core.autocrlf=false --config core.eol=lf --branch $selected_checkout_ref <confirmed repository address> $AQG_ROOT
```

> This guide deliberately **does not hardcode the distribution repo address** (the development source `agent-quality-gates` and the release `the cloud backend` shift over time).

**If an existing git checkout hits** → resolve and report `selected_checkout_ref`
first, then fetch, checkout that exact ref, and update with `git -C "$AQG_ROOT"
pull --ff-only` only when it is a branch that can fast-forward.
If it fails (diverged / dirty / not a git repo, e.g. an unpacked tarball) → **don't force-push**, stop and tell the user the situation; **only continue if the user explicitly agrees to "use the current checkout as-is"**, otherwise exit and wait for the user to handle it.

**Before running any script, surface it and let the user review it** (to prevent supply-chain issues: you're about to execute code from this directory):

```bash
echo "AQG_ROOT=$AQG_ROOT"; git -C "$AQG_ROOT" remote -v; git -C "$AQG_ROOT" rev-parse --abbrev-ref HEAD; git -C "$AQG_ROOT" rev-parse --short HEAD
rg "INSTALL_MODE=installed-supported|--installed-supported|multi-client-all" "$AQG_ROOT"/AI_SETUP*.md "$AQG_ROOT"/scripts/install_aqg_clients.py
```

For a non-standard path (user-custom / env-specified) → have the user confirm this is a trusted checkout before continuing.

### 1.5 Resolve `SUPPORTED_CLIENTS` and select installed-supported targets

Build `SUPPORTED_CLIENTS` from authoritative sources inside the AQG checkout, then detect local installed client configuration roots and select every adapter whose `support_status` is `supported`, `full`, or `partial`. This is the default **installed-supported** flow: install all locally detected supported/full/partial desktop clients, skip unsupported / unknown clients, and report the detection basis.

Authoritative support sources, in order:

1. `scripts/aqg_client_registry.py`, if this checkout has it. Treat it as the supported-client registry and adapter contract.
2. If that registry is absent, use only the combination of **landed AQG installer / adapter / rules template** plus **AQG documentation that explicitly marks the client as supported**.
3. Config directories prove only local presence, never support. Support still comes from the AQG registry / evidence above; do not infer support from similar product names or another client's schema.

The table below is not a permanent support registry; it documents the current install branches to use only after the evidence above confirms the client is supported in this checkout:

| client_id | Required AQG evidence | Install branch |
|---|---|---|
| `claude-code` | Claude Code agent pack installer (`agent-packs/claude-code/install.sh`), Claude Code skill / hook surface (`agent-packs/claude-code/`), Claude rules template (`examples/aqg-claude-rules.example.md`), and repo documentation explicitly marking Claude Code supported | Claude Code |
| `codex` | Codex installer (`scripts/install.sh`), Codex hook installer (`scripts/install_aqg_codex_hooks.py`), Codex skill surface (`skills/`), Codex rules template (`examples/aqg-codex-agents.example.md`), and repo documentation explicitly marking Codex supported | Codex |
| `cursor` | Cursor support installer (`scripts/install_cursor_support.py`), Cursor hook / rule surface managed by that installer, and repo documentation explicitly marking Cursor supported | Cursor |
| `workbuddy` / `workbuddy-ai` / `codebuddy` / `kimi-work` / `kimi-code` / `qoderwork` / `qoderwake` | Work/code client installer (`scripts/install_aqg_work_clients.py`), support report / rules / skills / MCP / hook surfaces per `docs/client-support-matrix.zh-CN.md`, and registry `support_status` declarations. `workbuddy-ai` is the independent WorkBuddy AI Desktop profile (`~/.workbuddy-ai`, bundle id `com.workbuddy.workbuddy-ai`); it does not share a root with `workbuddy` or `codebuddy`. | Work/Code clients |
| `qoder-cli` / `qoder-cli-cn` | Qoder support installer (`scripts/install_aqg_qoder.py`), Qoder agent pack / hook surface (`agent-packs/qoder/`), Qoder CLI rule surfaces managed by the installer, and repo documentation marking the CLI profiles full | Qoder CLI |
| `qoder` / `qoder-cn` | Qoder support installer (`scripts/install_aqg_qoder.py`), Qoder agent pack / hook surface (`agent-packs/qoder/`), exact macOS identities (`Qoder.app` / `com.qoder.app` or `Qoder IDE.app` / `com.qoder.ide`; `Qoder CN.app` / `com.qodercn.app` or `Qoder CN IDE.app` / `com.aliyun.lingma.ide`), and registry evidence marking the IDE profiles partial | Qoder Desktop |
| `trae` / `trae-cn` | Agent-client support installer (`scripts/install_aqg_agent_clients.py`), hook adapter (`scripts/agent_client_aqg_hook.py`), Trae official Skills/Rules/MCP/Hooks evidence, and `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md` marking the IDE profiles partial | Trae IDE |
| `trae-work` / `trae-work-cn` | Agent-client support installer (`scripts/install_aqg_agent_clients.py`), Trae Work Skills/Rules/MCP evidence, and `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md` marking the Work profiles partial because lifecycle-hook schema is not verified. `trae-work` shares `~/.trae/skills` with `trae`; `trae-work-cn` shares `~/.trae-cn/skills` with `trae-cn`. `~/.qoder-cn` is the Qoder CN Desktop root, and `.lingma` is not a Qoder CN install target. | Trae Work / Work CN |
| `zed` | Agent-client support installer (`scripts/install_aqg_agent_clients.py`), Zed Skills/Instructions/MCP evidence, and `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md` marking Zed partial because lifecycle hooks are unavailable | Zed |
| `devin` | Agent-client support installer (`scripts/install_aqg_agent_clients.py`), hook adapter (`scripts/agent_client_aqg_hook.py`), Devin Rules/Skills/MCP/Hooks evidence, and `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md` marking Devin partial because PreCompact-before-compaction is unavailable | Devin CLI |
| `pi` | Pi support installer (`scripts/install_aqg_pi.py`), Pi hook runner (`scripts/pi_aqg_hook.py`), Pi official Skills/Extensions evidence, and `docs/client-support-matrix.zh-CN.md` marking Pi partial because MCP/connectors are unsupported and closeout/WIP save depend on extension lifecycle delivery | Pi |

Known non-target statuses:

| client_id | Status | Action |
|---|---|---|
| `qoder` / `qoder-cn` | partial | Include in the default installed-supported target set when an accepted exact macOS bundle identity is detected; aliases sharing one profile select it only once. Report the missing `SessionStart`, `PreCompact`, and WIP save/recover coverage plus the AQG evidence source. |
| `workbuddy` / `kimi-work` / `kimi-code` / `qoderwork` / `qoderwake` | partial | Include when locally detected; report feature-level degradation from `docs/client-support-matrix.zh-CN.md`. `kimi-work` means Kimi Work Desktop / Kimi Desktop local Daimon skills root; `kimi-code` means Kimi Code CLI under `~/.kimi-code`. |
| `trae` / `trae-cn` | partial | Include when locally detected; report missing `PreCompact`, Stop-only WIP save, and UI-managed user rules. |
| `trae-work` / `trae-work-cn` | partial | Include when locally detected via the `TRAE SOLO.app` / `TRAE SOLO CN.app` bundle identity; report that Work lifecycle hooks are not verified, so only skills/project rules are managed, and that the skills root is shared with `trae` / `trae-cn`. |
| `zed` | partial | Include when locally detected; report that lifecycle hooks are unavailable, so mechanical gates are not installed. |
| `devin` | partial | Include when locally detected; report missing PreCompact-before-compaction and Stop-only WIP save. |
| `pi` | partial | Include when locally detected; report MCP/connectors unsupported and lifecycle delivery/trust caveats from `docs/client-support-matrix.zh-CN.md`. |

Only add a future client row after the AQG checkout contains its formal adapter / installer / rules template / capability evidence and a supported/full/partial status declaration. Manual-only, experimental, unsupported, or unknown clients are **not** install targets for this guide.

Selection rules:

- Default target set: run `scripts/install_aqg_clients.py --installed-supported` and use the wrapper plan as `SELECTED_CLIENTS`.
- Install and configure every client in `SELECTED_CLIENTS`; do not narrow to only `CURRENT_CLIENT`.
- If no locally installed supported/full/partial client is detected, stop before writing anything and report the empty detected set plus the AQG evidence source.
- Unsupported / unknown clients are not default install targets even if their config directories exist.
- Configure each selected client's owned rule surface: Codex -> `AGENTS.md`, Claude Code -> `CLAUDE.md`, Cursor/Qoder CLI/work-code clients -> the rule surface owned by that selected installer and scope.
- Project scope remains explicit: only use `--project-root` after the user confirms an existing absolute `PROJECT_ROOT`.
Default mode is `INSTALL_MODE=installed-supported`: dry-run `scripts/install_aqg_clients.py --installed-supported` first, validate the plan and required source/scope inputs, then automatically apply the same detected supported/full/partial set without asking the user to confirm the plan or hooks again. Supported lifecycle hooks are installed/refreshed by default for selected clients; pass `--no-hooks` only when the user explicitly chooses a skills/rules-only install and every selected profile can honor it. If the user explicitly wants every AQG registry adapter installed in one batch regardless of support-status label, switch to `INSTALL_MODE=multi-client-all` and use `scripts/install_aqg_clients.py --all-registry`. `--all-supported` remains a backward-compatible alias for the same registry-all mode.

Default `installed-supported` flow:

```bash
# Dry-run first. This detects local config roots for supported/full/partial registry clients
# such as ~/.codex, ~/.claude, ~/.cursor, ~/.qoder, ~/.qoder-cn,
# ~/.trae, ~/.trae-cn, ~/.trae-work-cn, ~/.config/zed, ~/.config/devin,
# ~/.pi/agent, and Kimi Work Desktop / Kimi Desktop Daimon skills roots
# discovered from KIMI_WORK_SKILLS_ROOT / KIMI_DESKTOP_SKILLS_ROOT,
# running Daimon process command lines, Kimi Desktop logs, .rehomed markers,
# or Kimi.exe install-drive evidence.
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT"

# Validate AQG_ROOT, remote, commit, detected/skipped clients, scope, and commands.
# Once prerequisites are satisfied, apply automatically; no extra hook/plan confirmation.
# Add --project-root when the plan includes project-scope adapters such as
# Qoder IDE partial profiles.
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT" --apply
# python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT" --project-root "/absolute/path/to/project" --apply
```
Optional `multi-client-all` flow:

```bash
# Dry-run first. This expands every adapter in scripts/aqg_client_registry.py;
# it does not inspect ~/.claude, ~/.codex, ~/.cursor, ~/.qoder, ~/.trae,
# ~/.trae-cn, ~/.trae-work, ~/.trae-work-cn, ~/.config/zed, ~/.config/devin,
# ~/.pi/agent, or any other config directory.
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --all-registry --aqg-root "$AQG_ROOT"

# Validate AQG_ROOT, remote, commit, clients, scope, and commands, then apply
# automatically within the requested registry-all scope. Use --project-root when the plan
# includes project-scope adapters such as Qoder IDE profiles.
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --all-registry --aqg-root "$AQG_ROOT" --project-root "/absolute/path/to/project" --apply
```

Wrapper exit codes (stable contract for callers such as decision-engine's `install.sh`):

| Code | Meaning | Caller action |
| --- | --- | --- |
| `0` | every selected client succeeded | continue |
| `2` | argument / client-selection error | stop and fix the invocation |
| `3` | no supported client detected on this host | continue; there is nothing to configure |
| `4` | some clients failed, at least one succeeded | warn, report the failed client ids, continue |
| `5` | every selected client failed | warn, report the failed client ids, continue |

Client-level failures are isolated per client id: a client is one atomic unit (skills install plus its hook install), so a failing client skips its own remaining commands while every other client still installs. Child process return codes are never propagated, so `4` / `5` always mean "a client failed" and never collide with `2`. Failed client ids are printed in the `- results:` summary and on stderr.

In `multi-client-all`, report every selected registry client plus its support-status label from the wrapper plan. Partial / unsupported / unknown labels are still selected because this mode is registry-all, but any degraded or uncertain coverage shown by the registry evidence must be reported before automatic apply; do not pause for another installation confirmation. Do not pass `--no-hooks` unless the displayed client set can honor it; Qoder-family profiles currently fail closed because their installer has no `--no-hooks` mode.

### 2. Install managed client support for `SELECTED_CLIENTS` (idempotent)

Default path: run the wrapper command group for every client in `SELECTED_CLIENTS`. The commands use repo-relative paths, so they work regardless of which repo the toolkit lives in. The per-client branches below are the explicit fallback/reference commands when the wrapper is unavailable or when the user deliberately chooses one client.

Only if Cursor or Qoder CLI project scope is selected, replace and validate `PROJECT_ROOT`:

```bash
PROJECT_ROOT="/absolute/path/to/project"                 # project scope only: replace with the confirmed path
test -d "$PROJECT_ROOT" || { echo "PROJECT_ROOT does not exist" >&2; exit 2; }
```

If `CURRENT_CLIENT=codex`:

```bash
# Silently install supported hooks by default, including 4 blocking policies,
# and update the client's hook configuration. Use --no-hooks
# only when the user explicitly chooses a skills-only install.
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --clients codex --aqg-root "$AQG_ROOT" --apply
```

Codex hook layout and activation are separate concerns:

1. The AQG Codex installer writes managed definitions into `~/.codex/hooks.json`
   by default. Use `scripts/install.sh --no-hooks` only for an explicit
   skills-only install; custom `--dest` installs skip hooks unless `--hooks` is
   passed.
   Do not expect a `~/.codex/hooks/` directory. The definitions invoke
   `scripts/run_aqg_codex_hook.py` as the Codex wrapper.
2. The wrapper currently reuses shared policy scripts from
   `agent-packs/claude-code/hooks/*.sh`. This is deliberate code reuse; it does
   not install or configure the Claude adapter in Codex.
3. Successful activation has three distinct layers: the installer wrote
   `~/.codex/hooks.json`; the Codex runtime discovered those definitions; and
   the user reviewed and trusted them through `/hooks`.

`install_aqg_codex_hooks.py --verify` and the doctor verify only the **on-disk**
AQG definition and integrity contract. They cannot prove that Codex Desktop has
discovered the hooks, cannot prove that the Settings page displays them, and
cannot replace the user's `/hooks` trust decision. When supported by the
installer, Codex installs a managed `UserPromptSubmit` handoff-routing hook; the
AGENTS.md rule remains the model-side fallback and explanation.

If `CURRENT_CLIENT=claude-code`:

```bash
# Claude Code skills + supported hooks (skills install to ~/.claude/skills; source is agent-packs/claude-code/skills, different from Codex)
# Pass --no-hooks only if the user explicitly chooses skills-only.
# Install silently: the hook pack includes 4 blocking gates (secret-scan / memory-write-guard /
# skill-validator / tamper-guard, all with an AQG_AGENT=human-opt-in escape); for a never-blocking
# version use settings.warn-only.example.json.
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --clients claude-code --aqg-root "$AQG_ROOT" --apply
```

If `CURRENT_CLIENT=cursor`:

```bash
# Cursor — choose ONE scope. By default this installs a fail-closed preToolUse
# hook without extra confirmation. Pass --no-hooks only if the user requests skills/rules only.
# Project scope also installs .cursor/rules/aqg.mdc. User Rules are UI-managed.
python3 "$AQG_ROOT/scripts/install_cursor_support.py" --apply --scope user --mode link
# python3 "$AQG_ROOT/scripts/install_cursor_support.py" --apply --scope project --project-root "$PROJECT_ROOT" --mode link
```

If `CURRENT_CLIENT` is one of `workbuddy`, `workbuddy-ai`, `codebuddy`, `trae-work`, `kimi-work`, `kimi-code`, `qoderwork`, or `qoderwake`:

```bash
WORK_CLIENT="$CURRENT_CLIENT"
case "$WORK_CLIENT" in workbuddy|workbuddy-ai|codebuddy|kimi-work|kimi-code|qoderwork|qoderwake) ;; *) echo "invalid WORK_CLIENT" >&2; exit 2;; esac

# Work/Code clients use per-profile support levels from docs/client-support-matrix.zh-CN.md.
# Degraded profiles install only documented surfaces; pass --no-hooks for skills/rules/MCP only.
# Kimi boundary: kimi-work = Kimi Work Desktop / Kimi Desktop local Daimon
# daimon-share/daimon/skills root, dynamically discovered and overrideable with
# --skills-root /absolute/path/to/skills. kimi-code = Kimi Code CLI under
# ~/.kimi-code; do not mix these install targets.
python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client "$WORK_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --apply
# python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client "$WORK_CLIENT" --scope project --project-root "$PROJECT_ROOT" --aqg-root "$AQG_ROOT" --mode link --apply
# Kimi Work link installs fall back to copy if symlink/junction creation fails;
# use --mode copy to copy directly, or --mode link --strict-link to fail closed.
# Restart Kimi Work / Kimi Desktop after apply so Daimon reloads AQG skills.
```

If `CURRENT_CLIENT=trae`, `trae-cn`, `trae-work-cn`, `zed`, or `devin`:

```bash
AGENT_CLIENT="$CURRENT_CLIENT"
case "$AGENT_CLIENT" in trae|trae-cn|trae-work|trae-work-cn|zed|devin) ;; *) echo "invalid AGENT_CLIENT" >&2; exit 2;; esac

# Trae/Zed/Devin profiles use docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md.
# Profiles with no verified lifecycle hooks install only skills/rules surfaces.
python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client "$AGENT_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --apply
# python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client "$AGENT_CLIENT" --scope project --project-root "$PROJECT_ROOT" --aqg-root "$AQG_ROOT" --mode link --apply
```

If `CURRENT_CLIENT=qoder-cli` or `CURRENT_CLIENT=qoder-cli-cn`:

```bash
QODER_CLIENT="$CURRENT_CLIENT"
case "$QODER_CLIENT" in qoder-cli|qoder-cli-cn) ;; *) echo "invalid QODER_CLIENT" >&2; exit 2;; esac

# Qoder CLI family — choose the confirmed full profile and ONE scope.
# The Qoder installer always manages hooks, including 2 blocking preToolUse gates,
# and has no --no-hooks mode. Install silently within the selected profile/scope.
python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client "$QODER_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --apply
# python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client "$QODER_CLIENT" --scope project --project-root "$PROJECT_ROOT" --aqg-root "$AQG_ROOT" --mode link --apply
```

If `CURRENT_CLIENT=pi`:

```bash
# Pi — installs skills plus a managed TypeScript extension for documented
# lifecycle events. Pass --no-hooks for skills/report only.
python3 "$AQG_ROOT/scripts/install_aqg_pi.py" --scope user --aqg-root "$AQG_ROOT" --mode link --apply
# python3 "$AQG_ROOT/scripts/install_aqg_pi.py" --scope project --project-root "$PROJECT_ROOT" --aqg-root "$AQG_ROOT" --mode link --apply
```

Then run:

```bash
# Health check
python3 "$AQG_ROOT/scripts/aqg_doctor.py"
```

If `aqg_doctor` reports **FAIL** → stop and fix per the hints, don't continue. (The doctor validates skills, basic environment, and Codex hook definitions on disk; it cannot confirm `/hooks` trust, and it does **not** validate the rules content of the next step's CLAUDE.md/AGENTS.md.)

> The Claude Code/Codex hook installers and Cursor/Qoder CLI/Work-Code/Trae-Zed-Devin/Pi support installers expose `--apply`, `--verify`, `--uninstall`, and `--is-installed`. Use the exact same client/profile and scope for every action. `aqg_doctor` does not replace Cursor/Qoder CLI/Work-Code/Trae-Zed-Devin/Pi `--verify`.

> Codex: restart after installation, run `/hooks`, review the exact definitions, and trust them before expecting non-managed hooks to run. The installer embeds the reviewed checkout and Python paths plus a runner+policy content digest, so it does not depend on shell inheritance of `AQG_ROOT`; re-run `--apply` after a path or hook-content update and review the changed definition hash.
>
> Claude Code: after hooks are installed you **must export `AQG_ROOT`** into the environment Claude Code runs in, otherwise they silent-skip. Restart Claude Code after installation.

#### Codex Desktop hook troubleshooting

If `~/.codex/hooks.json` exists but Codex Desktop Settings says no hooks were
found, fully quit and restart Codex Desktop first. Then run `/hooks` in a Codex
session; do not rely only on Settings. Review and trust the hook definitions in
`/hooks` before treating them as active. If `/hooks` still does not show them,
report a **Codex runtime discovery failure / possible schema drift** and include
the `~/.codex/hooks.json` path and Codex version in the report.

### 3. Install the current client's rules block (**critical — the always-resident channel**)

| Client | Target file `$target` | Template source `$tmpl` (under `$AQG_ROOT/`) |
|---|---|---|
| Claude Code | `~/.claude/CLAUDE.md` | `examples/aqg-claude-rules.example.md` |
| Codex | `~/.codex/AGENTS.md` | `examples/aqg-codex-agents.example.md` |

If `SELECTED_CLIENTS` contains `claude-code` or `codex`, update each matching target row. Do not modify a client's rules file unless that client is in `SELECTED_CLIENTS`.

> Do not run this section for Cursor, Qoder CLI, Work-Code, or Trae/Zed/Devin clients. Their installers own the supported rule surfaces. User-scope Cursor and Trae user rules intentionally remain UI-managed where the official product requires that.

**3.1 Install the block** — one command per selected client; the installer owns backup, idempotency and validation:

```bash
python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client claude-code --apply --aqg-root "$AQG_ROOT"
python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex        --apply --aqg-root "$AQG_ROOT"
```

Run only the line whose client is in `SELECTED_CLIENTS`. What the script guarantees, so you do not re-check it by hand:

- It creates `$target` when missing and **appends** the block when the file exists, leaving the user's own content in place.
- The region is delimited by `<!-- BEGIN AQG rules ... -->` / `<!-- END AQG rules -->`, so a re-run replaces **only** that region — content on both sides survives.
- `<AQG_ROOT>` is resolved to the checkout being installed from.
- The pre-existing file is stashed in the central backup store (`scripts/_aqg_backup.py`) before it is replaced; the run directory is printed.
- It refuses to write through a symlinked **rules file**, and refuses a half-deleted region rather than guessing where it ended. A symlinked *client directory* (a dotfiles layout) is followed by design.

**3.2 Handle a legacy unmarked block** — anyone who installed before this script has a block with no markers, pasted by an agent following the old hand-edit procedure. `--apply` **refuses** in that case rather than appending a second copy:

```
ERROR: found an unmarked legacy AQG block (no BEGIN/END markers) ...
```

Delete that section from `$target` by hand — its boundary runs from the `Agent Quality Gates (AQG) engineering discipline` heading to just before the next same-level or higher-level heading (or end of file) — then re-run 3.1. **Keep any subsection the user wrote themselves**; if you are unsure whether a subsection is user-authored, keep it and ask.

**3.3 Confirm** — the installer's own check, not a hand-written grep:

```bash
python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client <claude-code|codex> --verify --aqg-root "$AQG_ROOT"
```

`--verify` compares the installed region against the shipped template byte for byte, so it also catches a block written by an older version or edited by hand. A non-zero exit here means the block is absent, stale, or hand-edited — report it, do not paper over it.

### 4. Final verification

```bash
python3 "$AQG_ROOT/scripts/aqg_doctor.py"     # run again; Codex on-disk hook definitions are included
python3 "$AQG_ROOT/scripts/install_cursor_support.py" --verify --scope user --mode link  # if Cursor user scope was selected
python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client "$WORK_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --verify  # if a Work/Code profile was selected
python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client "$QODER_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --verify  # if Qoder CLI user scope was selected
python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client "$AGENT_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --verify  # if a Trae/Zed/Devin profile was selected
python3 "$AQG_ROOT/scripts/install_aqg_pi.py" --scope user --aqg-root "$AQG_ROOT" --mode link --verify  # if Pi user scope was selected
```

For project scope, repeat the matching `--project-root "$PROJECT_ROOT"` arguments used during `--apply`. Any failed verification stops the workflow. To inspect or remove a managed install later, use the same arguments with `--is-installed` or `--uninstall`.

For Cursor/Qoder CLI/Work-Code/Trae-Zed-Devin clients, also confirm their `--verify` output covers the profile's declared managed surfaces; do not use the manual rule-section checks from step 3.

Rules and hooks are loaded at client/session start. Restart every configured client in `SELECTED_CLIENTS`; Codex users must also review and trust the definitions through `/hooks`.

### 5. Report

Report the following: the detected `CURRENT_CLIENT`; the locally detected `SELECTED_CLIENTS`; the AQG support evidence source for each selected client; any skipped unsupported / unknown local clients; the selected scope when applicable; how many skills each got; which hooks/rules were managed; whether each Claude/Codex rule section was **newly added** or **re-synced** (3.3 a/b/c); every backup path reported by an installer/manual edit; verification results; and that a restart is required.

If other client directories exist but were not configured, say this is intentional only when their AQG registry status is unsupported / unknown or they were outside the confirmed scope. If `CURRENT_CLIENT` is not formally supported by AQG, report `unsupported` / `unknown`, cite the AQG evidence source, and still install any other locally detected supported/full/partial clients.

---

## Boundaries (the AI must obey)

- Only touch the **AQG section** of `CLAUDE.md` / `AGENTS.md` and paths explicitly owned by the selected managed installer; don't touch other user config, production, secrets, or branch protection.
- Configure every locally detected supported/full/partial adapter by default, and never claim support from directory presence alone: `~/.claude`, `~/.codex`, `~/.cursor`, `~/.qoder`, `~/.lingma`, `~/.qoder-cn`, or another config directory only indicate local presence; never apply one client's schema to another client.
- New client support declarations require landed AQG adapter / installer / rules template / capability evidence before this guide may install or configure them.
- **Back up before writing the rules file, validate zero changes outside the section after writing, roll back if it fails** (3.1 / 3.4); in case (c), **keep and ask** about subsections you're unsure of, never delete.
- The install/re-sync request authorizes selected managed skills, supported hooks (including blocking gates), and AQG rule blocks; do not request consent again for these steps. Keep source/scope checks and client-required trust steps; destructive / irreversible actions still need explicit authorization.

---

## Maintenance / distribution notes

- **This file is part of the AQG distribution package**. After the `examples/` templates update, already-configured users can **feed this file to the AI again** to idempotently re-sync (cases b/c).
- **This file must travel with the toolkit if it ever moves to another repo** (together with `examples/aqg-*.example.md`, `scripts/install.sh`, `agent-packs/claude-code/install.sh`, `scripts/install_aqg_hooks.py`, `scripts/install_aqg_codex_hooks.py`, `scripts/install_cursor_support.py`, `scripts/install_aqg_qoder.py`, `scripts/install_aqg_work_clients.py`, `scripts/install_aqg_agent_clients.py`, `scripts/install_aqg_pi.py`, `scripts/aqg_client_registry.py`, `scripts/install_aqg_clients.py`, `scripts/run_aqg_codex_hook.py`, `scripts/cursor_aqg_hook.py`, `scripts/agent_client_aqg_hook.py`, `scripts/pi_aqg_hook.py`, `agent-packs/claude-code/hooks/`, `agent-packs/qoder/hooks/`, `scripts/aqg_doctor.py`, and `docs/AUDIT_DECISION_MODEL.md`) — they are the minimal set that makes the configuration "self-bootstrapping."
- This guide deliberately **does not hardcode the distribution repo address** (it can change over time), relying on the step 1 probe / asking the user.
