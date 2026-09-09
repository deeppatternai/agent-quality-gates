---
name: aqg-startup-preflight
description: Verify worktree + GitHub live state (dirty git, gone upstream, behind remote, missing context files, open PRs/issues, stale status docs) at SESSION START for AI team project work, before the first code edit. Use exactly once per session, not for repeated mid-session checks; not after audit/review returns. Defaults to the current git root and supports explicit repo paths.
---

# AQG Startup Preflight

Use this skill before acting in a project where stale repo state or authorization boundaries matter. Its job is to make dirty-worktree, upstream, GitHub, and required-file risk visible before Codex edits files or gives a production-adjacent conclusion.

## Workflow

1. Start from the current git root unless the user names a repo path. Read project entry files when they exist, especially `AGENTS.md`, `CODEX.md`, `docs/SESSION_START_PROTOCOL.md`, `docs/PROJECT_STATUS.md`, and `docs/STACK_STATUS.md`.
2. Run the preflight helper:

   ```bash
   source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
     echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
     exit 2
   }
   script="$aqg_root/skills/aqg-startup-preflight/scripts/aqg_preflight.py"
   python3 "$script" --repo "${CLAUDE_PROJECT_DIR:?CLAUDE_PROJECT_DIR is required}"
   ```

   By default this uses the current git root and runs `git fetch origin`, which updates local remote-tracking refs under `.git/refs/remotes/*` and may use network credentials. Use `--no-fetch` when the user wants no network, no git-ref updates, or a fully offline/sandboxed pass. Use `--repo /path/to/repo` one or more times for multi-repo work, and `--required-file path/from/project/root` for project-specific context files that must exist.

   **Session dedup (§10.1):** because the SessionStart hook auto-runs preflight AND this rule tells you to invoke it, a second same-session run within `AQG_PREFLIGHT_DEDUP_WINDOW_S` (default 300s; `0` disables) short-circuits with a "skipped (session dedup)" note pointing at the earlier output — worktree/GitHub state is assumed unchanged. Pass `--force` to run anyway (the SessionStart hook always does, so each real session gets one fresh preflight; only a `--force` run writes the marker). The SKIP note is an optimization, not a clean bill of health — if no earlier report is in context, re-run with `--force`. Override the marker location with `AQG_PREFLIGHT_STATE_DIR` (defaults to a user-private `~/.cache/aqg` or `$XDG_CACHE_HOME`, never world-writable `/tmp`).
3. Treat these as hard blockers before edits:
   - Current repo or target repo is dirty and the task needs repo changes.
   - Current branch tracks a gone upstream.
   - Local branch is behind its tracked upstream (`@{u}`) and the task depends on it. (For a non-main branch, drift versus `origin/main` is a separate manual check the helper does not compute.)
   - The task is production-adjacent and no fresh authorization boundary is established.
4. If a blocker exists, either create a clean remote-baselined worktree or stop with the exact blocker. Do not "just patch" the dirty checkout.
5. If no blocker exists, continue with the narrowest executable next step.

## Boundary Rules

- The Python CLI also nudges AQG's signed updater in a detached background process on managed installs (Windows/macOS/Linux). It may update AQG's own installation and state, never the target project's source. It does not wait for updating; import/launch failures do not affect the report. Set `AQG_NO_UPDATE_CHECK=1` for strictly update-free invocation. Merely reading this skill does not trigger it.

- Live GitHub/git facts beat memory and old status notes.
- Do not infer merge state from previous sessions; verify PRs/issues live.
- Keep repo-only/offline, read-only production evidence, production authorization, and runtime closeout as separate gates.
- Do not prepare or execute production read/write actions unless the user explicitly authorizes that boundary in the current session.
- This preflight only surfaces local git, GitHub, and required-file state. Architecture, production, secrets, raw/private data, Owner/admin, and project-specific authorization gates remain separate checks.
- By default it runs `git fetch origin`, which updates remote-tracking refs under `.git/refs/remotes/*` (pass `--no-fetch` to skip). This does not edit project source. The sidecar conservatively declares `writes-code` because the detached updater can replace AQG's own installed code and refresh AQG-owned routes/hooks; it does not authorize edits to the target project's code.
- When other agent sessions may operate on this repo concurrently, work in an isolated `git worktree` (or a separate clone), not the shared checkout: a shared tree pollutes `git status` and test results with another session's uncommitted changes and races on HEAD/index. Trust verification only when it runs on an isolated base carrying your changes alone; if you must share a tree, gate on explicit refs (not `HEAD`), stage only your own files, and prefer single-target commands (e.g. `regen <skill>`, not a repo-wide `regen --all`) that will not rebuild another session's wrapper from its uncommitted source.

## Reporting Shape

Report:

```markdown
Preflight:
- repo: <branch/upstream/dirty/behind>
- recent commits: <top of `git log --oneline --no-decorate -5` — catches a stale handoff baseline>
- GitHub: <open PRs/issues that matter>
- blocker: <none or exact blocker>
- next safe step: <one concrete action>
```
