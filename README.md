# Agent Quality Gates

English | [中文](README.zh-CN.md)

Current version: `0.14.22` (source of truth: `VERSION`; release notes in `CHANGELOG.md`).

Agent Quality Gates (`AQG`) is a **quality-discipline toolkit** for AI coding workflows — it pushes quality from "review after writing" to "guard while writing." It **rides on top of your existing coding agent**, decoupled from any specific one, and has four parts:

- **skills** — 16 quality-discipline skills spanning the full work lifecycle (table below).
- **hooks** — a mechanical enforcement layer that lands the auto-triggerable subset of the discipline onto the agent's lifecycle events.
- **gate scripts + CI adapter** — deterministic worktree / audit / evidence checks, wired into local runs and GitHub PRs via a per-project `quality-gates.json`.
- **docs** — the engineering-framework contract, rollout / decision / audit-evidence records, and templates.

## Why AQG

Most quality tooling reviews code *after* it's written, by one model, in one language, and forgets everything between sessions. AQG does the opposite — it guards while you write, can cross-check through independent models when an external audit engine is connected, works on virtually any language, and carries state across sessions. Each skill below is a distinct, sharpened capability — not a name to guess at.

### Build it right the first time

- **Construction discipline, not post-hoc review.** `aqg-code-construction` runs a 6-step process *during* implementation. For new behavior and bug fixes, Step 2 — **Behavior Lock** — uses a RED → GREEN → REFACTOR TDD cycle: write the failing test before the code so the implementation is pinned to verifiable behavior. Pure refactors use the existing regression net; docs/spec/config changes and migrations follow the scoped alternatives in the skill. Step 3 — **Thin Slice** — ships the minimal change and defers opportunistic refactors, so no bloat rides along. Plus 4+4 anti-pattern blockers, tiered reviewer-objection prediction, and a structured evidence ledger.
- **Virtually any language.** The construction, review, and debugging skills are language-agnostic — the discipline applies to any codebase, whatever it's written in: Python, JavaScript, TypeScript, Java, Kotlin, Scala, Groovy, Clojure, C, C++, C#, F#, Objective-C, Go, Rust, Zig, Nim, Crystal, Swift, Ruby, PHP, Perl, Dart, Elixir, Erlang, Haskell, OCaml, Elm, Racket, Lua, Julia, R, Solidity, Shell/Bash, PowerShell, SQL, HTML/CSS, Vue, Svelte, CoffeeScript, and more. The construction reminder auto-fires on 54 file extensions out of the box; the discipline itself has no language boundary.

### Review that catches what one model misses

- **Cross-LLM-ready, 5-dimension review.** `aqg-multi-review` produces dimension-specific routing prompts and an adjudication ledger for logic / edge-cases / security / performance / concurrency. With an external `/audit` engine connected, those dimensions can be sent to an **independent-vendor** LLM panel; without one, the shipped capability remains a structured routing + self-review + adjudication harness.
- **Real security depth.** `aqg-security-review` supports a 3-layer security workflow: (1) AQG's shipped 6-step in-session OWASP Top 10 + CWE Top 25 + secure-by-default library checklist, (2) external Semgrep deterministic SAST, and (3) an external `/audit` LLM review. AQG defines and integrates the workflow; the Semgrep and LLM engines plug in separately.
- **Tests that test behavior.** `aqg-test-quality-review` flags tests that assert SHAPE (types, key existence) instead of BEHAVIOR (output / side-effects / errors), and catches weakened, skipped, or flaky tests (sleep / wall-clock / unseeded random / live network).
- **Root-cause debugging.** `aqg-systematic-debugging` enforces reproduce → isolate → one-hypothesis-at-a-time → minimal fix → regression check. No fix without evidence — on any language or stack.

### Never lose the thread across sessions

- **8-section handoff.** `aqg-session-handoff` emits a paste-ready cross-session prompt (background · verified state · next steps · discipline traps · first concrete action · open decisions) plus a `validate` gate that rejects it if a section is missing or a secret leaked. The next session cold-starts from a complete handoff — nothing dropped, nothing forgotten.
- **A decision log you can grep months later.** `aqg-decision-capture` appends each lasting decision to `docs/decisions/LOG.md` as a redacted one-liner (date · actor · decision · rationale · basis). Under long, heavy use this is where you pull up *why* you decided something — the reasoning survives, not just the outcome.
- **Project ledger.** `aqg-project-status` renders the machine-local progress ledger so you can review where a project actually stands.
- **Memory hygiene.** `aqg-memory-hygiene` keeps engineering knowledge in the repo (code / docs / PRs) instead of stale machine-local memory, validates memory frontmatter, and flags entries past their re-verify horizon.

### Guardrails that don't depend on remembering

- **Preflight before you touch anything.** `aqg-startup-preflight` checks live git + GitHub state (dirty worktree / gone upstream / behind remote / missing context files / open PRs) so you never start on a stale base.
- **Closeout before you say "done".** `aqg-evidence-closeout` makes you answer 6 questions — what changed · what fresh check proves it · what durable state updated · what boundary you did *not* cross · what's still blocked — before claiming completion.
- **Mechanical enforcement.** Claude Code, Codex, Cursor, CodeBuddy, Kimi Code, Qoder CLI, Trae IDE, and Devin CLI clients have managed lifecycle adapters; WorkBuddy, Trae Work, Trae Work CN, Kimi Work, Zed, QoderWork, and QoderWake expose only the documented subset listed in the client matrices.

At a glance:

| | |
|---|---|
| Skills | **16**, spanning the full work lifecycle — each a sharpened capability |
| Language coverage | **40+** languages — discipline is language-agnostic; auto-reminder on **54** file extensions |
| Review | **5**-dimension review router · optional external **cross-LLM** panel · OWASP Top 10 + CWE Top 25 security |
| Enforcement | Claude Code + Codex **6** lifecycle event types / **4** blocking policies · Cursor **5** events · CodeBuddy/Kimi Code profile-dependent · Qoder profile-dependent |
| Coding agents | **Claude Code + Codex + Cursor + Pi + CodeBuddy + Kimi Code + Qoder family + Trae family + Zed + Devin + selected work clients** |

## Supported coding agents

AQG's capability core is agent-agnostic; each agent plugs in through a thin adapter. [`AI_SETUP.md`](AI_SETUP.md) is installed-supported by default: it resolves support from `scripts/aqg_client_registry.py`, detects local supported/full/partial client configuration roots, and configures every local desktop adapter it finds.

| Coding agent | Status | Managed surfaces |
|---|---|---|
| Claude Code | supported | skills + hooks + `CLAUDE.md` rule block |
| Codex | supported | skills + hooks + `AGENTS.md` rule block |
| Cursor | supported | skills + hooks + project rules; user rules remain UI-managed |
| WorkBuddy (`workbuddy`) | partial | skills + rules/report; no documented lifecycle hooks |
| CodeBuddy (`codebuddy`) | full | skills + hooks + rules + MCP |
| WorkBuddy AI (`workbuddy-ai`) | full | skills + hooks + rules + MCP under its own independent `~/.workbuddy-ai` root; not shared with `workbuddy` or `codebuddy` |
| Trae Work (`trae-work`) | partial | skills in the shared `~/.trae/skills` root + project rules; Work lifecycle-hook schema not verified |
| Kimi Work (`kimi-work`) | partial | AQG skills in the Kimi Work Desktop / Kimi Desktop local Daimon skills root; no rules/hooks/MCP gate |
| Kimi Code (`kimi-code`) | partial | skills + advisory hooks + rules + MCP; hooks are fail-open |
| Qoder CLI / Qoder CLI CN (`qoder-cli` / `qoder-cli-cn`) | full | skills + hooks + user/project rules |
| Qoder IDE / Tongyi Lingma (`qoder` / `qoder-cn`) | partial | skills + hooks + project rules; no `SessionStart`, `PreCompact`, or WIP save/recover |
| Trae / Trae CN (`trae` / `trae-cn`) | partial | skills + hooks + project rules; no `PreCompact`, user rules remain UI-managed |
| Trae Work CN (`trae-work-cn`) | partial | skills in the shared `~/.trae-cn/skills` root + project rules; Work lifecycle-hook schema not verified |
| Zed (`zed`) | partial | skills + `AGENTS.md` instructions; no lifecycle hooks |
| Devin (`devin`) | partial | skills + hooks + `AGENTS.md` rules; no `PreCompact` before compaction |
| QoderWork (`qoderwork`) | partial | skills + rules + MCP; no Qoder CLI lifecycle hook contract |
| QoderWake (`qoderwake`) | partial | skills + rules + MCP; no blocking lifecycle hook contract |
| Pi (`pi`) | partial | skills + TypeScript extension hooks + support report; no AQG MCP connector |

See the capability matrices for evidence and per-feature degradation notes: [`docs/client-support-matrix.zh-CN.md`](docs/client-support-matrix.zh-CN.md) and [`docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md`](docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md).

## Install

### Quick start — let the AI install it

Paste the entire [`AI_SETUP.md`](AI_SETUP.md) into your coding agent. It detects locally installed AQG supported/full/partial desktop clients, plans them with `scripts/install_aqg_clients.py --installed-supported`, and idempotently configures every selected rule surface. No hand-editing is required.

### Multi-client install — explicit batch entry point

Use the wrapper directly when you want to inspect or override the default installed-supported target set:

```bash
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --apply
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --all-registry
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --all-registry --project-root /path/to/project --apply
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --clients codex,claude-code,cursor,qoder-cli --apply
```

The wrapper is dry-run by default; `--apply` is required before it writes. Supported lifecycle hooks are installed/refreshed by default for selected clients; pass `--no-hooks` only for an explicit skills/rules-only install. `--installed-supported` detects local config roots and selects registry clients whose `support_status` is `supported`, `full`, or `partial`. `kimi-work` means Kimi Work Desktop / Kimi Desktop and is detected from its Daimon skills root (`KIMI_WORK_SKILLS_ROOT`, `KIMI_DESKTOP_SKILLS_ROOT`, running Daimon process command lines, Kimi Desktop logs, `.rehomed` markers, or Kimi.exe install-drive evidence); `kimi-code` remains the Kimi Code CLI profile under `~/.kimi-code`. `--all-registry` dynamically expands from `scripts/aqg_client_registry.py` to every registry adapter regardless of support-status label; `--all-supported` is a backward-compatible alias for the same mode. The current expansion is `codex`, `claude-code`, `cursor`, `workbuddy`, `codebuddy`, `trae-work`, `kimi-work`, `kimi-code`, `qoder-cli`, `qoder-cli-cn`, `qoder`, `qoder-cn`, `trae`, `trae-cn`, `trae-work-cn`, `zed`, `devin`, `qoderwork`, `qoderwake`, and `pi`. The plan prints each client with its `support_status` and capability notices for degraded labels; for `kimi-work`, it also prints `resolved_skills_root` and `discovery_source`. It also exposes the registry's read/remove actions: `--verify`, `--is-installed`, and `--uninstall`. `--core` expands only to `codex,claude-code`. Project-scope adapters such as Qoder IDE and Pi project profiles require `--project-root` before write actions run.

Registry client skills default to link/symlink installs. Copy mode must be explicit: Codex uses `--copy`; Claude Code uses `--mode copy`; Cursor, Qoder, work/code clients, Trae/Zed/Devin clients, and Pi use `--mode copy`. Kimi Work tries link mode first and automatically falls back to copy if the OS refuses symlink/junction creation; pass `--mode copy` to copy directly, or `--mode link --strict-link` to fail closed instead. Restart Kimi Work / Kimi Desktop after apply so Daimon reloads the skills. The Qoder installer currently has no `--no-hooks` flag, so wrapper `--no-hooks` rejects Qoder-family profiles instead of inventing an unsupported argument. Work/code clients use `scripts/install_aqg_work_clients.py`, Trae/Zed/Devin clients use `scripts/install_aqg_agent_clients.py`, and Pi uses `scripts/install_aqg_pi.py`; all three support `--no-hooks`. Clients without documented hooks simply install no hook entries.

### One command — Claude Code + Codex first install

```bash
bash -lc 'set -euo pipefail; repo="$HOME/.deeppattern/agent-quality-gates"; if [ ! -e "$repo" ] && [ ! -L "$repo" ]; then mkdir -p "$(dirname "$repo")"; gh repo clone deeppatternai/agent-quality-gates "$repo" -- --config core.autocrlf=false --config core.eol=lf; fi; export AQG_ROOT="$repo"; python3 "$repo/scripts/install_aqg_clients.py" --clients codex,claude-code --aqg-root "$repo" --apply; python3 "$repo/scripts/aqg_doctor.py" --no-cli'
```

This clones `$HOME/.deeppattern/agent-quality-gates` on first install, or reuses the existing checkout, then installs Codex and Claude Code through the shared wrapper and runs Doctor. A successful install at the default location attempts to create the managed version layout. Check the final automatic-update result; installation success alone does not prove migration succeeded. Existing managed versions are never checked out in place by this command. For an explicit upgrade, use `scripts/upgrade.sh`. See [Windows automatic updates](docs/WINDOWS_AUTO_UPDATE.md) for prerequisites and the updates that still require reconciliation.

Claude Code hooks require `AQG_ROOT` in the client environment. Codex hooks embed the reviewed checkout path and do not rely on shell inheritance:

```bash
export AQG_ROOT="$HOME/.deeppattern/agent-quality-gates"   # add to your shell profile
```

> No `gh`? Swap `gh repo clone …` for `git clone git@github.com:deeppatternai/agent-quality-gates.git "$repo"`. Windows / PowerShell: keep the `bash -lc '...'` wrapper (PowerShell does not expand `$HOME`).

### Claude Code + Codex upgrade

```bash
"$HOME/.deeppattern/agent-quality-gates/scripts/upgrade.sh"
```

Pulls main → reinstalls and prunes leftover skills → installs/refreshes supported Claude Code/Codex hooks by default → verifies with `aqg_doctor.py`. It does **not** rewrite the AQG rules block in your `CLAUDE.md` / `AGENTS.md`; refresh that separately with `scripts/install_aqg_rules.py --client <claude-code|codex> --apply`, which backs up the file and replaces only the managed region. `aqg_doctor.py` reports a block that is missing or carries a retired framing (`| grep rules_block`). Use `--no-hooks` only for an explicit skills-only upgrade. Cursor and Qoder-family installs are refreshed by rerunning their idempotent `--apply` commands below. Options: `--ref vX.Y.Z`, `--hooks`, `--no-hooks`, `--clean-only`.

### Hooks & pre-commit

- **Codex hooks (installed by default with Codex support; 4 blocking policies):** `python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$AQG_ROOT"` (`--verify` / `--uninstall` / `--is-installed`). It merges `~/.codex/hooks.json`, emits `commandWindows`, binds each trusted definition to a runner+policy content digest, and requires review/trust through `/hooks` after restart.
- **Claude Code hooks:** `python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --apply` (`--verify` / `--uninstall` / `--is-installed`). For a never-blocking starter, use `agent-packs/claude-code/hooks/settings.warn-only.example.json`.
- **Cursor (user scope):** `python3 "$AQG_ROOT/scripts/install_cursor_support.py" --apply --scope user --mode link` installs skills and hooks; project scope adds `.cursor/rules/aqg.mdc` via `--scope project --project-root /path/to/project --mode link`. Supports `--verify` / `--uninstall` / `--is-installed`; `--no-hooks` is available. Cursor User Rules are UI-managed and are not written by AQG.
- **Work/Code clients:** `python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client codebuddy --scope user --aqg-root "$AQG_ROOT" --mode link --apply`. Profiles: `workbuddy`, `codebuddy`, `kimi-work`, `kimi-code`, `qoderwork`, `qoderwake`. `trae-work` was retired from this adapter — it shares the Trae skills root, so it is installed with `install_aqg_agent_clients.py`; the old entry point exits 2 with that redirect. `kimi-work` is Kimi Work Desktop / Kimi Desktop and installs AQG skills to the resolved Daimon `daimon-share/daimon/skills` root; use `--skills-root /absolute/path/to/skills` to override unusual installs or tests. `kimi-code` is Kimi Code CLI and still uses `~/.kimi-code`. Supports `--verify` / `--uninstall` / `--is-installed`; `--no-hooks` is available. Degraded profiles install only documented surfaces and report the missing gate coverage.

Kimi Work Desktop / Kimi Desktop may rehome Daimon data based on the install drive: older installs can use `%APPDATA%\kimi-desktop\daimon-share`, while D-drive installs can use `D:\KimiData\daimon-share`. AQG therefore discovers the skills root dynamically and never adds a separate `kimi-desktop` client id. Public Kimi docs mostly cover Kimi Code CLI; the Kimi Work Daimon root is derived from local installer README/log/process evidence, so explicit `--skills-root` override is part of the supported adapter contract.
- **Qoder family:** `python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client qoder-cli --scope user --aqg-root "$AQG_ROOT" --mode link --apply`. Choose exactly one profile: `qoder`, `qoder-cli`, `qoder-cn`, or `qoder-cli-cn`; project scope requires `--project-root /path/to/project --mode link`. Supports `--verify` / `--uninstall` / `--is-installed`. CLI profiles report `full`; IDE profiles report `partial` because `SessionStart`, `PreCompact`, and WIP save/recover are unavailable. There is no Qoder `--no-hooks` mode.

Profile roots are per product: `qoder` and `qoder-cli` use `~/.qoder`, `qoder-cn` and `qoder-cli-cn` use `~/.qoder-cn`. `.lingma` is not a Qoder CN install target — `~/.lingma` is the legacy Tongyi Lingma root. Qoder IDE user-scope skills and hooks install without `--project-root`: `python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client qoder --scope user --aqg-root "$AQG_ROOT" --mode link --apply`. Only the project `rules/aqg.md` file needs a project root, so a missing `--project-root` skips that one command rather than the whole Desktop install.

Desktop profiles are selected from macOS product identity, never from a config directory. The `qoder` profile accepts `Qoder.app` / `com.qoder.app` and `Qoder IDE.app` / `com.qoder.ide`; `qoder-cn` accepts `Qoder CN.app` / `com.qodercn.app` and `Qoder CN IDE.app` / `com.aliyun.lingma.ide`. Multiple accepted bundles still select each profile only once. The other identities are `Trae.app` / `com.trae.app`, `Trae CN.app` / `cn.trae.app`, `TRAE SOLO.app` / `com.trae.solo.app`, and `TRAE SOLO CN.app` / `cn.trae.solo.app`. CLI profiles need an executable on `PATH` (`qoder`, `qoder-cn`). If a desktop bundle and a CLI runtime are both found for the same settings surface, `--installed-supported` selects neither and asks for an explicit `--clients` choice, because the two install different hook contracts into one `settings.json`.
- **Trae / Zed / Devin:** `python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client trae --scope user --aqg-root "$AQG_ROOT" --mode link --apply`. Choose one profile: `trae`, `trae-cn`, `trae-work`, `trae-work-cn`, `zed`, or `devin`; project scope requires `--project-root /path/to/project --mode link`. Supports `--verify` / `--uninstall` / `--is-installed` and `--no-hooks`. All six profiles report `partial`, and this adapter owns all four Trae products. `trae` and `trae-work` share the `~/.trae/skills` user root; `trae-cn` and `trae-work-cn` share `~/.trae-cn/skills`. Installing both profiles of a pair is idempotent, and uninstalling one keeps the skills the other still needs — AQG records which profiles claim a shared root and removes the skills only when the last claim is released. The Work profiles install no hooks, because no TRAE Work lifecycle-hook schema has been verified. See `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md` for the degraded gates.
- **Pi:** `python3 "$AQG_ROOT/scripts/install_aqg_pi.py" --scope user --aqg-root "$AQG_ROOT" --mode link --apply` installs AQG skills into `~/.pi/agent/skills`, a managed TypeScript extension into `~/.pi/agent/extensions`, and a support report. Project scope uses `.pi/` via `--scope project --project-root /path/to/project --mode link`. Supports `--verify` / `--uninstall` / `--is-installed` and `--no-hooks`; reports `partial` because MCP/connectors are unsupported and closeout/WIP save depend on Pi extension lifecycle delivery.
- **pre-commit** (git-lifecycle secret gate, opt-in) — wires [Gitleaks](https://github.com/gitleaks/gitleaks) via the [pre-commit framework](https://pre-commit.com/): `python3 "$AQG_ROOT/scripts/install_pre_commit.py" --target-repo /path/to/project`. AQG does **not** `pip install` by default (`--allow-pip` to opt in). Templates: `templates/pre-commit-config.aqg.example.yaml`, `templates/.gitleaks.aqg.example.toml`.
- Per-agent low-level installers (`--copy` / `--force` / `--dest`), versioned/pinned installs, and multi-machine setup: see `docs/INTEGRATION_GUIDE.md` + `docs/INSTALL_VERSIONING.md`.

> **Add `.aqg/` to the target project's `.gitignore` before using hooks** (a warn-only hook reads `<project>/.aqg/pr-body.md` locally; keep it out of commits). Copy `examples/aqg-gitignore.example`.

## Skills (16)

Every supported client installer delivers the same 16 skills (Claude Code uses its generated wrapper pack), grouped by work phase:

| phase | skill | what it does |
|---|---|---|
| Startup | `aqg-startup-preflight` | Before work, checks worktree + GitHub state (dirty / gone upstream / behind / missing required file) + a parallel-session advisory; never a blocker, defaults to the git root (`--repo` override) |
| Construct | `aqg-code-construction` | 6-step proactive construction process + 4+4 anti-pattern blockers + tiered objection prediction + a structured ledger (see the Code Construction section below) |
| Debug | `aqg-systematic-debugging` | Root-cause-first 6-step debug + 8-tier feedback-loop tactic (type/lint < 5s → unit < 30s → … → E2E); no fix without evidence |
| Review | `aqg-multi-review` | Code-review router across 5 dimensions (logic / edge_cases / security / performance / concurrency); emits dimension-specific prompts and a structured adjudication ledger |
| Review | `aqg-security-review` | In-session OWASP Top 10 + CWE Top 25 + an 8-category secure-by-default library checklist; complements semgrep SAST + audit-mcp external review |
| Review | `aqg-test-quality-review` | Judges whether a test asserts BEHAVIOR (output/side-effect/error) vs SHAPE (type/structure/key existence) + finds coverage-gap / weakened / flaky; signal-only |
| Review | `aqg-audit-adjudication` | Adjudicate each audit / code-review / second-opinion finding into accept / reject / needs-user-decision; outputs a structured table |
| Close-out | `aqg-evidence-closeout` | Produce an evidence ledger before completion; auto-imports `.aqg/current_ledger.md` (from the construction skill) + the Transfer Test Pack run summary |
| Close-out | `aqg-decision-capture` | Turn lasting decisions (ruling / agent choice / external-audit adjudication) into a grep-able, redacted-on-read one-line entry in `docs/decisions/LOG.md` (`format` / `query` / `validate`); fights "why did we decide this" amnesia |
| Handoff | `aqg-session-handoff` | When CTX is near the limit / already compacted / handing off, produce a paste-ready handoff prompt (background + precise state + next steps + discipline traps) |
| Orchestrate | `aqg-phase-transition` | Emits a signal at three phase nodes (`PLAN_DONE` / `IMPL_DONE` / `TESTS_WRITTEN`); depth comes from the `depth-by-stakes` mapping in `docs/policies/audit-trigger.md` (the skill supplies timing, never depth) + 5-min dedup |
| Orchestrate | `aqg-re-anchor` | At long-session step / WorkPacket boundaries, emits a compact restatement (goal + active gates + progress) to resist goal-drift; emit-only |
| Meta | `aqg-skill-validator` | Before commit, validates a skill's sidecar manifest + SKILL.md frontmatter + boundary section + cross-cutting registration + wrapper-sync drift (fails under `--strict`) |
| Meta | `aqg-automation-audit` | Automation-stack inventory (hooks / MCP / plugins / skills / env) + overlap-check + a structured verdict (`keep` / `migrate` / `disable` / `fix`); advisory + read-only |
| Meta | `aqg-project-status` | View low-fidelity progress events from the machine-local ledger (non-merge commits in the session window); progress-only, never goes online / to the cloud |
| Meta | `aqg-memory-hygiene` | Self-check before writing memory — engineering/project content goes to the repo (code/docs/PR), not machine-local memory; keep only user prefs / how-to-work / external pointers |

These skills are a tooling layer: no production secrets, no deployments, no project-path restrictions. They run at the git root of the current working directory by default; pass `--repo /path/to/repo` for multi-repo checks.

## How AQG enforces discipline

Three surfaces land the same discipline, from softest to hardest.

### 1. Rule block — all agents

Skills auto-match by description, but to **force** an invocation at the key junctures, install the discipline on the client's documented rule surface. Claude Code and Codex use these manually syncable files:

| Scope | Claude Code | Codex |
|---|---|---|
| Global (user) | `~/.claude/CLAUDE.md` | `~/.codex/AGENTS.md` |
| Project | `<project>/CLAUDE.md` | `<project>/AGENTS.md` |

Install the global block with the client-support installer, which appends it below whatever the file already contains, marks the region it owns so a re-run replaces only that, resolves the checkout path inside it, and backs the file up first:

```bash
# Claude Code
python3 "$HOME/.deeppattern/agent-quality-gates/scripts/install_aqg_rules.py" --client claude-code --apply
# Codex
python3 "$HOME/.deeppattern/agent-quality-gates/scripts/install_aqg_rules.py" --client codex --apply
```

`--verify` re-checks an installed block against the shipped template; `--uninstall` removes the managed region and leaves the rest of the file alone. For a **project**-scope block, paste the template's own `sed` line (in its header) into the project file — the installer writes user scope only. If you installed a block by hand before this installer existed, `--apply` refuses rather than appending a second copy: delete the old section first.

Cursor and Qoder-family rules are owned by their installers: Cursor writes project rules only (user rules are UI-managed); Qoder IDE writes project rules, while Qoder CLI profiles support user and project rules. The installers refuse to overwrite unowned rule files.

The block names **one entry point** — `aqg-code-construction`, invoked before writing code — plus the Gate A criteria that decide whether a change needs an audit at all. It deliberately does not restate what each skill does: the host already loads all 16 skill descriptions as its index, and every `aqg-*` skill announces its own triggers there. What the block carries is what has to be resident *before* anything is read. Global vs project: use global for yourself, a project-level block (committed to git) so contributors share the same rules.

> **Dogfood note**: a session running *inside* an AQG-led project is itself a user of these rules — invoke the skill, don't just follow its spirit (schema consistency + finding ergonomic flaws + cross-actor handoff).

### 2. Hook layer — supported clients

Hooks land the mechanically-triggerable subset onto each client's documented lifecycle events, so a session cannot simply forget it. Claude Code uses the canonical managed scripts; Codex, Cursor, and Qoder use thin adapters that translate their event schemas while reusing those policies where the host exposes a matching event. Coverage therefore follows the support matrix above rather than implying identical events on every client. Hosted tools and specialized paths that bypass the local hook pipeline remain outside mechanical enforcement.

| Trigger | Level | Effect |
|---|---|---|
| PreToolUse(Bash) — about to `git commit` | **BLOCK** | staged changes touch `skills/aqg-*/SKILL.md` or `install.sh` → auto-runs `aqg-skill-validator`; failure rejects the commit |
| PreToolUse(Edit\|Write\|MultiEdit) — memory write | **BLOCK** | target under `~/.claude/projects/*/memory/` + code-shaped content (fenced block / `def`/`class`) → reject (engineering knowledge belongs in the repo) |
| PreToolUse(file edit) — AQG gate-file tamper guard | **BLOCK** | an edit from outside the AQG checkout targets a managed AQG gate file → reject (cross-project tamper protection) |
| PreToolUse(Write\|Edit\|Bash) — secret scan | **BLOCK** | a recognized secret written to disk or passed in a Bash command (18 built-in patterns) → reject |
| PostToolUse(Bash, error) | warn | prompts the `aqg-systematic-debugging` 6 steps |
| PostToolUse(code-file edit) | warn | asks whether it went through the `aqg-code-construction` 6 steps |
| PostToolUse(test-file edit) | warn | prompts `aqg-test-quality-review` (BEHAVIOR vs SHAPE / flaky) |
| PostToolUse(security-sensitive edit) | warn | prompts `aqg-security-review` (OWASP / CWE) |
| PostToolUse(AQG skill-file edit) | warn | prompts `aqg-skill-validator` before commit |
| PreCompact + Stop | warn | checks the `aqg-evidence-closeout` 6 questions + prompts `aqg-session-handoff`; snapshots the worktree to `refs/aqg-wip/<session>` (zero-touch, never leaves the machine) |
| SessionStart | info | auto-runs `aqg-startup-preflight` (summary capped at 50 lines) + surfaces unrecovered wip checkpoints |
| UserPromptSubmit(handoff intent) | inject | detects handoff intent → injects an instruction forcing `aqg-session-handoff` (forbids freestyling); silent otherwise |

The 4 blocking gates each ship an `AQG_AGENT=human-opt-in` escape. Managed policy scripts live in `agent-packs/claude-code/hooks/`; Codex invokes them through `run_aqg_codex_hook.py`, which supplies the stable checkout path and Codex memory root. On Codex, the memory-placement gate covers `apply_patch`; shell-mediated writes are checked only for literal secrets, not memory-placement semantics. Hosted tools and specialized paths that opt out of the local hook pipeline remain outside this enforcement boundary. Behavior suites: `tests/behavior/test_aqg_hooks.py` and `tests/test_codex_hooks.py`.

### 3. Code construction — proactive discipline

`aqg-code-construction` pushes quality from reactive review to discipline applied *during* implementation, treating three failure modes: freestyling without reading neighbor code, opportunistic refactoring, empty self-review. It enforces a 6-step process (Pattern Mining → Behavior Lock → Thin Slice → Construction Rules → Local Verification → Self Review) + 4+4 anti-pattern blockers + tiered (1/3/5) reviewer-objection prediction + a structured 5-column ledger.

A pre-commit checker enforces it, but **only when `AQG_AGENT` names an agent** — a human commit passes silently (transparent). Set `AQG_AGENT=claude` / `codex` to enforce. Setup (per repo) and full workflow: see `skills/aqg-code-construction/SKILL.md`.

## Gate scripts & CI

The skills remind you within a session; the gate scripts give repeatable, CI-ready red/green signals. Roll out **warn-only first**, switch to blocking only after templates and habits stabilize.

Deterministic gates (each ships an offline `--self-test`):

```bash
python3 scripts/check_dirty_or_gone_worktree.py --repo /path/to/repo   # dirty / detached / upstream gone / behind → non-zero
python3 scripts/validate_audit_adjudication.py path/to/audit.md         # requires finding/decision/action/verification table
python3 scripts/check_evidence_closeout.py --strict path/to/pr.md       # scope / verification / audit / durable state / boundary / blockers
```

**Config** — a per-project `quality-gates.json` (`version: 1`; strictness per gate: `warn` / `blocking` / `off`; unknown version/gate/mode fails closed). Validate: `python3 scripts/quality_gates_config.py --config quality-gates.json --print-json`.

**CI adapters** — the local adapter (`scripts/run_quality_gates.py`) reads the config + a local PR-body markdown and emits a console summary + redacted JSON (no GitHub access, never writes the raw PR body). The GitHub PR adapter (`scripts/fetch_pr_body.py` / `render_pr_comment.py` / `post_pr_comment.py`) posts one bounded sticky comment. Copy `examples/github-actions/quality-gates-warn.yml` + `examples/quality-gates.json` into a target repo to adopt; it never modifies a target repo's CI or branch protection. Full rollout (private-read credentials, PAT rotation, fork-PR safety, blocking): `docs/INTEGRATION_GUIDE.md`.

## Boundaries & safety

- **Tooling layer only** — no production secrets, no deployments, no branch-protection changes; produces evidence / score / source-linked answers, never executes a merge.
- **Never in the target's production runtime** — discipline is applied at review-time and during construction (dev-time), never inside the project's production path.
- **Redaction by default** — the doctor session fingerprint stores only metadata (hash / count / status / version), never raw `.env` values / tokens / full paths; the machine-local ledger and wip checkpoints never leave the machine.
- **`.aqg/` must be gitignored** — add it to each target project's `.gitignore` before enabling hooks so workspace state stays out of commit history.

## Known limitations

Be clear-eyed about what ships in this repo and what does not:

- **The cross-vendor audit engine is external.** `aqg-multi-review` and the `/audit` handoff points in `aqg-code-construction` produce routing prompts and adjudicate returned findings, but the panel engine itself is **not in this repo**; it is a separate component you supply or stub. Out of the box, treat these capabilities as a **structured routing + self-review + adjudication harness**, not a working cross-vendor second opinion. Nothing here should be read as "install AQG and you get an independent multi-model audit" until you wire an engine in.
- **Hook reminder delivery is best-effort, not verified end-to-end.** The hook layer emits reminders at trigger points, but their delivery across every agent surface and version is not comprehensively tested. Treat the hooks as nudges that keep gates resident, not a guaranteed-delivery channel.
- **"Catches what one model misses" is a mechanism argument, not a measured result.** It rests on the rationale that independent-vendor panels have uncorrelated blind spots — not on a statistically powered benchmark (the internal comparison is currently n=1). Read it as an illustration of the mechanism, not a quantified quality delta.

## Docs & templates

- `docs/ENGINEERING_FRAMEWORK.md` — the general-purpose engineering-discipline contract (north-star invariant / AQG boundary / Build-Adapt-Defer / 4-Layer model / redaction). AQG is its reference implementation.
- `docs/AGENT_COMPATIBILITY_STRATEGY.md` — the cross-agent adapter strategy.
- `docs/DOCUMENTATION_OPERATING_MODEL.md` — where bugfix / decision / gate-rollout / audit-evidence records land.
- `docs/INTEGRATION_GUIDE.md` · `docs/INSTALL_VERSIONING.md` — multi-machine setup + pinned-commit / tag install strategy.
- `templates/` — PR, bugfix, decision, gate-rollout, and audit-evidence record templates.

## Maintaining the release version

`VERSION` is the source of truth. Run from the repository root:

```bash
python3 scripts/set_version.py               # sync both README current-version lines
python3 scripts/set_version.py 0.15.0        # set VERSION and sync (example next version)
python3 scripts/set_version.py --check       # read-only check; exits 1 on drift
```

The script accepts SemVer, including prerelease/build suffixes and an optional `v` prefix.
`--check` always checks `VERSION` and cannot be combined with a new version.
All three managed files must already exist. Sync also normalizes `VERSION` to the version
plus one LF newline; `--check` reports noncanonical formatting. Add any future current-release
fields explicitly to the script's marker map and tests.
It validates every marker before writing and preserves README line endings. Update release
notes separately: historical versions, schema/tool versions, tags and signed manifests are
not rewritten. Relevant pushes, PR CI and tag-release publishing run `--check` to catch drift.

## License

MIT — see the `LICENSE` file. © 2026 Zhou Peng.

It covers this public release (the shipped skills, hooks, scripts, docs, and templates). The external cross-vendor audit engine is a separate component and is not part of this release — see [Known limitations](#known-limitations).
