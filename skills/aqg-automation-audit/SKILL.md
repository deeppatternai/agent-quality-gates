---
name: aqg-automation-audit
description: Inventory automation stack (hooks / MCP / plugins / skills / env) + overlap-check vs AQG workflow + emit structured verdict in any host. Use PROACTIVELY when installing or updating a plugin / marketplace, suspecting a hook is interfering with AQG flow, seeing repeated system-reminder injections (typical noise signal), running monthly automation hygiene, OR before evidence-closeout if the automation stack changed mid-session. Advisory + read-only — it inventories and emits verdicts (keep / migrate / disable / fix); the human applies any change — this skill never modifies hooks, plugins, MCP, or skills. Defaults to current host (`~/.claude`) and supports `--skill-root` override.
---

# AQG Automation Audit

Inventory + overlap check + verdict skill. **Advisory only**: it scans deterministic state, classifies each item, and emits a structured verdict (`keep` / `migrate` / `disable` / `fix`). It never applies changes — the human reviews the verdict and acts (renaming, disabling, or reconfiguring the offending item themselves).

## How To Run

Resolve the AQG root via the shared helper, then run the inventory + overlap-check helpers:

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script_dir="$aqg_root/skills/aqg-automation-audit/scripts"
python3 "$script_dir/inventory.py" --json > /tmp/aqg_inv.json
python3 "$script_dir/overlap_check.py" --inventory /tmp/aqg_inv.json
cat "$aqg_root/skills/aqg-automation-audit/templates/audit_verdict.md"
```

## 6-step Workflow

1. **Inventory Capture** (`inventory.py`) — deterministic sources, never LLM-described:
   - **hooks**: `~/.claude/settings.json` user `hooks` field + `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/hooks/hooks.json` × N plugins
   - **MCP**: settings.json `enabledPlugins` + (best-effort) `claude mcp list` subprocess output
   - **plugins**: `~/.claude/plugins/installed_plugins.json`
   - **skills**: `~/.claude/skills/*/SKILL.md` + `~/.claude/plugins/cache/<m>/<p>/<v>/skills/*/SKILL.md` (excluding `.disabled-by-eaf` suffix dirs)
   - **env**: `~/.claude/settings.json` `env` field — **keys only, values redacted**
   - **secret redaction (capture-time)**: every free-text field that can carry a token — hook `command` strings (user + plugin), MCP `raw` / `stderr`, and the plugin install-path tail — is passed through a redaction helper (`inventory._redact`) **at capture time**, so the emitted JSON never carries a raw secret. Distinctive-prefix patterns (AWS / GitHub / OpenAI / Slack / Google), bearer tokens, `token|api_key|secret|password=` assignments, and URL userinfo (`://user:pass@`) are replaced with `<redacted>`; ordinary commands pass through unchanged. `overlap_check.py` reads the already-redacted inventory, so `command_hint` / `rule` inherit the redaction.
2. **State Classification** — each item flagged:
   - `live` — installed + connected + not disabled
   - `broken` — installed but fails (e.g. MCP OAuth pending; hook missing required env)
   - `redundant` — multiple sources provide the same capability (e.g. another plugin's verification-loop skill vs `aqg-evidence-closeout`)
   - `missing` — expected but absent (e.g. `AQG_ROOT` env unset)
   - `dormant` — installed but no trigger condition met (e.g. skill description never matches)
   - `disabled` — explicitly turned off (`.disabled-by-eaf` suffix or env kill switch)
3. **Overlap Analysis** (`overlap_check.py`) — flag conflicts vs AQG workflows. The conflict rules below run against **plugin** hooks (the noise/blocking source); **user-level hooks** (`settings.json`) are surfaced **informational-only by design** — the user chose them deliberately, so they are listed (`category: user_hook`, `severity: cosmetic`) with their matcher for review, but not matched against the conflict rules:
   - PreToolUse plugin hook on `Bash` / `Edit` / `Write` matcher → conflicts with `aqg-code-construction`
   - PostToolUse plugin hook on `Edit` / `Write` matcher → conflicts with `aqg-evidence-closeout`
   - UserPromptSubmit plugin hook injecting text → **noise candidate** (system-reminder pollution)
   - SessionStart plugin hook duplicate initialization (≥2 distinct **live** plugins emitting the same command base — stale cache versions of a single plugin are excluded; see liveness cross-join below)
   - skill `description` containing AQG-routing phrases (`verification routing`, `verify before`, `debug workflow`, `debugging session`, `audit findings`, `audit adjudication`, `checklist before commit`, `closeout`, `evidence-back`, `fact-forcing`) → conflicts AQG 6-piece suite description-match routing. Matching is on reserved **phrases**, not bare base words; a bare base word alone (e.g. "debug" in unrelated context, or "verification" in "email verification tool") is **not** flagged, to avoid false positives. The full frontmatter description is analysed (capture limit raised so a reserved phrase past char 200 is still caught).
   - env field: `AQG_ROOT` missing → blocking; `AQG_METRICS` missing → noise. Allowlisted keys emit a derived state (`set` / `empty` / `on` / `off`) only — raw values are never exposed.
   - **Plugin-hook liveness cross-join (state-aware filtering)**: a plugin hook is reported as a **live** conflict only after cross-joining its plugin against `enabledPlugins` (is the plugin enabled?) + `installed_plugins.json` (is this cache version the installed-current one?). A hook whose plugin is **disabled** (absent from a non-empty `enabledPlugins`) or whose cache dir is a **stale** superseded version is downgraded to `severity: cosmetic` with a `liveness: disabled|stale` field and a `— NOT LIVE (…)` rule suffix, instead of a false `blocking` / `noise`. **Fail-safe**: only positive evidence downgrades — an empty `enabledPlugins` (no data) is not read as disabled, and an unknown installed-current version is not read as stale, so a genuine conflict is never silenced (the skill errs toward over-reporting). (Assumption: `enabledPlugins` is read from whatever settings scope the inventory captured — `--skill-root`, default `~/.claude`; a plugin enabled only in a *different* scope reads as disabled here. Narrow in practice, since a host-cache hook belongs to a host-scope plugin, and the empty-`enabledPlugins` fail-safe still prevents silencing.) SessionStart duplicate detection likewise counts **only live** hooks, so one plugin's old + new cache dirs no longer self-report a phantom cross-plugin duplicate.
4. **Risk Assessment** — severity per overlap:
   - `blocking` — directly halts AQG flow (e.g. a PreToolUse hook blocking Bash)
   - `noise` — does not block but pollutes context (e.g. UserPromptSubmit injecting reference list each turn)
   - `silent` — silently hijacks routing (e.g. another plugin's skill whose description matches closeout intent)
   - `cosmetic` — no behavioral collision: a description-only overlap, OR a plugin hook present in cache but unable to fire (disabled plugin / stale-cache version, carrying a `liveness` field)
5. **Verdict** — four-state structured table per item (`templates/audit_verdict.md`):
   - `disable` — recommend disabling the conflicting item (the human renames or turns it off)
   - `migrate` — vendor the capability into AQG (file as a follow-up task)
   - `keep-as-tool` — retain as a domain tool library (e.g. a language-specific reviewer for that stack)
   - `fix` — repair config (e.g. set missing env var, complete OAuth, fix broken connector)
6. **Evidence Hand-off** — emit `.aqg/automation_audit_<date>.json` inventory snapshot + filled `verdict.md`, both importable into `aqg-evidence-closeout` ledger.

## Decision Output Shape (closeout-importable)

> **Provenance**: the YAML below is the **agent-filled verdict** (the reviewer fills it from `templates/audit_verdict.md` after running the scripts) — no script emits this `automation_audit` payload directly. The two scripts emit raw inputs to it: `inventory.py` emits the **JSON snapshot** (`.aqg/automation_audit_<date>.json`), and `overlap_check.py` emits the **findings table** (markdown, or a flat `{"findings": [...]}` JSON via `--json`). The agent merges snapshot counts + findings + state classification into the verdict shape below.

```yaml
automation_audit:
  audited_at: 2026-05-05T10:30:00Z
  inventory_snapshot: .aqg/automation_audit_2026-05-05.json
  counts:
    hooks: { live: 4, broken: 0, redundant: 0, disabled: 2 }
    mcp: { live: 5, broken: 1 }
    plugins: { enabled: 7, disabled: 0 }
    skills: { live: 184, disabled: 8, redundant: 0 }
    env: { present: 6, missing: 0 }
  verdicts:
    - item: example-plugin:example-skill
      category: skill
      state: disabled
      overlap_with: aqg-code-construction
      severity: blocking
      verdict: disable  # recommend the human turn it off
      action: none
    - item: github-official
      category: mcp
      state: broken
      severity: blocking  # whenever GitHub Actions raw logs needed
      verdict: fix
      action: complete OAuth on next session restart
  decision: clean  # one of: clean | needs-action | needs-owner-decision
```

## Boundaries

- **Advisory + read-only**: never modifies hooks / plugins / MCP configs / skills directories. This skill only emits verdicts; the human applies any change.
- Production / deploy / restart / credentials / raw private data / Owner-admin actions remain separate authorization gates.
- **Owner-only 5 items** — escalate; never decide:
  1. business direction (which markets, which features, ship vs cut)
  2. strategy (build vs buy, vendor selection, architecture pivot)
  3. taste (UX / brand / wording / aesthetic judgment)
  4. tacit knowledge (unwritten team conventions, project history)
  5. long-horizon + irreversible actions (production deploy, credential rotation, data migration, irreversible architecture decisions)
- Token / secret values in env field: **keys only** in inventory output (values redacted).

## References

- Complementary AQG layers: `skills/aqg-evidence-closeout/SKILL.md` (ledger import target), `skills/aqg-systematic-debugging/SKILL.md` (6-step style)
