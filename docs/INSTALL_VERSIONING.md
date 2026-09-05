# AQG Install Versioning

## Purpose

Current AQG version is recorded in the repository root `VERSION` file. `python3 scripts/run_quality_gates.py --version` reads that package version.

Developer machines and CI should be able to install from a reviewed ref instead of always following `main`.

Use branch installs only for fast internal iteration. Use a pinned commit SHA or reviewed release tag for repeatable team setup, CI examples, and any future blocking rollout.

## Pinned Commit Install

Codex skills, symlink mode:

```bash
scripts/install_from_ref.sh \
  --repo deeppatternai/agent-quality-gates \
  --ref <reviewed-commit-sha> \
  --dest "$HOME/.deeppattern/agent-quality-gates"
```

Codex skills, copy mode:

```bash
scripts/install_from_ref.sh \
  --repo deeppatternai/agent-quality-gates \
  --ref <reviewed-commit-sha> \
  --dest "$HOME/.deeppattern/agent-quality-gates" \
  --copy
```

Clone only, without touching local Codex skills:

```bash
scripts/install_from_ref.sh \
  --repo deeppatternai/agent-quality-gates \
  --ref <reviewed-commit-sha> \
  --dest /tmp/aqg-install-test \
  --skip-codex-install
git -C /tmp/aqg-install-test rev-parse HEAD
```

Private repo access must already be configured through `gh auth login`, SSH, or an approved read-only GitHub credential.

## Check And Update Main

For an installed checkout that follows `main`, upgrade with one command. It compares the local checkout against `origin/main`, fast-forwards only when changed, reinstalls skills (pruning residue from renamed/removed skills), installs or refreshes supported AQG hooks by default, and runs doctor. Pass `--no-hooks` only for an explicit skills/rules-only upgrade:

```bash
"$HOME/.deeppattern/agent-quality-gates/scripts/upgrade.sh"
```

`upgrade.sh` runs from inside an existing checkout — bootstrap the first clone via the Pinned Commit Install above (or the README first-install). Pin a release instead of `main` with `scripts/upgrade.sh --ref <tag-or-sha>`. Use reviewed commit SHAs or signed tags instead of the `main` flow for repeatable CI setup or future blocking rollout.

## Client packs and Hooks

For a new dual-client user, use the supported one-command installer after
cloning or installing AQG from a reviewed ref. It installs the client-specific
skills plus Claude Code's managed Hooks and Codex's WS-6 pilot Hooks:

```bash
"$AQG_ROOT/scripts/install_aqg.sh" --yes --clients claude,codex
```

Use the low-level Claude installer only when deliberately managing that pack
alone:

```bash
"$HOME/.deeppattern/agent-quality-gates/agent-packs/claude-code/install.sh" \
  --scope user \
  --mode link \
  --force
```

Project-local Claude Code install:

```bash
"$HOME/.deeppattern/agent-quality-gates/agent-packs/claude-code/install.sh" \
  --scope project \
  --project-root /path/to/project \
  --mode link \
  --force
```

The managed Claude Code hook installer is the default path for supported user installs. Hook examples remain available only for manual warn-only or project-specific experiments:

```text
agent-packs/claude-code/hooks/settings.warn-only.example.json
```

There is no verified warn-only Codex equivalent. The Codex pack is intentionally
limited to its tested event surface, so do not represent it as full Claude Hook
parity. Set `AQG_ROOT` to the reviewed local checkout before using either
low-level installer:

```bash
export AQG_ROOT="$HOME/.deeppattern/agent-quality-gates"
```

## Release Tags

Signed release tags are planned but not required for the current rollout. Until tag policy is accepted, use reviewed commit SHAs for reproducible installs.
