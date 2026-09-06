# Contributing to AQG

English | [中文](CONTRIBUTING.zh-CN.md)

> For both people **and** AI sessions working on AQG. **Start with "Two-repo model" below** — it
> decides whether you can contribute code directly at all. Maintainers developing in the private
> upstream should then read the parallel-worktree discipline (AQG often has multiple AI sessions
> developing in parallel).

## Two-repo model: the public repo is a read-only mirror

AQG is developed under a **two-repo governance model**:

- **Private upstream** — the single place all development happens (where this file is authored).
- **Public mirror** ([`deeppatternai/agent-quality-gates`](https://github.com/deeppatternai/agent-quality-gates))
  — **regenerated one-way from the private source on every release**. The public tree is a carved
  snapshot; it is never merged back.

**If you are viewing the public mirror, it is read-only:** pull requests and direct pushes are **not
accepted** — a release-time re-carve would overwrite them. Instead, please open an **issue** or a
**discussion** on the public repo; bug reports, feature ideas, and questions are welcome there and
get triaged into the private upstream. Everything below is for maintainers working in the private
upstream.

## Parallel AI sessions: each uses its own isolated git worktree (important)

**Do not let multiple sessions share the same worktree.** They will contend over the same `HEAD`:
when one session runs `git checkout` to switch branches, it changes the HEAD that every session
sharing that worktree sees, and uncommitted edits will **bleed into someone else's branch**
(observed in practice on 2026-06-10: one session's HEAD was switched back and forth 3 times by a
parallel session in the shared tree, and uncommitted changes briefly landed on someone else's branch).

**Rule**: each parallel session opens its own isolated worktree from the start:

```bash
# Create an isolated worktree + isolated branch from the latest main
git worktree add ../aqg-<session-name> -b <your-branch> origin/main
cd ../aqg-<session-name>
# …do your work here: edit / commit / push / open PR…
# When done (after the PR is merged), clean up
git worktree remove ../aqg-<session-name>
```

An isolated worktree = an isolated `HEAD` + an isolated index, so there is no contention or bleed at
the root. Claude Code users can also have the session `EnterWorktree` at the start (same effect).

**If you really must share one worktree** (not recommended): strictly **commit-before-switch** — commit
each logical change immediately, **explicitly stage specific files** (do not `git add -A`), confirm
`git status` is clean before switching branches, and minimize the uncommitted window. Verify others'
changes with an explicit ref (e.g. `origin/main`), not `HEAD`.

## Changing an AQG skill → sync the three-piece set

After changing `skills/aqg-*/SKILL.md` or a sidecar (`description.md` / `triggers.md`), you **must run**:

```bash
python3 scripts/aqg_skill_gen.py regen --all   # sync the generated wrapper
```

and commit the changed wrapper along with it. CI's **"Wrapper generated-artifact gate"** will reject
out-of-sync state (skipping it locally = red CI). Adding / changing a skill also requires running
`python3 scripts/aqg_skill_validator.py --strict <skill-name>`. See
[`docs/SKILL_AUTHORING_GUIDE.md`](docs/SKILL_AUTHORING_GUIDE.md) for details.

## audit / testing discipline

- **Before committing a change**, choose the audit depth per
  [`docs/AUDIT_DECISION_MODEL.md`](docs/AUDIT_DECISION_MODEL.md) (the code external-audit chokepoint =
  the audit-before-commit gate in `aqg-code-construction`).
- Tests follow **vertical-slice TDD** (one test → one implementation, no horizontal batching); behavior
  tests are a CI gate: get `python3 -m pytest tests/behavior/ -q` green locally before pushing.
- A bare `pytest` at the repo root does **not** cover the per-skill `scripts/self_test.py` files —
  they share a basename, so they are excluded from collection (see `pytest.ini`) and gated separately
  by `bash scripts/run_skill_self_tests.sh`. Every root run prints that reminder in its header. If you
  add or move a `self_test.py`, keep the globs in `pytest.ini` and in that script identical.
- For human-facing artifacts (README / guides / reports), additionally review once from a **reader's
  perspective** (guard against over-engineering).

## Other guides

- [`docs/AUDIT_DECISION_MODEL.md`](docs/AUDIT_DECISION_MODEL.md) — single source of truth for audit orchestration
- [`docs/SKILL_AUTHORING_GUIDE.md`](docs/SKILL_AUTHORING_GUIDE.md) — writing / changing skills
- [`docs/INTEGRATION_GUIDE.md`](docs/INTEGRATION_GUIDE.md) — integration
- [`AI_SETUP.md`](AI_SETUP.md) — wiring AQG into CLAUDE.md / AGENTS.md (fed to AI for self-install)
