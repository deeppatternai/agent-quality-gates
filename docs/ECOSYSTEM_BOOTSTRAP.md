# Agent Quality Gates — New Machine Setup Guide

English | [中文](ECOSYSTEM_BOOTSTRAP.zh-CN.md)

> Scope: macOS (Linux adaptation OK; Windows out of scope)
>
> This document is the single source of truth for **standing up Agent Quality Gates (AQG) on a new machine**. For AQG's own version management, see [INSTALL_VERSIONING.md](./INSTALL_VERSIONING.md). For the recommended complementary tool stack (SAST, production feedback, deterministic checks), see [ECOSYSTEM_MUST_INSTALL.md](./ECOSYSTEM_MUST_INSTALL.md).

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Setup Flow](#setup-flow)
- [Secret Management Principles](#secret-management-principles)
- [Verification Checklist](#verification-checklist)
- [Common Failures](#common-failures)

---

## Prerequisites

The new machine must already have:

| Tool | Verification command | Notes |
|---|---|---|
| macOS 14+ | `sw_vers` | Linux adaptation available, Windows out of scope |
| A host coding agent | — | Claude Code (`claude --version`, from [claude.ai/download](https://claude.ai/download)) and/or Codex |
| git | `git --version` | Usually preinstalled |
| Python 3.11+ | `python3 --version` | Required by AQG helper scripts |
| GitHub account | Browser login / `gh auth login` | Used to clone `deeppatternai/agent-quality-gates` |

Optional (only needed if you adopt the corresponding complementary tool from [ECOSYSTEM_MUST_INSTALL.md](./ECOSYSTEM_MUST_INSTALL.md)):

| Tool | Verification command | Notes |
|---|---|---|
| Homebrew | `brew --version` | Required to install semgrep / other tools locally |
| Node.js 20+ / npm | `node --version` | Required to install pyright locally |

---

## Setup Flow

### Step 1: Clone the AQG repo

The local stable install path is uniformly `$HOME/.deeppattern/agent-quality-gates`. Set `AQG_ROOT` to it:

```bash
export AQG_ROOT="$HOME/.deeppattern/agent-quality-gates"
mkdir -p "$(dirname "$AQG_ROOT")"
gh repo clone deeppatternai/agent-quality-gates "$AQG_ROOT"
cd "$AQG_ROOT"
git checkout main   # or a specific release tag (check with cat "$AQG_ROOT/VERSION")
```

### Step 2: Install the AQG skills

Run the installer for whichever host agent you use (either or both):

```bash
# Codex skills
"$AQG_ROOT/scripts/install.sh" --force

# Claude Code agent pack
"$AQG_ROOT/agent-packs/claude-code/install.sh" --scope user --mode link --force
```

The default install links the skills into the host agent's skill directory; a later `git pull` in this repo (or `scripts/upgrade.sh`) updates everything. After installing, restart the host agent or open a new session so the skill list reloads.

### Step 3: Install the resident hooks

AQG designs its key gates (session-start preflight, handoff enforcement, pre-commit skill validation, completion closeout reminder, etc.) as **resident hooks** — without them, the skills only trigger when the model "remembers" to. One command installs the full set (edits `~/.claude/settings.json`, supports `--uninstall`):

```bash
python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --apply
```

The hooks read `AQG_ROOT` at runtime and **silently skip if it's unset (installed = no-op)** — so be sure to export `AQG_ROOT` in the environment where the host agent runs (see Step 4). For a never-blocking starter, copy or adapt `agent-packs/claude-code/hooks/settings.warn-only.example.json` instead.

> **Before using hooks, add `.aqg/` to each target project's `.gitignore`**: a warn-only hook reads `<project>/.aqg/pr-body.md` as a local "PR body" markdown; if it contains secret-like content the secret scan surfaces it, but the raw text still lands on disk. You can `cat examples/aqg-gitignore.example` into the target project's `.gitignore`.

### Step 4: Export `AQG_ROOT` for the host environment

`settings.json`'s env field feeds hook subprocesses, and `~/.zshrc` feeds the interactive shell; **configure both to avoid behavior differences between GUI startup and terminal startup** (launching a GUI app does not read `~/.zshrc`).

Add to `~/.claude/settings.json` (absolute path — settings.json does not expand shell variables):

```json
{
  "env": {
    "AQG_ROOT": "<absolute path from Step 1 above>",
    "AQG_METRICS": "1"
  }
}
```

Add to `~/.zshrc` (belt and suspenders):

```bash
cat >> ~/.zshrc <<'EOF'

# Agent Quality Gates
export AQG_ROOT="$HOME/.deeppattern/agent-quality-gates"
export AQG_METRICS=1
EOF
source ~/.zshrc
```

### Step 5: Verify with the doctor

```bash
python3 "$AQG_ROOT/scripts/aqg_doctor.py" --no-cli
```

Then exit the current session, start a new one, and run a quick panorama self-check:

```bash
bash -c '
cd "$AQG_ROOT"
echo "=== AQG skills installed ===" && ls "${CODEX_HOME:-$HOME/.codex}/skills" 2>/dev/null | grep -c "aqg-" | xargs echo "  aqg-* skills:"
echo
echo "=== Local binaries ==="
for cmd in python3 git gh claude; do
    p=$(which $cmd 2>/dev/null) && echo "  ✅ $cmd → $p" || echo "  ❌ $cmd not installed"
done
'
```

### Optional: complementary tools

The complementary tool stack (semgrep SAST, GitHub official MCP for Actions logs, Sentry production feedback, pyright type feedback) is documented separately in [ECOSYSTEM_MUST_INSTALL.md](./ECOSYSTEM_MUST_INSTALL.md); install only what your workflow needs. **semgrep local SAST needs no account** — it runs fully locally against local / OSS rulesets; a `SEMGREP_APP_TOKEN` is only for the optional hosted Semgrep platform.

---

## Secret Management Principles

| Secret | Reuse across machines? | Why |
|---|---|---|
| GitHub OAuth token | ❌ Authorize separately per machine | The OAuth flow is bound to the device; do not copy the OAuth from `~/.claude.json` |
| API keys (e.g. `ANTHROPIC_API_KEY`) | ⚠️ Depends | Reusable for personal development; use IAM isolation for team deployments |
| Any optional tool token (e.g. the hosted-platform `SEMGREP_APP_TOKEN`) | ❌ Create one per machine | Trace access per device; revoking affects only one machine |

**Never paste any secret into the host agent's conversation** — use `pbpaste` or `read -s` with a temporary variable and write it into `~/.claude/settings.json`'s `env` field, never into the conversation.

---

## Verification Checklist

After installation, the new machine should satisfy:

- [ ] `aqg-*` skills visible in the host agent's skill directory (Step 5 self-check)
- [ ] `python3 scripts/aqg_doctor.py --no-cli` passes
- [ ] The resident hooks are installed (or the warn-only example is in place)
- [ ] The `AQG_ROOT` env is visible in hook subprocesses (not relying on lucky inheritance from `~/.zshrc`)
- [ ] `~/.aqg/metrics-ledger.jsonl` has a new entry after a new session starts (proving the hook actually ran)
- [ ] `.aqg/` is in each target project's `.gitignore`

---

## Common Failures

### Q: AQG hook reports "AQG_ROOT not set"?
A: Check the `settings.json` env field (not just `~/.zshrc`). Launching the host agent via the GUI does not read `~/.zshrc`; hook subprocesses must get `AQG_ROOT` from the `settings.json` env field. The hooks are designed to no-op silently when `AQG_ROOT` is unset, so a missing export means the gates simply never fire.

### Q: Skills don't show up after install?
A: Restart the host agent or open a new session so the skill list reloads. For Claude Code you can also `/reload-plugins`. Confirm the install linked into the expected directory (`${CODEX_HOME:-$HOME/.codex}/skills` for Codex; the agent pack install path for Claude Code).

### Q: `aqg_doctor.py` reports a stale or drifted install?
A: Run `scripts/upgrade.sh` to bring the local checkout up to `origin/main` (ff-only) and refresh the skills; it exits non-zero when tracked local changes block the update.

### Q: A hook is blocking when you wanted warn-only?
A: The default hook set enforces the key gates. To switch to never-blocking, install from `agent-packs/claude-code/hooks/settings.warn-only.example.json` (delegates all precondition checks to `run_warn_only.sh`, which prints a hint to stderr and always exits 0).

---

## References

- [ECOSYSTEM_MUST_INSTALL.md](./ECOSYSTEM_MUST_INSTALL.md) — recommended complementary tool stack (SAST, production feedback, deterministic checks)
- [INSTALL_VERSIONING.md](./INSTALL_VERSIONING.md) — AQG's own version management (branch / tag / sha pinning)

---

## Maintenance

- After each AQG release, update the tag reference in Step 1 (or just track `main` and use `scripts/upgrade.sh`)
- Re-run `scripts/aqg_doctor.py --no-cli` after upgrades to confirm the install is healthy
