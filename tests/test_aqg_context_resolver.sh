#!/usr/bin/env bash
# Bash-side tests for scripts/_aqg_context.sh.
#
# Verifies the source-safe contract from the sketch a2 §3.2:
# - sourcing twice returns same aqg_root
# - failure paths use return 1, not exit 1 (caller shell survives)
# - source skill (AQG_REQUIRE_ENV=1) hard-fails when env unset
# - wrapper variant follows env > sentinel > walk-up > default install
# - direct execution is refused
# - no scratch vars leak into caller
#
# Each test isolates env via subshell so cross-contamination is impossible.
# Exit 0 on all-pass, 1 on first failure.

set -uo pipefail  # NOT -e — we want to count failures, not abort

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
HELPER="$REPO_ROOT/scripts/_aqg_context.sh"
PASS=0
FAIL=0
FAIL_NAMES=()

_assert_eq() {
  local _name="$1" _expected="$2" _actual="$3"
  if [ "$_expected" = "$_actual" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    FAIL_NAMES+=("$_name (expected '$_expected', got '$_actual')")
  fi
}

# 1. Source skill: AQG_ROOT set to valid checkout → success
test_source_env_valid() {
  local got
  got=$(env -i HOME="$HOME" PATH="$PATH" AQG_REQUIRE_ENV=1 AQG_ROOT="$REPO_ROOT" \
    bash -c "source '$HELPER' && echo \$aqg_root" 2>/dev/null)
  # Physical path since the generation-pinning change (see the helper header).
  _assert_eq "test_source_env_valid" "$(cd "$REPO_ROOT" && pwd -P)" "$got"
}

# 2. Source skill: AQG_ROOT unset → return 1, NOT exit 1 (caller survives)
test_source_env_missing_returns_not_exits() {
  local got
  got=$(env -i HOME="$HOME" PATH="$PATH" AQG_REQUIRE_ENV=1 \
    bash -c "source '$HELPER' 2>/dev/null; echo caller-survived-rc=\$?" 2>/dev/null)
  case "$got" in
    "caller-survived-rc=1")
      PASS=$((PASS + 1))
      ;;
    *)
      FAIL=$((FAIL + 1))
      FAIL_NAMES+=("test_source_env_missing_returns_not_exits (got '$got')")
      ;;
  esac
}

# 3. Source skill: AQG_ROOT points to invalid path → return 1
test_source_env_invalid_path() {
  local got
  got=$(env -i HOME="$HOME" PATH="$PATH" AQG_REQUIRE_ENV=1 AQG_ROOT="/nonexistent/aqg/path" \
    bash -c "source '$HELPER' 2>/dev/null; echo caller-survived-rc=\$?" 2>/dev/null)
  case "$got" in
    "caller-survived-rc=1")
      PASS=$((PASS + 1))
      ;;
    *)
      FAIL=$((FAIL + 1))
      FAIL_NAMES+=("test_source_env_invalid_path (got '$got')")
      ;;
  esac
}

# 4. Wrapper: AQG_ROOT set → use it (PASS)
test_wrapper_env_valid() {
  local got
  got=$(env -i HOME="$HOME" PATH="$PATH" AQG_ROOT="$REPO_ROOT" \
    bash -c "source '$HELPER' && echo \$aqg_root" 2>/dev/null)
  _assert_eq "test_wrapper_env_valid" "$(cd "$REPO_ROOT" && pwd -P)" "$got"
}

# 5. Wrapper: env unset, default install exists → use default install
test_wrapper_default_install_fallback() {
  local fake_home
  fake_home=$(mktemp -d)
  mkdir -p "$fake_home/.local/share/aqg/agent-quality-gates"
  echo "0.0.0-test" > "$fake_home/.local/share/aqg/agent-quality-gates/VERSION"

  local got
  got=$(env -i HOME="$fake_home" PATH="$PATH" \
    bash -c "source '$HELPER' && echo \$aqg_root" 2>/dev/null)
  # Expected value computed INDEPENDENTLY of the `cd`+`pwd -P` idiom under test.
  # The other cases here derive their expectation the same way the helper does,
  # which would pass through any bug in that idiom (a CDPATH or trailing-newline
  # defect included) -- a tautology wearing a test's clothes.
  _assert_eq "test_wrapper_default_install_fallback" \
    "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' \
       "$fake_home/.local/share/aqg/agent-quality-gates")" "$got"

  rm -rf "$fake_home"
}

# 5b. Wrapper: env unset, new .deeppattern install exists → use it
test_wrapper_deeppattern_fallback() {
  local fake_home
  fake_home=$(mktemp -d)
  mkdir -p "$fake_home/.deeppattern/agent-quality-gates"
  echo "0.0.0-test" > "$fake_home/.deeppattern/agent-quality-gates/VERSION"

  local got
  got=$(env -i HOME="$fake_home" PATH="$PATH" \
    bash -c "source '$HELPER' && echo \$aqg_root" 2>/dev/null)
  _assert_eq "test_wrapper_deeppattern_fallback" \
    "$(cd "$fake_home/.deeppattern/agent-quality-gates" && pwd -P)" "$got"

  rm -rf "$fake_home"
}

# 5c. Wrapper: both .deeppattern and legacy exist → .deeppattern wins (precedence)
test_wrapper_deeppattern_precedence() {
  local fake_home
  fake_home=$(mktemp -d)
  mkdir -p "$fake_home/.deeppattern/agent-quality-gates"
  echo "0.0.0-test" > "$fake_home/.deeppattern/agent-quality-gates/VERSION"
  mkdir -p "$fake_home/.local/share/aqg/agent-quality-gates"
  echo "0.0.0-legacy" > "$fake_home/.local/share/aqg/agent-quality-gates/VERSION"

  local got
  got=$(env -i HOME="$fake_home" PATH="$PATH" \
    bash -c "source '$HELPER' && echo \$aqg_root" 2>/dev/null)
  _assert_eq "test_wrapper_deeppattern_precedence" \
    "$(cd "$fake_home/.deeppattern/agent-quality-gates" && pwd -P)" "$got"

  rm -rf "$fake_home"
}

# 6. Wrapper: nothing set, nothing exists → return 1
test_wrapper_no_resolution() {
  local fake_home
  fake_home=$(mktemp -d)
  local got
  got=$(env -i HOME="$fake_home" PATH="$PATH" \
    bash -c "source '$HELPER' 2>/dev/null; echo rc=\$?" 2>/dev/null)
  _assert_eq "test_wrapper_no_resolution" "rc=1" "$got"
  rm -rf "$fake_home"
}

# 7. Wrapper: CLAUDE_SKILL_DIR/.aqg-root sentinel → use sentinel content
test_wrapper_sentinel_resolution() {
  local fake_home
  fake_home=$(mktemp -d)
  mkdir -p "$fake_home/skills/foo"
  printf '%s' "$REPO_ROOT" > "$fake_home/skills/foo/.aqg-root"

  local got
  got=$(env -i HOME="$fake_home" PATH="$PATH" CLAUDE_SKILL_DIR="$fake_home/skills/foo" \
    bash -c "source '$HELPER' && echo \$aqg_root" 2>/dev/null)
  _assert_eq "test_wrapper_sentinel_resolution" "$REPO_ROOT" "$got"
  rm -rf "$fake_home"
}

# 8. Direct execution refused (script must be sourced)
test_direct_execution_refused() {
  local rc
  bash "$HELPER" >/dev/null 2>&1
  rc=$?
  _assert_eq "test_direct_execution_refused" "2" "$rc"
}

# 9. No scratch vars leak — caller's $1 / $2 preserved, _aqgctx_* unset
test_no_scratch_leak() {
  local got
  got=$(env -i HOME="$HOME" PATH="$PATH" AQG_ROOT="$REPO_ROOT" \
    bash -c 'set -- arg1 arg2; source '"$HELPER"' && echo "args=$1,$2 leaked=${_aqgctx_candidate:-none}"' 2>/dev/null)
  _assert_eq "test_no_scratch_leak" "args=arg1,arg2 leaked=none" "$got"
}

# 10. Idempotent — sourcing twice returns same aqg_root, no error
test_idempotent_sourcing() {
  local got
  got=$(env -i HOME="$HOME" PATH="$PATH" AQG_ROOT="$REPO_ROOT" \
    bash -c "source '$HELPER' && first=\$aqg_root && source '$HELPER' && echo \"first=\$first second=\$aqg_root\"" 2>/dev/null)
  _assert_eq "test_idempotent_sourcing" "first=$REPO_ROOT second=$REPO_ROOT" "$got"
}

# Run all
test_source_env_valid
test_source_env_missing_returns_not_exits
test_source_env_invalid_path
test_wrapper_env_valid
test_wrapper_default_install_fallback
test_wrapper_deeppattern_fallback
test_wrapper_deeppattern_precedence
test_wrapper_no_resolution
test_wrapper_sentinel_resolution
test_direct_execution_refused
test_no_scratch_leak
test_idempotent_sourcing

if [ "$FAIL" -eq 0 ]; then
  echo "_aqg_context.sh bash self-test: $PASS/$PASS PASS"
  exit 0
fi

echo "_aqg_context.sh bash self-test: $PASS PASS, $FAIL FAIL"
for _n in "${FAIL_NAMES[@]}"; do
  echo "  - $_n"
done
exit 1
