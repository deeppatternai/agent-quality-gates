# Engineering Framework

English | [中文](ENGINEERING_FRAMEWORK.zh-CN.md)

> Theme: complete engineering tasks to a high standard inside a high-quality engineering environment
> Carrier: AQG is the reference implementation of this framework; other projects plug in via their own `quality-gates.json`

## 0. Document status

This is a **general-purpose engineering-discipline contract** — it defines the framework-level agreement for "in what kind of engineering environment, with what discipline, do we get engineering tasks right", decoupled from any specific project or tool version.

AQG is the **reference implementation carrier** of this framework; any engineering project can plug in via its own `quality-gates.json`.

**Not in this document**: project-level invariants, business logic, product decisions, and any tool- or version-specific implementation detail — those live in each project's own `CLAUDE.md` / `AGENTS.md` / `03_INVARIANTS.md`, or in commits / PRs / the decision log. This document keeps only the **principles and boundaries** that stay stable across projects and across time.

## 1. North-star invariant

**Core question**: swap the lead / swap Claude / swap Codex / swap the context — can the system still produce correct behavior?

**Operational verifier — Transfer Test Pack** (lives in `tests/transfer/`):

- fresh checkout + model/client switch + fixed task set
- expected artifacts schema: PR body / commits / evidence ledger / decision log
- required-checks list
- pass/fail metrics: artifacts-schema match rate, required-check pass rate, boundary-violation count
- **testable threshold (recommended)**: Transfer Test Pack ≥ 95% pass rate

**Layer 1-3 partial validation** (not a replacement for the Continuity Drill):

- Cross-env CI (Layer 1) validates "swap OS / shell / tool version"
- Multi-agent handoff manifest (Layer 2) validates "swap agent"
- Incident recall (Layer 3) validates "swap context"

The Continuity Drill / Context Wipe Test is the ultimate e2e means.

**Mechanisms that operationalize the north star**: decision log (durable decisions, fighting "why did we decide this" amnesia) / handoff manifest (cross-session handoff) / memory hygiene (memory that does not rot) / re-anchor (goal-drift guard in long sessions) — these let work resume after "swap the lead / swap the agent / swap the context"; they turn the north star from a slogan into a mechanism (capability split in §3, resistance to knowledge decay in §5 Layer 3).

## 2. AQG boundary (staying light = the quality layer)

### What AQG actually owns

- **schema definitions** (quality-gates / handoff manifest / runtime contract / scorecard / smoke / incident index / metrics ledger / surface fingerprint)
- **validator** implementations
- **judge** decisions (pass / warn / block)
- **review-time analyzers** (doctor / validate_agent_pack / blast-radius / skill trigger points / weekly retro clustering / incident markdown index)
- **construction-time discipline** (the `aqg-code-construction` 6-step workflow + construction-time hooks: PostToolUse checker / pre-commit gate / secret-scan / tamper-guard — discipline applied **during** the work, not only post-hoc review)

> **Two enforcement points, neither in the target's production runtime**: AQG evolved from "reactive review-time inspection" to "also proactive construction-time discipline" (skills + hooks that guard while the agent is working). Both happen at **dev/review time** — AQG never enters the target project's production path (see the boundary below).

### What AQG never does

- No docker / no **project-level** real chaos (its own synthetic shell/install fixture excepted, see §3)
- No fuzz / no simulation **implementation** (schema excepted)
- No remote telemetry (the local metrics ledger is a local JSONL, see §3)
- No **orchestration** of sub-agent collaboration flows (schema + sample reference excepted, see §3)
- No project runtime adapter implementation
- No embedding into **the target project's production runtime** (both review-time inspection and construction-time discipline are done, but never inside the target project's production path)
- **No merge execution / holds no repo-privileged action** (produces only evidence / score / source-linked answers; merge is done by GitHub Actions branch protection / repo maintainer / admin; holds no admin token / does not bypass branch protection)

### AQG core boundary (guarding against the "stuff everything into core" bloat trap)

- The AQG core install contains **only**: schema + validator + lightweight scripts + skills + doctor + handoff manifest validator + blast-radius + incident markdown index + weekly retro clustering
- **adapters must be a standalone CLI or project-owned**: OpenAI Agents SDK adapter / Langfuse adapter / Hypothesis adapter / Gitleaks adapter / pre-commit framework wrapper — **not bundled into the AQG core install**
- `install.sh --with-extras` controls optional standalone-CLI installation (modeled on GNU coreutils), without breaking the core boundary

### AQG always verifies external evidence

Never trusts self-attestation alone. Every sub-agent claim must carry verifiable provenance (see §4).

> **Naming note**: the handoff-manifest field names are **inspired by** MPLP (Multi-Agent Lifecycle Protocol) L2 module names (Context / Plan / Confirm / Trace), using prefixed keys `aqg_context` / `aqg_plan` / `aqg_confirm` / `aqg_trace`. **Only the naming is borrowed — an AQG manifest is NOT valid MPLP L2 output**; do not feed it into an MPLP-strict parser.

## 3. Build / Adapt / Defer matrix

Every capability is trisected:
- **build** — AQG writes it (schema / validator / evidence gates / lightweight review-time scripts)
- **adapt** — an industry tool + a schema layer AQG wraps around it (the adapter is standalone or project-owned)
- **defer** — wait until it is genuinely needed

| Capability | AQG | Implementation owner |
|---|---|---|
| Quality gate config schema | build | AQG |
| Gate result JSON schema | build | AQG |
| Evidence closeout / Audit adjudication / Handoff manifest validator | build | AQG |
| Blast-radius / Incident markdown index / Weekly retro clustering | build | AQG |
| Doctor multi-mode + Surface fingerprint validator | build | AQG (**incl. strict redaction, see §6**) |
| Skill trigger points | build | AQG |
| **Cross-env CI matrix (full)** | **defer** | project-owned GitHub Actions workflow (AQG provides a reference example, not mandatory) |
| **Cross-env minimal AQG compliance smoke** | **build** | AQG (**mandatory**, **deliberately narrow — no real task / no Docker**: fresh checkout + install + doctor + validate_agent_pack + generate 1 no-op handoff manifest to verify schema) |
| Pre-commit hooks engine | adapt (standalone) | pre-commit framework; AQG owns the hook evidence schema + installer |
| Static analysis (shellcheck / mypy / ruff / bandit / Semgrep / OSV / Scorecard) | adapt (standalone) | industry tools; the project declares its own analyzer list |
| Secret detection (deep scan) | adapt (standalone, **tiered mandatory**) | **mandatory for AQG itself** (Gitleaks); **mandatory for high-stakes target repos**; **warn-only / reference example for ordinary target repos** (lowers adoption friction); AQG keeps a minimal core set of built-in regexes for self-defense (18; source of truth: `scripts/_secret_patterns.py`) |
| Docker ephemeral install | defer | project-owned Dockerfile + pinned image |
| **Chaos / adversarial dogfooding** | **build** (AQG self synthetic shell/install fixture only; no project-level real chaos) | AQG owns the scenario schema + its own synthetic fixture (drop +x / mutate ENV / PowerShell paste test / git fileMode false and other environment chaos) |
| Property-based testing | schema only | hypothesis runs directly in CI (project-owned); AQG owns the invariant schema + seed/shrink/version/repro fields |
| Simulation environment | schema only | project-owned simulation harness + CI |
| LLM eval / red-team | adapt (standalone) | promptfoo / DeepEval / OpenAI Evals |
| **Local metrics ledger** (local JSONL only, opt-in) | **build** | AQG owns an append-only JSONL ledger at `~/.aqg/metrics-ledger.jsonl`; **explicit opt-in**: enabled via the `--record-metrics` flag or the `AQG_METRICS=1` env var, off by default. **Strict boundary**: no remote send / no raw path / no raw auth-env text / only hash + count + status + version. **Remote telemetry requires a separate ADR** |
| **Multi-agent orchestration / sub-agent invoker** | **schema + sample reference only** | OpenAI Agents SDK / Swarm reference + Claude/Codex built-in spawn capability; AQG **does not bundle an invoker** |
| Docker sandbox / autonomous fix loop | defer | mini-swe-agent / SWE-ReX / OpenHands (evaluation only, throw-away branch, does not enter AQG core) |
| Vector DB recall | defer | revisit when the markdown index is no longer enough |
| Continuity-of-Operation Drill | build | AQG owns the Context Wipe Test; the longer-cycle drill is promoted once stable |
| **AQG dangerous command guard policy + example hook** (policy + example hook spec; **not an active enforcer**) | **build** | AQG owns (1) the policy definition (production write / branch-protection bypass `gh pr merge --admin` / `rm -rf` risk scope / known secret-leak patterns) + (2) an example PreToolUse hook script for agent clients to reuse. Actual interception = the agent client (Claude Code / Codex / others) owns it; AQG holds no runtime enforcement authority. **Does not replace closeout boundary discipline** (the skills' surface boundary still leads); it is a technical policy patch against prompt-level misses |
| **Agent pack behavior tests** (prompt-level smoke) | **build** | AQG owns a behavior test set that verifies the agent pack actually invokes the right skill under the matching trigger conditions. **`scripts/validate_agent_pack.py` checks disk shape / static metadata; behavior tests check runtime behavior under trigger conditions** — the two are complementary |
| **Proactive construction discipline** (construction-time 6-step + anti-pattern blocker + reviewer-objection prediction) | **build** | AQG owns the skill (`aqg-code-construction`) + construction-time hooks (PostToolUse checker / pre-commit gate); `AQG_AGENT` env gating (humans transparent / AI fail-closed) — pushes quality from post-hoc review into the work itself (§2 construction-time) |
| **Decision + knowledge continuity** (durable decisions / memory hygiene / handoff) | **build** | AQG owns an append-only decision LOG (grammar + secret-scan gate, `docs/decisions/LOG.md`) + memory schema/staleness scan + handoff manifest — operationalizes the north star's continuity (§1) and resists knowledge decay (§5 Layer 3) |
| **Agent-lifecycle hook layer** (SessionStart / PreToolUse / PostToolUse / Stop / PreCompact / UserPromptSubmit) | **build** | AQG owns a set of hook scripts (preflight / secret-scan / tamper-guard / construction check / closeout+handoff reminders / WIP checkpoint); **mostly warn-only semantics, hard items fail-closed**; actual interception is still owned by the agent client (§4 boundary preserved) |
| **Security review** (OWASP Top 10 / CWE Top 25 / secure-by-default libraries) | **build** | AQG owns an in-session checklist skill (`aqg-security-review`); **complementary to, not a substitute for** this table's Secret detection + semgrep SAST + external audit |

### Gate error philosophy: fail-closed vs warn-open

The hook-layer row above says "mostly warn-only semantics, hard items fail-closed." Two PreToolUse gates make the split concrete and are deliberately **opposite** when their own machinery degrades. The difference is driven by the cost and reversibility of a miss, not by inconsistency:

| gate | own machinery broken (pattern bank / validator unimportable) | why |
|---|---|---|
| `pretooluse_secret_scan.sh` | **fail-CLOSED** — deny (exit 2) | A secret that lands is **irreversible**: it must be rotated and may already be indexed. If the pattern bank is unimportable, or the tamper canary no longer matches (a sign the bank was disabled), allowing the write risks a leak that cannot be undone — so the gate denies. This also defends against an adversary disabling the scanner. |
| `pretooluse_bash_skill_validator.sh` | **fail-OPEN** — warn + allow (exit 0) | A malformed skill registration is **recoverable** and caught downstream (CI drift test / `aqg_doctor` / review). Blocking every skill edit because the validator is transiently unavailable would halt legitimate work for no safety gain — so the gate warns and allows. |

Both gates fail-OPEN when `python3` is entirely absent (the environment cannot run any Python gate; a visible stderr note is the most either can do), and both **block with exit 2 on an actual violation**. So the philosophy is consistent, not contradictory: **irreversibility + blast radius pick the degraded-mode direction** — fail-closed guards the irreversible (secret leak), warn-open guards the recoverable (skill lint). The same rule sets the sub-agent verify contract in §4 below (high-stakes fail-closed, ordinary fail-open).

## 4. Sub-agent + AQG contract (verifiable provenance)

Every sub-agent claim must carry **verifiable provenance**, not just self-attestation:

1. When the main session dispatches a sub-agent, the prompt explicitly requires "you must call `aqg-startup-preflight` and write the result run_id into the manifest".
2. The sub-agent does the work → the industry tool actually executes (CI / docker / hypothesis).
3. The sub-agent finishes → explicitly calls `aqg-evidence-closeout`; the manifest must contain: actor / parent_session_id / task / result / command_line / tool_version / env_fingerprint / exit_code / artifact_uri / log_digest / seeds / scenario_ids / ci_run_id.
4. The main session receives the manifest → AQG **cross-checks external evidence**:
   - call `gh api` to confirm the ci_run_id really exists and conclusion=success;
   - **cross-check: the commit SHA linked to ci_run_id == the current PR head SHA** (guards against a hallucinated reuse of a historical run_id);
   - **cross-check: the workflow name matches the expected workflow**;
   - fetch the artifact and compare log_digest.
5. Verify failure → **high-stakes fail-closed (block) + mark an INC**; ordinary PRs do not enforce the contract (fail-open = not-required, does not pollute the baseline).

**Signing chain (future)**: GitHub artifact attestation today only auto-signs CI products (OIDC/SLSA) and does not cover a desktop sub-agent's local commands, so the fallback for local work is replayable commands + artifact digests. Once the attestation profile settles (clear producer / subject digest / issuer / repo+ref+workflow constraints / verifier policy), wire up the full `signed_attestation` chain via an ADR.

## 5. 4-Layer model

### Layer 1: environment consistency

| Capability | AQG own | Implementation owner |
|---|---|---|
| Cross-env CI matrix (full) | matrix schema | GitHub Actions (project-owned workflow) |
| **Cross-env minimal compliance smoke** | **build + mandatory** | AQG |
| Pre-commit / pre-push hooks | hook evidence schema + installer | pre-commit framework |
| Static analysis | analyzer-declaration schema | project declares (language-specific) |
| `aqg_doctor --mode {commit,push,pr,session}` | core (session mode shipped; others extended as needed) | — |
| **Surface fingerprint (strict redaction)** | **schema + validator + redaction enforcement** | — |

### Layer 2: task-execution reliability

| Capability | AQG own | Implementation owner |
|---|---|---|
| Skill trigger points | ✅ | — |
| Blast-radius analyzer | analyzer | AQG (review-time) |
| Multi-agent handoff manifest schema + validator | schema + validator + cross-check (commit SHA / workflow name match) | one manifest per PR under `docs/handoff-manifests/` |
| Quality scorecard | template + scoring runner | project-defined weights |
| Failure taxonomy (5 labels) | enforced into the INC template | — |
| Closed-loop smoke framework requirement | enforce schema | project-owned smoke command / `aqg-smoke.yaml` / owner-approved N/A with expiry |

### Layer 3: resistance to system decay

| Capability | AQG own | Implementation owner |
|---|---|---|
| Context Wipe Test | runner + bootstrap verifier | issue pool prep |
| **AQG self synthetic chaos fixture** (not project-level chaos) | scenario schema + light fixture | mandatory before release |
| Incident markdown index + grep recall | sanitized index schema + retention | write INCs (existing convention) |
| **Local metrics ledger** | append-only JSONL at `~/.aqg/`, **explicit opt-in (off by default; see §3)**, strict redaction | AQG own |
| AQG self-improvement loop (weekly retro auto-clustering) | weekly cron + grep + clustering | AQG own |
| **Decision + knowledge continuity** | append-only decision LOG schema (grammar + secret-scan gate) + memory schema/staleness scan + handoff manifest | AQG own — resists **knowledge decay** ("why did we decide this" / is the memory stale / does it reconnect across sessions), same family as resisting system decay |

### Layer 4: runtime contract

AQG owns schemas + validators + required evidence fields + compliance reports; the project owns the real runtime adapters + instrumentation.

| Capability | AQG own | Project own |
|---|---|---|
| Trace id end-to-end | schema + validation that evidence fields are required | real instrumentation |
| Decision replay | schema + replay-log format check | replayer implementation |
| Kill switch / safe mode | required evidence field | real kill-switch code |
| Shadow → canary → rollout | promotion-rule schema + compliance report | rollout pipeline |
| Closed-loop smoke contract | schema + N/A mechanism + expiry | smoke command |

## 6. Surface Fingerprint strict-redaction schema

Desktop-client surfaces carry sensitive data. The doctor `--mode session` fingerprint must be strictly redacted.

### Allowed fields (only these)

- `exists`: boolean
- `size_bytes`: int
- `sha256_first8`: hex string (first 8 chars)
- `*_count`: int (hooks_count / permissions_count / mcp_servers_count / scopes_count / path_dirs_count, etc.)
- `version`: string (CLI version)
- `set` / `is_directory` / `verify_root`: boolean / status enum
- `host`: normalized hostname (github.com / etc.)
- `*_status`: enum status (pass / fail / warn)

### Strictly forbidden to store

- Any raw `.env` value
- Any token / API key / password / credential (even a truncated hash is disallowed, since it is rainbow-table-able)
- Full paths (store only dirs_count; if a path must be stored, store only an anonymized hash)
- The text content of `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` (size + sha256_first8 is fine)
- Raw MCP server connection strings
- The API endpoint URL of a model default (model name only)

### Implementation sample

```json
{
  "claude_settings_json": {
    "exists": true,
    "size_bytes": 1234,
    "sha256_first8": "a1b2c3d4",
    "hooks_count": 1,
    "permissions_count": 5,
    "mcp_servers_count": 3
  },
  "env_AQG_ROOT": {
    "set": true,
    "is_directory": true,
    "verify_root": "pass"
  },
  "gh_auth": {
    "logged_in": true,
    "scopes_count": 3,
    "host": "github.com"
  },
  "path_dirs_count": 12,
  "claude_cli_version": "1.2.3"
}
```

### Validator (belt-and-suspenders, three layers)

- `scripts/_surface_redaction.py`: the fingerprint-output gate — raises + blocks on any forbidden field.
- `scripts/_session_fingerprint.py`: collects the surface and guarantees only the allowlist fields above.
- `scripts/aqg_doctor.py --mode session`: calls the two modules above + at the emit layer sanitizes install-detail paths and sanitizes violation output (never leaks a raw value).

### Full desktop-client surface list

Targeting the common Codex + Claude Code desktop clients:

| Category | Concrete surface | doctor check mode |
|---|---|---|
| User-level prompt | `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` | size + sha256_first8 (no raw content) |
| User-level skills | `~/.claude/skills/` / `~/.codex/skills/` | dir exists + count |
| User-level hooks | `~/.claude/settings.json` | size + sha256_first8 + hooks_count |
| MCP servers | `settings.json` mcpServers | count + name list (normalized) |
| Env vars / `.env` | `$AQG_ROOT` / `$XDG_DATA_HOME` / `$CODEX_HOME` | set + is_directory + verify_root (no value) |
| CLI versions | git / gh / python3 / bash / pwsh | version string |
| Model defaults | Claude / Codex default model | model name only (no endpoint URL) |
| Permissions / sandbox | macOS Full Disk Access / Codex sandbox mode | enum status |
| `PATH` | dirs count (no raw path) | dirs_count |
| Auth state | gh auth / git credential / audit-mcp | logged_in boolean + scopes_count + host |

**Uncontrollable variables**: the LLM models themselves (Anthropic / OpenAI upgrades), client release-behavior changes, real business signals.

## 7. Versioning and update rules

- A version bump goes through an ADR: anything affecting ownership boundaries / the Layer split / how the north-star invariant is operationalized → must have an ADR + dual audit.
- A small change that does not affect the contract → PR + single audit.
- As each block of framework capability lands, update this document's own/owner split for the corresponding Layer (do not record version-specific implementation logs here — those live in commits / PRs / the decision log).
