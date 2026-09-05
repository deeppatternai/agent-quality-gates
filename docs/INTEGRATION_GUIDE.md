# AQG Integration Guide

## What To Install

AQG has three surfaces:

- Codex skills under `skills/`
- Claude Code skills under `agent-packs/claude-code/`
- core scripts and GitHub Actions examples under `scripts/` and `examples/`

The tooling repo is `deeppatternai/agent-quality-gates`.

## Other Machine Setup

Prerequisites:

- access to the private GitHub repo
- `gh auth login` or working SSH credentials
- Python 3
- Codex or Claude Code depending on the target agent

Recommended reviewed-ref install:

```bash
bash -lc 'set -euo pipefail; ref="<reviewed-commit-sha>"; repo="$HOME/.deeppattern/agent-quality-gates"; if [ -d "$repo/.git" ]; then git -C "$repo" fetch --tags --prune origin; else mkdir -p "$(dirname "$repo")"; gh repo clone deeppatternai/agent-quality-gates "$repo"; fi; git -C "$repo" checkout --detach "$ref"; "$repo/scripts/install.sh" --force'
```

Fast internal branch install:

```bash
bash -lc 'set -euo pipefail; repo="$HOME/.deeppattern/agent-quality-gates"; if [ -d "$repo/.git" ]; then git -C "$repo" fetch --prune origin && git -C "$repo" checkout main && git -C "$repo" pull --ff-only; else mkdir -p "$(dirname "$repo")"; gh repo clone deeppatternai/agent-quality-gates "$repo"; fi; "$repo/scripts/install.sh" --force'
```

After Codex install, restart Codex or open a new session so the skill list and managed lifecycle hooks reload. Use `scripts/install.sh --no-hooks` only for an explicit skills-only install.

## Claude Code Setup

User-level install:

```bash
repo="$HOME/.deeppattern/agent-quality-gates"
"$repo/agent-packs/claude-code/install.sh" --scope user --mode link --force
```

Project-level install:

```bash
repo="$HOME/.deeppattern/agent-quality-gates"
"$repo/agent-packs/claude-code/install.sh" --scope project --project-root /path/to/project --mode link --force
```

Restart Claude Code or open a new session after installation.

Optional hook setup:

- Copy `agent-packs/claude-code/hooks/settings.warn-only.example.json` into the target project only after the project owner chooses to use hooks. The JSON delegates to `agent-packs/claude-code/hooks/run_warn_only.sh`, which prints a readable hint to stderr per failed precondition and always exits 0.
- Keep hooks warn-only while calibrating.
- Set `AQG_ROOT` to the local AQG checkout.
- Provide target project `quality-gates.json`.
- Provide `<project>/.aqg/pr-body.md` as the local PR body input; **MUST add `.aqg/` to `.gitignore`** before using hooks (PR body may contain secrets — see `examples/aqg-gitignore.example`).
- AQG itself never modifies the target project `.gitignore`; that's the project owner's call.

### Codex user-level pilot Hooks

The verified Codex hook surface is user-level rather than a project auto-install:

```bash
python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$AQG_ROOT"
```

It merges only the AQG pilot handlers into `${CODEX_HOME:-$HOME/.codex}/hooks.json`,
does not replace other handlers, and writes the reviewed root into the command
entries so desktop Codex does not depend on shell-profile inheritance. Restart
Codex and review/trust the commands in Hooks settings or CLI `/hooks`. This is
not full Claude Hook parity; it currently wires only the live-verified secret
scan and handoff reminder paths.

### Optional: tamper-resistance guard (#328, partial)

`agent-packs/claude-code/hooks/pretooluse_aqg_tamper_guard.sh` denies a `Write`/`Edit`/`MultiEdit` whose target is an AQG gate-bearing file (a hook script under `agent-packs/*/hooks/`, or the secret-pattern / redaction bank under `scripts/`) **under `$AQG_ROOT`, edited from OUTSIDE the AQG checkout** — i.e. the "reach into the AQG install, neuter the secret gate, then write a secret" path.

- **Not in the managed hook set.** A deliberate deny gate's blast radius is an Owner call; opt in by adding a `PreToolUse` (`Write|Edit|MultiEdit`) entry pointing at the script, in the same shape as the managed entries in `settings.blocking.example.json`.
- **Carve-outs (exit 0):** editing AQG from *inside* the AQG checkout (`cwd` under `$AQG_ROOT` — self-development) and a human-pre-set `AQG_AGENT=human-opt-in`.
- **Honest scope — in-band, PARTIAL** (same class as the secret-scan canary): it raises the bar against a *naive* Edit/Write neuter, but does NOT stop an adaptive agent who edits the guard itself, mutates files via Bash (`mv`/`tee`/`python -c`), or manipulates `cwd`. Full tamper-resistance is the read-only / signed install path. Defense-in-depth, not a boundary.

## Project Config

Add `quality-gates.json` to the target project root. Start from:

```text
examples/quality-gates.json
```

Default rollout should be warn-only. Do not make a gate blocking until warn-only evidence is stable and false positives are handled.

AQG itself is the first self-adopted target repo. Its repository-local config is `quality-gates.json`.

## GitHub Actions Setup

For the AQG tooling repo itself, `.github/workflows/quality-gates-warn.yml` is already installed in warn-only mode and uses the repository's own checkout.

Copy one workflow example into the target project only after explicit target-repo owner approval:

- `examples/github-actions/quality-gates-warn.yml`
- `examples/github-actions/quality-gates-blocking.yml`

Required secret for private AQG repo checkout:

```text
AQG_PRIVATE_READ_TOKEN
```

This must be a GitHub App installation token, fine-grained PAT, or deploy key with explicit read access to `deeppatternai/agent-quality-gates`. The target repo default `GITHUB_TOKEN` is not enough to clone AQG.

For real rollout, replace `AQG_TOOL_REF: main` in the copied workflow with a reviewed commit SHA or signed tag.

Record each target rollout under `docs/gate-rollouts/` or the target project's equivalent evidence archive. Use `templates/gate_rollout_record_template.md` as the starting shape.

## GitLab CI / Other CI Setup

AQG is a quality-discipline layer, not a CI/CD platform: it ships the gate CONFIG and your CI runner executes it. The same `run_quality_gates.py` the GitHub Actions examples run works on any CI.

- `examples/gitlab-ci/aqg-quality-gates.gitlab-ci.yml` — the GitLab CI sibling of the warn-only workflow. **Copy it into your top-level `.gitlab-ci.yml`** (do not rely on `include:` alone — GitLab creates merge-request pipelines only from rules in the top-level config). Set a CI/CD variable `AQG_PRIVATE_READ_TOKEN` (a GitHub read token for `deeppatternai/agent-quality-gates`): mark it **Protected** — that is the *access boundary* for this long-lived PAT, keeping it off non-protected and fork pipelines — and also Masked. **Masking is log-redaction only, NOT access control**: a Masked-but-not-Protected variable is readable by any same-project member's branch pipeline. Where Protected withholds the token (unprotected MR source branch) the job skips warn-only and never clones (nothing to leak); keep "run fork MR pipelines in the parent project" disabled. The job uses job-scoped temp git state (no shared `$HOME` mutation). It is a **reference template**: run `gitlab-ci lint` + a real MR on your instance before relying on it (Protected-variable exposure, fork policy, and masked-variable character rules are GitLab-version/setting-dependent). Commit `quality-gates.json`.

For CircleCI / Jenkins / any other runner, follow the same shape: clone the AQG tooling repo, run `scripts/run_quality_gates.py --config quality-gates.json --repo . --pr-body-file <body> --output-json <out>`, treat exit 0/1 as warn-only, and surface the JSON. The GitHub PR-comment scripts (`fetch_pr_body.py` / `post_pr_comment.py`) are GitHub-API-specific; posting a native MR/PR note on another platform is an optional API extension. The same `AQG_TOOL_REF` SHA-pinning and rollout-record discipline above applies.

## When A Gate Fails

Use the finding message, suggestion, and config key from the AQG summary. Fix the PR evidence or adjudication table first. If the gate behavior itself is wrong, add a bugfix record from:

```text
templates/bugfix_record_template.md
```

Do not loosen global AQG scripts for one project. Project-specific policy belongs in that project `quality-gates.json`.

## Boundaries

This guide does not authorize:

- changing target repo CI without owner approval
- branch protection changes
- repo visibility changes
- secrets or credential creation
- production deploys, restarts, or runtime writes
- storing raw private data in AQG artifacts
