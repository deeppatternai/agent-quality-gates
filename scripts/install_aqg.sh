#!/usr/bin/env bash
# AQG one-shot installer.
#
# Orchestrates the existing per-client installers, writes AQG_ROOT into your
# shell config (the #1 "installed but inert" trap — see AI_SETUP.md), and runs
# the doctor. It does NOT rewrite any install logic; it chains the scripts that
# already own it:
#   - scripts/install.sh                      Codex skills  -> ~/.codex/skills
#   - agent-packs/claude-code/install.sh      Claude skills -> ~/.claude/skills
#   - scripts/install_aqg_hooks.py --apply    Claude Code enforcement hooks
#   - scripts/install_aqg_codex_hooks.py --apply  Codex lifecycle hook pack
#   - scripts/aqg_doctor.py                   health check
#
# Idempotent + re-runnable (each sub-installer is --force / dedup; the AQG_ROOT
# block is marker-delimited so a re-run replaces, never stacks). `--uninstall`
# reverses it.
#
# Usage:
#   scripts/install_aqg.sh [options]
#     --clients LIST       comma list of claude,codex (default: auto-detect ~/.claude, ~/.codex)
#     --no-hooks           skip every client hook layer
#     --warn-only-hooks    use Claude's never-blocking hooks and skip Codex (no verified warn-only Codex pack)
#     --uninstall          remove AQG skills + hooks + the AQG_ROOT shell block
#     --dry-run            print every action, change nothing
#     --yes                do not prompt before editing the shell config
#     -h, --help           this help
#
# Env overrides: AQG_SHELL_RC (force a specific rc file), CODEX_HOME, HOME.
set -euo pipefail

AQG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

CLIENTS=""
DO_HOOKS=1
WARN_ONLY_HOOKS=0
UNINSTALL=0
DRY_RUN=0
ASSUME_YES=0

# Marker so a re-run replaces (never stacks) and --uninstall can excise cleanly.
BLOCK_BEGIN="# >>> agent-quality-gates (AQG_ROOT) >>>"
BLOCK_END="# <<< agent-quality-gates (AQG_ROOT) <<<"

usage() { sed -n '/^# Usage:/,/^# Env overrides:/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

_err()  { printf 'ERROR: %s\n' "$*" >&2; }
_info() { printf '%s\n' "$*"; }
_step() { printf '\n== %s ==\n' "$*"; }

# Echo a command; run it only when not a dry run.
_run() {
  printf '  $ %s\n' "$*"
  [ "$DRY_RUN" = "1" ] && return 0
  "$@"
}

_confirm() {
  # $1 = prompt. Yes when --yes, or on an interactive "y". Non-tty non-yes = no.
  [ "$ASSUME_YES" = "1" ] && return 0
  [ -t 0 ] || { _err "not a tty and --yes not given; refusing to edit $1"; return 1; }
  printf '  edit %s to export AQG_ROOT? [y/N] ' "$1"
  local reply; read -r reply
  case "$reply" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

# Resolve the shell rc file to edit: AQG_SHELL_RC override wins, else by $SHELL.
# bash on macOS (darwin) login shells read ~/.bash_profile, NOT ~/.bashrc — target
# that so the export actually loads (audit 04545796 f3).
_shell_rc() {
  if [ -n "${AQG_SHELL_RC:-}" ]; then printf '%s\n' "$AQG_SHELL_RC"; return 0; fi
  case "$(basename "${SHELL:-/bin/bash}")" in
    zsh)  printf '%s\n' "$HOME/.zshrc" ;;
    fish) printf '%s\n' "$HOME/.config/fish/config.fish" ;;
    *)    if [ "$(uname -s)" = "Darwin" ] || [ -f "$HOME/.bash_profile" ]; then
            printf '%s\n' "$HOME/.bash_profile"
          else
            printf '%s\n' "$HOME/.bashrc"
          fi ;;
  esac
}

# The export line for the rc's shell family. fish gets fish-safe single-quoting
# (bash %q is NOT valid fish for metacharacter paths — audit 04545796 f5).
_export_line() {
  case "$1" in
    */config.fish)
      local q="${AQG_ROOT//\\/\\\\}"; q="${q//\'/\\\'}"
      printf "set -gx AQG_ROOT '%s'\n" "$q" ;;
    *) printf 'export AQG_ROOT=%q\n' "$AQG_ROOT" ;;
  esac
}

# Print $1 with the AQG block removed. Exit non-zero (emitting NOTHING) on an
# unbalanced / duplicated marker so the caller aborts instead of truncating the
# rc past a stray BEGIN (audit 04545796 f1). Buffers, so no partial output leaks.
_strip_block() {
  awk -v b="$BLOCK_BEGIN" -v e="$BLOCK_END" '
    $0==b { if (inblock) exit 3; inblock=1; next }
    $0==e { if (!inblock) exit 3; inblock=0; next }
    { if (!inblock) buf = buf $0 ORS }
    END { if (inblock) exit 3; printf "%s", buf }
  ' "$1"
}

# 0 iff the AQG block in $1 contains the exact line $2 (block-scoped, NOT a
# whole-file grep — a stale block + the path on an unrelated line must not read
# as "unchanged", audit 04545796 f2).
_block_has_line() {
  awk -v b="$BLOCK_BEGIN" -v e="$BLOCK_END" -v want="$2" '
    $0==b {inblock=1; next} $0==e {inblock=0; next}
    inblock && $0==want {found=1}
    END {exit(found?0:1)}
  ' "$1"
}

_write_aqg_root() {
  local rc want; rc="$(_shell_rc)"; want="$(_export_line "$rc")"
  if [ -f "$rc" ] && _block_has_line "$rc" "$want"; then
    _info "  AQG_ROOT already exported in $rc (unchanged)"
    return 0
  fi
  if [ "$DRY_RUN" = "1" ]; then
    _info "  would write to $rc:"; _info "    $BLOCK_BEGIN"
    _info "    $want"; _info "    $BLOCK_END"; return 0
  fi
  _confirm "$rc" || { _err "skipped shell config; add yourself: $want"; return 1; }
  local rc_dir; rc_dir="$(dirname "$rc")"
  mkdir -p "$rc_dir"
  local tmp; tmp="$(mktemp "$rc_dir/.aqg-rc.XXXXXX")"
  if [ -f "$rc" ]; then
    if ! _strip_block "$rc" > "$tmp"; then
      rm -f "$tmp"
      _err "malformed AQG block in $rc (unbalanced markers) — fix it by hand; leaving the file untouched"
      return 1
    fi
    cp -p "$rc" "$rc.aqg-bak.$(date +%Y%m%d-%H%M%S)"
  fi
  { printf '%s\n' "$BLOCK_BEGIN"; printf '%s\n' "$want"; printf '%s\n' "$BLOCK_END"; } >> "$tmp"
  mv "$tmp" "$rc"
  _info "  wrote AQG_ROOT to $rc (backup alongside). Restart your shell / agent to load it."
}

_remove_aqg_root() {
  local rc; rc="$(_shell_rc)"
  [ -f "$rc" ] || { _info "  no $rc; nothing to remove"; return 0; }
  grep -qF "$BLOCK_BEGIN" "$rc" || { _info "  no AQG block in $rc"; return 0; }
  if [ "$DRY_RUN" = "1" ]; then _info "  would remove the AQG_ROOT block from $rc"; return 0; fi
  local tmp; tmp="$(mktemp "$(dirname "$rc")/.aqg-rc.XXXXXX")"
  if ! _strip_block "$rc" > "$tmp"; then
    rm -f "$tmp"
    _err "malformed AQG block in $rc (unbalanced markers) — fix it by hand; leaving the file untouched"
    return 1
  fi
  cp -p "$rc" "$rc.aqg-bak.$(date +%Y%m%d-%H%M%S)"
  mv "$tmp" "$rc"
  _info "  removed the AQG_ROOT block from $rc"
}

_detect_clients() {
  local out=""
  [ -d "$HOME/.claude" ] && out="claude"
  [ -d "${CODEX_HOME:-$HOME/.codex}" ] && out="${out:+$out,}codex"
  printf '%s\n' "$out"
}

_has_client() { case ",$CLIENTS," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

install_flow() {
  _step "clients: ${CLIENTS:-none detected}"
  [ -z "$CLIENTS" ] && { _err "no client selected (pass --clients claude,codex)"; return 1; }

  if _has_client codex; then
    _step "Codex skills -> ${CODEX_HOME:-$HOME/.codex}/skills"
    # This orchestrator owns the Codex hook choice below. install.sh now applies
    # the hook pack by default, so suppress it here — otherwise --no-hooks and
    # --warn-only-hooks would still land the blocking policies. Same mechanism as
    # the Claude branch below, so neither run reports a flag the user never passed.
    _run env AQG_SKIP_HOOK_PROMPT=1 "$AQG_ROOT/scripts/install.sh" --force
    if [ "$DO_HOOKS" = "1" ]; then
      if [ "$WARN_ONLY_HOOKS" = "1" ]; then
        _step "Codex hooks: skipped (--warn-only-hooks has no verified Codex variant)"
      else
        _step "Codex hooks (review and trust in /hooks after restart)"
        _run python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$AQG_ROOT"
      fi
    fi
  fi
  if _has_client claude; then
    _step "Claude Code skills -> $HOME/.claude/skills"
    # This orchestrator owns the hook choice below. Suppress the skill
    # installer's separate recommendation/prompt so --no-hooks is truthful.
    _run env AQG_SKIP_HOOK_PROMPT=1 "$AQG_ROOT/agent-packs/claude-code/install.sh" --scope user --mode link --force
    if [ "$DO_HOOKS" = "1" ]; then
      if [ "$WARN_ONLY_HOOKS" = "1" ]; then
        _step "Claude Code hooks (warn-only variant)"
        _install_warn_only_hooks
      else
        _step "Claude Code hooks (default gates; each has an AQG_AGENT=human-opt-in escape)"
        _run python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --apply --aqg-root "$AQG_ROOT"
      fi
    fi
  fi

  _step "AQG_ROOT -> shell config"
  _write_aqg_root

  _step "doctor"
  _run env AQG_ROOT="$AQG_ROOT" python3 "$AQG_ROOT/scripts/aqg_doctor.py"
}

_install_warn_only_hooks() {
  # install_aqg_hooks.py only ships the canonical (gated) set; the never-blocking
  # config is a separate example. Install it as settings.json only when there is
  # none to clobber — otherwise guide the user (merging their settings is theirs).
  local ex="$AQG_ROOT/agent-packs/claude-code/hooks/settings.warn-only.example.json"
  local dst="$HOME/.claude/settings.json"
  if [ -e "$dst" ]; then
    _info "  $dst exists — not overwriting. To go warn-only, merge $ex into it by hand."
    return 0
  fi
  _run mkdir -p "$HOME/.claude"
  _run cp "$ex" "$dst"
}

uninstall_flow() {
  # No detected client is not a reason to skip cleanup — try both; _rm_skill_links
  # no-ops on absent dirs (audit 04545796 f7).
  [ -z "$CLIENTS" ] && CLIENTS="claude,codex"
  _step "clients: $CLIENTS"
  if _has_client claude; then
    _step "remove Claude Code hooks"
    _run python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --uninstall --aqg-root "$AQG_ROOT" || true
    _step "remove Claude skill symlinks (~/.claude/skills/aqg-*)"
    _rm_skill_links "$HOME/.claude/skills"
  fi
  if _has_client codex; then
    _step "remove AQG Codex hooks"
    _run python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --uninstall || true
    _step "remove Codex skill symlinks (~/.codex/skills/aqg-*)"
    _rm_skill_links "${CODEX_HOME:-$HOME/.codex}/skills"
  fi
  _step "remove AQG_ROOT shell block"
  _remove_aqg_root
  _info "\nUninstalled. (Rules text in CLAUDE.md/AGENTS.md is left untouched — remove the AQG section by hand if wanted.)"
}

_rm_skill_links() {
  local dir="$1"
  [ -d "$dir" ] || { _info "  no $dir"; return 0; }
  local n; n="$(find "$dir" -maxdepth 1 -name 'aqg-*' 2>/dev/null | wc -l | tr -d ' ')"
  [ "$n" = "0" ] && { _info "  no aqg-* under $dir"; return 0; }
  _run find "$dir" -maxdepth 1 -name 'aqg-*' -exec rm -rf {} +
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --clients)          [ $# -ge 2 ] || { _err "--clients requires a value"; return 2; }
                          CLIENTS="$2"; shift 2 ;;
      --clients=*)        CLIENTS="${1#*=}"; shift ;;
      --no-hooks)         DO_HOOKS=0; shift ;;
      --warn-only-hooks)  WARN_ONLY_HOOKS=1; shift ;;
      --uninstall)        UNINSTALL=1; shift ;;
      --dry-run)          DRY_RUN=1; shift ;;
      --yes|-y)           ASSUME_YES=1; shift ;;
      -h|--help)          usage; return 0 ;;
      *) _err "unknown arg: $1"; usage; return 2 ;;
    esac
  done

  [ -f "$AQG_ROOT/VERSION" ] || { _err "not an AQG checkout: $AQG_ROOT"; return 1; }
  [ -z "$CLIENTS" ] && [ "$UNINSTALL" != "1" ] && CLIENTS="$(_detect_clients)"

  printf 'AQG installer  (AQG_ROOT=%s%s)\n' "$AQG_ROOT" "$([ "$DRY_RUN" = 1 ] && echo ', dry-run')"
  if [ "$UNINSTALL" = "1" ]; then uninstall_flow; else install_flow; fi
}

main "$@"
