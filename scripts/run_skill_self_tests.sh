#!/usr/bin/env bash
# Run every `*/scripts/self_test.py` in the tree STANDALONE and gate on any failure.
#
# Why standalone (not `pytest skills/`): every skill ships a `self_test.py`, so
# pytest's default import mode collides on the duplicate basename
# (`import file mismatch`). Each self_test is designed to run from its own
# scripts/ dir (it does `import aqg_<helper>` relying on sys.path[0]); running it
# standalone is the contract. This script gates that existing coverage in CI —
# previously the self_tests passed locally but nothing ran them in CI.
#
# The two globs below MUST stay identical to the `--ignore-glob` lines in the
# repo-root pytest.ini. That ini hides these files from collection precisely
# because this script owns them; if the patterns drift, a self_test is hidden
# from pytest and run by nothing. This iterated `skills/aqg-*/` only until
# 2026-09-05, which is how templates/example-skill/scripts/self_test.py — a file
# that ships publicly — ended up executed by neither.
#
# Exit: 0 = all self_tests passed; 1 = one or more failed; 2 = none found.
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root" || { echo "ERROR: cannot cd to repo root" >&2; exit 2; }

ran=0
fail=0
failed_skills=()

for st in skills/*/scripts/self_test.py templates/*/scripts/self_test.py; do
  [ -f "$st" ] || continue
  ran=$((ran + 1))
  dir="$(dirname "$st")"
  if out="$(cd "$dir" && python3 self_test.py 2>&1)"; then
    echo "  PASS  $st"
  else
    echo "  FAIL  $st"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=$((fail + 1))
    failed_skills+=("$st")
  fi
done

echo "----------------------------------------"
if [ "$ran" -eq 0 ]; then
  echo "ERROR: no self_tests found under skills/*/ or templates/*/scripts/self_test.py" >&2
  exit 2
fi
echo "skill self-tests: ${ran} run, ${fail} failed"
if [ "$fail" -ne 0 ]; then
  printf 'failed: %s\n' "${failed_skills[*]}" >&2
  exit 1
fi
echo "OK: all ${ran} skill self_tests passed"
exit 0
