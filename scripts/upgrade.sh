#!/usr/bin/env bash
set -euo pipefail

# AQG one-command upgrade.
#
# Replaces the multi-step manual flow (fetch/pull + Codex install + Claude
# install + doctor) that is easy to get wrong. Each step is idempotent and
# adaptive: only the clients actually present on this machine are touched.
#
#   scripts/upgrade.sh                 # follow main: pull + reinstall skills + doctor
#   scripts/upgrade.sh --ref v0.8.4    # pin to a reviewed tag / commit instead of main
#   scripts/upgrade.sh --clean-only    # skip git; just reinstall (prunes residue) + verify
#   scripts/upgrade.sh --hooks         # force install/refresh Codex + Claude Code hooks
#
# Supported managed hook sets are installed/refreshed by default. `--hooks`
# forces the refresh path; `--no-hooks` is the explicit skills-only path.
#
# What it does NOT do: edit production, secrets, or branch protection; nothing
# outside ${CODEX_HOME:-~/.codex}/skills, ~/.claude/skills, and (unless
# --no-hooks is used) ~/.claude/settings.json or ${CODEX_HOME:-~/.codex}/hooks.json. Skill (re)install prunes only stale symlinks
# that point back into this repo — never real directories or third-party links.

usage() {
  cat <<'EOF'
Usage: scripts/upgrade.sh [--ref REF] [--clean-only] [--hooks | --no-hooks]
                          [--no-codex] [--no-claude] [-h|--help]

  --ref REF      Check out REF (tag or commit) instead of pulling main.
  --clean-only   Skip the git update; only reinstall/prune + verify current checkout.
  --migrate      Move this install onto the managed-update layout: AQG_ROOT becomes
                 a symlink into versions/<commit>/. ONE-WAY. Shows what it would do
                 and asks before moving anything. Required before the managed
                 update engine can apply anything to this install.
  --hooks        Force install/refresh both Codex and Claude Code managed hooks.
  --no-hooks     Skip all hook steps entirely.
                 Default (neither flag): on a machine WITH Claude Code, install the
                 managed hook set on a fresh machine, refresh it if already installed,
                 and leave an explicit warn-only opt-in untouched.
                 For Codex, install or refresh the managed set, and fail loudly
                 when an existing hooks.json is invalid.
  --no-codex     Skip Codex skill (re)install even if ~/.codex exists.
  --no-claude    Skip Claude Code skill (re)install even if ~/.claude exists.
  -h, --help     Show this help.
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
original_args=("$@")

ref=""
clean_only="0"
do_migrate="0"
hooks_mode="auto"   # default-on: install on fresh / refresh managed / respect warn-only; --hooks=force, --no-hooks=skip
do_codex="1"
do_claude="1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref)
      [[ $# -ge 2 ]] || { echo "ERROR: --ref requires a value" >&2; exit 2; }
      ref="$2"; shift 2 ;;
    --clean-only) clean_only="1"; shift ;;
    --migrate)    do_migrate="1"; shift ;;
    --hooks)      hooks_mode="force"; shift ;;
    --no-hooks)   hooks_mode="skip"; shift ;;
    --no-codex)   do_codex="0"; shift ;;
    --no-claude)  do_claude="0"; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# A ref must never look like a git flag (e.g. --ref -f → `git checkout -f`).
if [[ -n "$ref" && "$ref" == -* ]]; then
  echo "ERROR: --ref must not start with '-' (got '$ref')" >&2
  exit 2
fi

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

reconciliation_enabled() {
  [[ "$hooks_mode" != "skip" ]] || return 1
  local enabled
  for enabled in "$do_codex" "$do_claude"; do
    [[ "$enabled" == "1" ]] || return 1
  done
}

# --- 0. optional one-way layout migration ---------------------------------------
#
# Behind a flag and behind a confirmation, because it is not reversible and
# because there is a two-syscall window in which $AQG_ROOT does not exist: a hook
# firing in that gap fails into its own `|| true` and one tool call goes
# unguarded. That is acceptable for something a human asks for and watches; it
# would not be acceptable as a side effect of an ordinary upgrade.
if [[ "$do_migrate" == "1" ]]; then
  step "Migrate to the managed-update layout"
  # The refusal is the important output here, so it is printed as a sentence
  # rather than raised as a traceback: a user deciding whether to move their
  # install should not have to read a stack to find out why they cannot.
  # The path goes through argv, never through string interpolation. Built the
  # other way, an install directory named with an apostrophe broke the inline
  # program outright -- and one named to close the quote and open a statement
  # would have RUN, which makes the checkout's own location an injection point.
  if ! python3 -c '
import sys
root = sys.argv[1]
sys.path.insert(0, root)
from scripts.aqg_update import migrate
try:
    plan = migrate.migrate(root, dry_run=True)
except migrate.MigrateError as exc:
    print("cannot migrate: " + str(exc), file=sys.stderr)
    raise SystemExit(1)
if plan.already:
    print("already on the managed-update layout: " + str(plan.target))
    raise SystemExit(3)
print("would move   " + str(plan.root))
print("          -> " + str(plan.target))
print("and leave a symlink at " + str(plan.root))
' "$repo_root"; then
    rc=$?
    [[ "$rc" == "3" ]] && exit 0
    exit 1
  fi
  printf '\nThis is one-way. Type "migrate" to continue: '
  read -r answer
  if [[ "$answer" != "migrate" ]]; then
    echo "aborted; nothing was moved."
    exit 0
  fi
  python3 -c '
import sys
root = sys.argv[1]
sys.path.insert(0, root)
from scripts.aqg_update import migrate
try:
    done = migrate.migrate(root)
except migrate.MigrateError as exc:
    print("migration failed: " + str(exc), file=sys.stderr)
    raise SystemExit(1)
print("migrated: " + str(done.root) + " -> " + str(done.target))
' "$repo_root" || exit 1
  echo "Re-run scripts/upgrade.sh to continue with the usual steps."
  exit 0
fi

# Hold one OS lock across the transactional root swap and every host write.
# Re-entering the script under Python avoids Bash-4-only coprocess features and
# keeps crash cleanup kernel-backed on macOS, Linux, and Git Bash alike.
if [[ -L "$repo_root" && "$clean_only" != "1" \
      && "${AQG_RECONCILIATION_LOCK_HELD:-0}" != "1" ]] \
      && reconciliation_enabled; then
  exec python3 -c '
import os
import subprocess
import sys
root, script, *args = sys.argv[1:]
sys.path.insert(0, root)
from scripts.aqg_update import lock, run, state
try:
    with lock.install_lock(
        path=state.state_root(create=True) / run.RECONCILIATION_LOCK_FILENAME
    ):
        env = dict(os.environ)
        env["AQG_RECONCILIATION_LOCK_HELD"] = "1"
        done = subprocess.run(["bash", script, *args], env=env)
except lock.LockBusy:
    print("ERROR: another AQG update or host reconciliation is in progress.", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(done.returncode)
' "$repo_root" "$script_dir/upgrade.sh" "${original_args[@]}"
fi

# Read VERSION without leaking a shell redirect error when the file is absent
# (e.g. checking out a very old --ref): guard the redirect with -f.
read_version() {
  if [[ -f "$1" ]]; then
    local v; v="$(tr -d '[:space:]' < "$1" 2>/dev/null)"; printf '%s' "${v:-unknown}"
  else
    printf 'unknown'
  fi
}

update_skipped_dirty="0"   # requested git update was skipped due to tracked changes

# --- 1. Update the checkout ----------------------------------------------------
if [[ "$clean_only" == "1" ]]; then
  step "Git update: skipped (--clean-only)"
elif ! git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # NOT `-d "$repo_root/.git"`. After the first managed update the root points
  # at a git WORKTREE, whose .git is a FILE — that test would have called every
  # updated install "not a git checkout" and skipped updates forever, silently.
  step "Git update: skipped ($repo_root is not a git checkout)"
else
  step "Git update"
  echo "local:  $(read_version "$repo_root/VERSION") @ $(git -C "$repo_root" rev-parse --short HEAD)"
  # Refuse to move HEAD when there are tracked, uncommitted changes (untracked
  # files are fine — a fast-forward never touches them).
  if ! git -C "$repo_root" diff --quiet || ! git -C "$repo_root" diff --cached --quiet; then
    echo "WARN: tracked local changes present — skipping git update to avoid clobbering them." >&2
    echo "      Commit/stash and re-run, or use --clean-only to reinstall without updating." >&2
    update_skipped_dirty="1"
  else
    git -C "$repo_root" fetch --prune origin
    pre_swap_target=""
    [[ -L "$repo_root" ]] && pre_swap_target="$(readlink "$repo_root")"
    target_ref="${ref:-origin/main}"
    target_commit="$(git -C "$repo_root" rev-parse --verify "${target_ref}^{commit}")"

    if [[ -L "$repo_root" ]]; then
      # On the version-tree layout: stage the commit beside the live one and
      # swap the root, so the update either lands or does not and the previous
      # version stays on disk. docs 9 puts the signature requirement on the
      # AUTOMATIC channel; this path is a human typing a command, and what it
      # gains here is the transaction, not verification.
      # Whether this run will actually reconcile the hosts afterwards. Steps 2-4
      # reinstall skills and refresh hooks, which IS the reconciliation a
      # host-touching update needs — but --no-hooks and the per-client skips turn
      # those steps off, and claiming reconciliation without doing it would swap
      # the root and leave the hosts describing the old tree.
      reconcile="0"
      # AND, not OR. `--no-codex` skips the step that reconciles Codex, so a run
      # with it claimed reconciliation while leaving that host describing the old
      # tree — the exact state the claim exists to avoid. A run that will not
      # reconcile EVERY host must not claim to reconcile any.
      if reconciliation_enabled; then
        reconcile="1"
      fi

      if [[ "$reconcile" == "1" ]]; then
        python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from scripts.aqg_update import run
run.prepare_reconciliation_snapshot()
' "$repo_root"
      fi

      echo "applying $target_commit transactionally (reconcile=$reconcile)"
      python3 -c '
import sys
root, commit, reconcile = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
sys.path.insert(0, root)
from scripts.aqg_update import run
result = run.apply_commit(root=root, commit=commit, host_reconciliation=reconcile)
print("update: " + result.outcome + (". " + result.detail if result.detail else ""))
for item in result.pending:
    print(("  reconciled by the next steps: " if reconcile else "  needs a human: ") + item)
# 2 = refused (nothing was changed); 1 = the transaction went wrong.
# 3 = repair-required: the transaction could NOT put the previous version back,
# so the caller must not print a message claiming it did.
raise SystemExit(0 if result.outcome in ("applied", "current")
                 else 2 if result.outcome == "pending"
                 else 3 if result.outcome == "repair-required"
                 else 1)
' "$repo_root" "$target_commit" "$reconcile" || {
        rc=$?
        if [[ "$rc" == "2" ]]; then
          # Refused, nothing changed. Not a reason to abandon the rest of the
          # command: the skills and hook steps below used to run in this case and
          # still should.
          echo "WARN: the tree was not updated (see above); continuing with the remaining steps." >&2
          update_skipped_dirty="1"
        elif [[ "$rc" == "3" ]]; then
          echo "ERROR: the update could not be undone; this install needs attention." >&2
          echo "       See docs/UPDATE_ARCHITECTURE.md 5 and the versions/ directory." >&2
          exit 1
        else
          echo "ERROR: the transactional update did not complete; the previous version is still live." >&2
          exit 1
        fi
      }

      # The swap has happened and the hosts have NOT been reconciled yet. If any
      # later step fails, `set -e` aborts and leaves the root on the new tree
      # while the hosts still describe the old one — which is the state the
      # automatic path refuses outright. Put the root back.
      #
      # Not a perfect state either: some skill routes may already point at the
      # new tree. It is the SAFE direction, because the old tree still contains
      # every hook script the host config references, and the failure being
      # avoided is a guardrail whose script vanished from under the root.
      if [[ "$reconcile" == "1" && -n "$pre_swap_target" ]]; then
        aqg_rollback_root() {
          local rc=$?
          [[ "$rc" == "0" ]] && return 0
          echo "" >&2
          echo "ERROR: a step after the version swap failed (exit $rc)." >&2
          echo "       Putting the root back to $pre_swap_target so the hosts and the" >&2
          echo "       tree they point into stay consistent. Re-run when fixed." >&2
          python3 -c '
import sys
root, commit, previous = sys.argv[1:4]
sys.path.insert(0, root)
from scripts.aqg_update import run
result = run.rollback_reconciliation(
    root=root, expected_commit=commit, previous_root=previous
)
print("rollback: " + result.outcome + (". " + result.detail if result.detail else ""))
raise SystemExit(0 if result.outcome == "rolled-back" else 1)
' "$repo_root" "$target_commit" "$pre_swap_target" || {
            echo "ERROR: could not restore both root and install state." >&2
            return 1
          }
          return "$rc"
        }
        trap aqg_rollback_root EXIT
      fi
    else
      # A plain checkout. Same behaviour as before — and a pointer, because
      # silently keeping the old path is how nobody ever migrates.
      if [[ -n "$ref" ]]; then
        git -C "$repo_root" checkout --quiet "$ref"
        echo "checked out $ref"
      else
        git -C "$repo_root" checkout --quiet main
        git -C "$repo_root" pull --ff-only
      fi
      echo "note: this install updates in place. Run scripts/upgrade.sh --migrate"
      echo "      to move onto the version-tree layout, where an update is a"
      echo "      transaction with the previous version kept on disk."
    fi
    echo "now:    $(read_version "$repo_root/VERSION") @ $(git -C "$repo_root" rev-parse --short HEAD)"
  fi
fi

# --- 2. Codex skills -----------------------------------------------------------
if [[ "$do_codex" == "1" && ( -d "${CODEX_HOME:-$HOME/.codex}" || -n "${CODEX_HOME:-}" ) ]]; then
  step "Codex skills (reinstall + prune)"
  # AQG_SKIP_HOOK_PROMPT: upgrade.sh owns the Codex hook decision in step 4, so
  # the skills installer must not also install them — nor blame --no-hooks for it.
  AQG_SKIP_HOOK_PROMPT=1 bash "$repo_root/scripts/install.sh" --force
else
  step "Codex skills: skipped (no ~/.codex, or --no-codex)"
fi

# --- 3. Claude Code skills -----------------------------------------------------
if [[ "$do_claude" == "1" && -d "$HOME/.claude" ]]; then
  step "Claude Code skills (reinstall + prune)"
  # AQG_SKIP_HOOK_PROMPT: upgrade.sh owns the hook decision (--hooks below), so
  # the skills installer must not also prompt/recommend hooks here.
  AQG_SKIP_HOOK_PROMPT=1 bash "$repo_root/agent-packs/claude-code/install.sh" --scope user --mode link --force
else
  step "Claude Code skills: skipped (no ~/.claude, or --no-claude)"
fi

# --- 4. Codex hooks (default-on; --no-hooks to skip) ----------------------------
codex_hooks_rc=0
codex_hooks_target="${CODEX_HOME:-$HOME/.codex}/hooks.json"
_install_codex_hooks() {
  python3 "$repo_root/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$repo_root" \
    --target "$codex_hooks_target" || codex_hooks_rc=$?
  [[ "$codex_hooks_rc" -eq 0 ]] || echo "WARN: Codex hook install returned $codex_hooks_rc." >&2
}
if [[ "$hooks_mode" == "skip" ]]; then
  step "Codex hooks: skipped (--no-hooks)"
elif [[ "$do_codex" != "1" || ! -d "${CODEX_HOME:-$HOME/.codex}" ]]; then
  step "Codex hooks: skipped (no Codex home, or --no-codex)"
elif [[ "$hooks_mode" == "force" ]]; then
  step "Codex hooks (force install/refresh)"
  _install_codex_hooks
else
  codex_install_status=0
  python3 "$repo_root/scripts/install_aqg_codex_hooks.py" --is-installed \
    --target "$codex_hooks_target" >/dev/null 2>&1 || codex_install_status=$?
  if [[ "$codex_install_status" -eq 0 ]]; then
    step "Codex hooks: refreshing existing managed set"
    _install_codex_hooks
  elif [[ "$codex_install_status" -eq 3 ]]; then
    step "Codex hooks: invalid Codex hooks config — refusing silent preservation"
    echo "  Repair or restore $codex_hooks_target, then re-run with --hooks." >&2
    codex_hooks_rc=3
  else
    step "Codex hooks: installing managed set (default — use --no-hooks to skip)"
    _install_codex_hooks
  fi
fi

# --- 5. Claude Code hooks (default-on; --no-hooks to skip) ----------------------
# Default ("auto"): a machine WITH Claude Code (~/.claude) gets the managed hook set so
# "installed AQG = the enforcement layer is resident". Two carve-outs preserve an
# explicit user choice: a machine already on the managed set is refreshed (picks up
# newly-shipped hooks like the handoff mandate), and an explicit warn-only opt-in
# (run_warn_only.sh present in settings.json) is left byte-for-byte untouched — never
# promoted to the blocking set (audit cf5adc7f f1). --hooks forces; --no-hooks skips.
hooks_rc=0
claude_settings="$HOME/.claude/settings.json"
_install_hooks() {
  AQG_ROOT="$repo_root" python3 "$repo_root/scripts/install_aqg_hooks.py" --apply --aqg-root "$repo_root" \
    || hooks_rc=$?
  [[ "$hooks_rc" -eq 0 ]] || echo "WARN: hook install returned $hooks_rc (see output above)." >&2
}
if [[ "$hooks_mode" == "skip" ]]; then
  step "Claude Code hooks: skipped (--no-hooks)"
elif [[ ! -d "$HOME/.claude" ]]; then
  step "Claude Code hooks: skipped (no ~/.claude — Claude Code not installed here)"
elif [[ "$hooks_mode" == "force" ]]; then
  step "Claude Code hooks (force install/refresh)"
  _install_hooks
elif AQG_ROOT="$repo_root" python3 "$repo_root/scripts/install_aqg_hooks.py" --is-installed >/dev/null 2>&1; then
  step "Claude Code hooks: refreshing managed set (pick up newly-shipped hooks)"
  _install_hooks
# run_warn_only.sh is ONLY in the warn-only example, never the managed set — so a
# managed machine already matched --is-installed above and never reaches this branch
# (invariant pinned by test_default_installs_managed_set_on_fresh_claude; audit 2ab8499e claude f2).
elif [[ -f "$claude_settings" ]] && grep -q 'run_warn_only.sh' "$claude_settings" 2>/dev/null; then
  step "Claude Code hooks: warn-only opt-in detected — leaving untouched"
  echo "  You explicitly chose the never-blocking warn-only set; a default upgrade won't"
  echo "  promote it to the full (blocking) set. Switch anytime:  scripts/upgrade.sh --hooks"
else
  step "Claude Code hooks: installing managed set (default — use --no-hooks to skip)"
  _install_hooks
fi

# Every reconciliation step (2-5) must succeed and its result must be recorded
# before the root may stay where the update put it. The hook installers capture
# their return codes so they can print useful diagnostics; check those codes
# while the rollback trap is still armed.
if [[ "${reconcile:-0}" == "1" ]]; then
  if [[ "$codex_hooks_rc" -ne 0 || "$hooks_rc" -ne 0 ]]; then
    echo "ERROR: host reconciliation failed; restoring the previous AQG root." >&2
    exit 1
  fi

  step "Finalize host reconciliation"
  python3 -c '
import sys
root, commit = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
from scripts.aqg_update import run
result = run.finalize_reconciliation(root=root, expected_commit=commit)
print("reconciliation: " + result.outcome + (". " + result.detail if result.detail else ""))
raise SystemExit(0 if result.outcome == "current" else 1)
' "$repo_root" "$target_commit" || {
    echo "ERROR: host reconciliation could not be finalized; restoring the previous AQG root." >&2
    exit 1
  }

  trap - EXIT
fi

# --- 6. Verify -----------------------------------------------------------------
step "Doctor (verify)"
doctor_rc=0
AQG_ROOT="$repo_root" python3 "$repo_root/scripts/aqg_doctor.py" --no-cli || doctor_rc=$?

# --- Final status --------------------------------------------------------------
step "Done"
fail=0
if [[ "$update_skipped_dirty" == "1" ]]; then
  echo "⚠ NOT upgraded: tracked local changes blocked the git update; reinstalled the CURRENT checkout only." >&2
  echo "  Commit/stash and re-run (or use --clean-only if reinstalling-in-place was intended)." >&2
  fail=1
fi
if [[ "$hooks_rc" -ne 0 ]]; then
  echo "⚠ Hook install failed (exit $hooks_rc) — hooks may be stale." >&2
  fail=1
fi
if [[ "$codex_hooks_rc" -ne 0 ]]; then
  echo "⚠ Codex hook install failed (exit $codex_hooks_rc) — hooks may be stale." >&2
  fail=1
fi
if [[ "$doctor_rc" -ne 0 ]]; then
  echo "⚠ Doctor reported issues (exit $doctor_rc above)." >&2
  fail=1
fi
if [[ "$fail" -eq 0 ]]; then
  echo "✓ AQG upgraded + verified. Restart Codex / Claude Code (or open a new session) to load."
else
  echo "✗ Upgrade incomplete — resolve the warnings above before relying on this checkout." >&2
  exit 1
fi
