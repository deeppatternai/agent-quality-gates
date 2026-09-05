#!/usr/bin/env bash
# AQG context helper: resolve $aqg_root for sourced SKILL.md How To Run blocks.
#
# Contract (hardened by audit 58bb9dc5 #1-#4):
#
# - This file MUST be SOURCED, not executed. It uses `return`, never `exit`,
#   so failures do not kill the caller shell.
# - It does NOT enable `set -euo pipefail` globally — the caller's shell
#   options stay intact.
# - It exports `aqg_root` (so child processes inherit the resolved path).
#   All scratch variables are prefixed `_aqgctx_` and unset on every exit
#   path including failure (no function leak — audit #1).
# - It preserves the caller's positional parameters; nothing inside the
#   function uses `$1` etc.
# - It always re-resolves on source — does NOT skip based on a cached
#   `aqg_root`, so `AQG_REQUIRE_ENV=1` cannot be bypassed by prior state
#   (audit #3).
# - It tolerates whitespace in checkout paths via `IFS= read -r` for the
#   sentinel file (audit #4).
#
# API:
#   AQG_REQUIRE_ENV=1 source _aqg_context.sh   # source skill: AQG_ROOT env required
#   source _aqg_context.sh                     # Claude wrapper: env > sentinel > fallback
#
# After successful return, `$aqg_root` holds the resolved checkout path AND
# is exported to child processes. Behavior table: see sketch a2 §4.
#
# `$aqg_root` is the PHYSICAL path — symlinks resolved, absolute, no trailing
# slash — whichever row resolved it (docs/UPDATE_ARCHITECTURE.md §5.2). The
# managed update swaps the root symlink to a new version tree while sessions are
# running, so code that resolved the root and then invoked another file under it
# afterwards would span two generations: one logical operation reading half of
# each. Pinning the physical path keeps it on the tree it started on.
#
# SCOPE — three limits, each narrower than the sentence above suggests:
#
# 1. This pins everything downstream of ONE sourcing. The contract above
#    requires re-resolving on every source, so a run that sources this helper
#    twice across a swap gets two different roots. That is deliberate: caching
#    would let a stale value bypass AQG_REQUIRE_ENV=1.
# 2. Hooks are not pinned at all. They reference $AQG_ROOT directly in their
#    settings.json command strings and never source this helper, so each
#    invocation resolves the link on its own. A hook is usually one short-lived
#    file execution — but a hook script that itself invokes other files under
#    the root can still tear across a swap. That residual is real, not argued
#    away.
# 3. The pinned tree survives only as long as retention keeps it: the managed
#    update retains the current and previous version trees, so something still
#    running across two further updates can lose the tree it pinned.

# Guard against direct execution — sourced files have $0 == bash / -bash etc.
case "${BASH_SOURCE[0]}" in
  "$0")
    echo "ERROR: _aqg_context.sh must be sourced, not executed." >&2
    echo "  Use: source $(basename "${BASH_SOURCE[0]}")" >&2
    exit 2
    ;;
esac

# Physical path of $1, or empty if it is not a reachable directory. `cd`+`pwd -P`
# rather than `realpath`, which is absent on some POSIX hosts and is not in this
# file's existing toolset; rows 3/4 already used this idiom.
#
# `CDPATH=` because `cd` consults it for a RELATIVE operand: with CDPATH set, a
# relative root resolves against a CDPATH entry instead of the cwd — silently
# pinning the wrong directory — and `cd` then prints its choice on stdout, which
# the command substitution would swallow into the result. Reproduced before
# fixing. `--` because a directory whose name starts with `-` is otherwise read
# as options, turning a working install into a hard failure.
_aqgctx_physical() {
  ( CDPATH= cd -P -- "$1" 2>/dev/null && pwd -P ) || true
}

# A path whose final component ends in a newline cannot survive `$( )`, which
# strips trailing newlines: it would resolve to a silently TRUNCATED path that
# passes every non-empty check downstream. Such a name cannot occur in a real
# checkout, so it is refused rather than supported — a loud failure instead of a
# quiet wrong answer, and the file's whitespace-tolerance claim stays true for
# every case it does cover.
# NOTE: written with $'\n' rather than $(printf '\n'). The latter evaluates to
# the EMPTY string, because command substitution strips trailing newlines — the
# very behaviour this guard exists to catch — which would turn the pattern into
# `**` and reject every path. The file already uses $'...' for the CR strip.
_aqgctx_untruncated() {
  case "$1" in
    *$'\n'*) return 1 ;;
    *) return 0 ;;
  esac
}

_aqgctx_resolve() {
  local _aqgctx_candidate=""
  local _aqgctx_resolved=""
  local _aqgctx_sentinel=""

  # Row 1: AQG_ROOT explicitly set
  if [ -n "${AQG_ROOT:-}" ]; then
    if [ -f "$AQG_ROOT/VERSION" ]; then
      _aqgctx_resolved="$(_aqgctx_physical "$AQG_ROOT")"
      if [ -n "$_aqgctx_resolved" ] && _aqgctx_untruncated "$AQG_ROOT"; then
        aqg_root="$_aqgctx_resolved"
        return 0
      fi
    fi
    echo "ERROR: AQG_ROOT points to invalid path: $AQG_ROOT" >&2
    echo "  Expected sentinel: \$AQG_ROOT/VERSION" >&2
    echo "  Fix: export AQG_ROOT=/path/to/agent-quality-gates checkout." >&2
    return 1
  fi

  # Source skill (AQG_REQUIRE_ENV=1) hard-fails when env is not set.
  if [ "${AQG_REQUIRE_ENV:-}" = "1" ]; then
    echo "ERROR: source skill requires AQG_ROOT env var." >&2
    echo "  Fix: export AQG_ROOT=/path/to/agent-quality-gates checkout." >&2
    return 1
  fi

  # Wrapper variant — try CLAUDE_SKILL_DIR fallbacks.

  # Row 3: CLAUDE_SKILL_DIR/.aqg-root sentinel
  if [ -n "${CLAUDE_SKILL_DIR:-}" ] && [ -d "$CLAUDE_SKILL_DIR" ]; then
    _aqgctx_resolved="$(cd "$CLAUDE_SKILL_DIR" && pwd -P)"
    _aqgctx_sentinel="$_aqgctx_resolved/.aqg-root"
    if [ -f "$_aqgctx_sentinel" ]; then
      # Read first line; preserve embedded whitespace (audit #4).
      # IFS= prevents word splitting; -r keeps backslashes literal.
      # Note: `read` returns rc!=0 when the file has no trailing newline,
      # but it still assigns the partial line — so we ignore the rc and
      # check whether _aqgctx_candidate is non-empty afterwards.
      _aqgctx_candidate=""
      IFS= read -r _aqgctx_candidate < "$_aqgctx_sentinel" || true
      # Strip trailing CR (CRLF sentinel files) only.
      _aqgctx_candidate="${_aqgctx_candidate%$'\r'}"
      if [ -n "$_aqgctx_candidate" ] && [ -f "$_aqgctx_candidate/VERSION" ]; then
        _aqgctx_candidate="$(_aqgctx_physical "$_aqgctx_candidate")"
        if [ -n "$_aqgctx_candidate" ]; then
          aqg_root="$_aqgctx_candidate"
          return 0
        fi
      fi
    fi

    # Row 4: walk up 4 levels from CLAUDE_SKILL_DIR
    _aqgctx_candidate="$(_aqgctx_physical "$_aqgctx_resolved/../../../..")"
    if [ -n "$_aqgctx_candidate" ] && [ -f "$_aqgctx_candidate/VERSION" ]; then
      aqg_root="$_aqgctx_candidate"
      return 0
    fi
  fi

  # Row 5: default install path. New unified location first
  # ($HOME/.deeppattern/agent-quality-gates), then the legacy path for
  # backward compatibility with installs made before the .deeppattern move.
  for _aqgctx_candidate in \
    "$HOME/.deeppattern/agent-quality-gates" \
    "$HOME/.local/share/aqg/agent-quality-gates"; do
    if [ -f "$_aqgctx_candidate/VERSION" ]; then
      _aqgctx_candidate="$(_aqgctx_physical "$_aqgctx_candidate")"
      if [ -n "$_aqgctx_candidate" ]; then
        aqg_root="$_aqgctx_candidate"
        return 0
      fi
    fi
  done

  # Row 6: nothing set, nothing exists
  echo "ERROR: cannot resolve AQG root." >&2
  echo "  Tried: AQG_ROOT env, CLAUDE_SKILL_DIR/.aqg-root sentinel," >&2
  echo "         CLAUDE_SKILL_DIR walk-up, \$HOME/.deeppattern/agent-quality-gates" >&2
  echo "  Fix: export AQG_ROOT=/path/to/agent-quality-gates checkout." >&2
  return 1
}

# Always re-resolve (audit #3): never trust a cached aqg_root, since the
# caller's prior state could otherwise bypass AQG_REQUIRE_ENV=1 or carry
# a stale path. Cleanup the function on every exit path including failure
# (audit #1) so sourcing never leaves _aqgctx_resolve in caller's shell.
if _aqgctx_resolve; then
  export aqg_root  # audit #2: child processes must inherit resolved path
  unset -f _aqgctx_resolve _aqgctx_physical _aqgctx_untruncated 2>/dev/null || true
else
  unset -f _aqgctx_resolve _aqgctx_physical _aqgctx_untruncated 2>/dev/null || true
  return 1
fi
