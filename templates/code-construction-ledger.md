---
task_slug: CHANGEME-task-slug
path: full
created_at: CHANGEME-iso-8601-utc-now
session_agent: CHANGEME-agent-name
skipped_checks: []
objections_diff_coverage_exception: null
behavior_contract_exception: null
---

# AQG Code Construction Ledger — CHANGEME-task-slug

> Per `aqg-code-construction` skill: fill this ledger as you implement.
> Hard-blocks if structure incomplete; semantic accuracy is your job.
> Header `created_at` MUST be set BEFORE first edit (checker validates).
> Default `path: full` is conservative; change it before first edit when the
> task is truly `mini` or must be `plan`.
> Keep evidence in one working language unless quoting code, commands, output,
> or existing identifiers.

| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | <what neighbor patterns/contracts/helpers/error styles you read> | <e.g., scripts/foo.py:23-45> | <e.g., rg "..." matched N occurrences> |
| 2. Behavior Lock | yes (full/plan) | <focused test written FIRST, existing regression net for refactor, or N/A for docs-only with reason> | <e.g., tests/test_foo.py:12> | <e.g., RED pytest -k test_X failed, GREEN pytest -k test_X PASS> |
| 3. Thin Slice | yes (full/plan) | <one behavior face description> | <e.g., scripts/bar.py:55-78> | <description of what NOT done> |
| 4. Construction Rules | yes (full/plan) | <which: simple / fail-closed / no-spec / reuse-existing-API> | <e.g., scripts/bar.py:80> | <description of choice> |
| 5. Local Verification | yes | <focused test + lint/type/compile/diff> | <e.g., tests/test_foo.py + ruff scripts/> | <e.g., pytest PASS 12/12 + ruff clean> |
| 6. Self Review (5 axes) | yes | <correctness + readability + architecture + security + performance> | <files reviewed> | <e.g., manual review OK> |

## Behavior Contract

(Expected at full/plan tier when production code changes — **warn-only in current 0.14.x**
(BC advisories on stderr, never blocks), hard-required in a later minor. mini tier +
non-prod changes may omit. Each requirement: a normative statement line using
MUST / MUST NOT / SHALL / SHALL NOT, then ≥1 scenario with GIVEN / WHEN / THEN.
Row-2 Behavior Lock evidence must cite every requirement id here via a `covers: R1, R2`
token. To opt out with a concrete reason, set the header field
`behavior_contract_exception` (a vague or `null` reason does NOT opt out).
THEN must be observable and falsifiable; do not encode implementation detail in MUST.)

Common exceptions: for pure docs/spec/config edits, write row 2 as
`N/A — no executable behavior` and name the validation in row 5. For pure
refactors, cite the existing regression net that stayed green. If this section
does not apply, set `behavior_contract_exception` to a concrete reason; do not
use vague values such as "not needed" or "later".

### R1: <short requirement title>
The system MUST <observable, testable behavior>.

- Scenario S1.1: <name>
  - GIVEN <precondition / state>
  - WHEN <action / input>
  - THEN <observable outcome>
  - AND <optional additional outcome>

## Warning Acknowledgements

(Required iff anti-pattern warns triggered: new TODO/FIXME (W5), new dep entry (W6), large diff without plan (W7), docs/status changed (W8). Each row: condition | ack=yes | concrete reason. Vague reason is rejected.)

| condition | ack | reason |
|---|---|---|

## Predicted Objections

(Required count by path: mini=1, full=3, plan=5. Each must include `file:line` of an EXISTING file (checker validates) + concrete mitigation (command/test name/check name; NOT vague "add tests"/"do review"/"watch boundary"). At least one objection MUST reference a changed file in the diff (else set `objections_diff_coverage_exception` in header.)

1. **Objection**: <specific risk> (<file:line>) — **Mitigation**: <concrete command/test name/check name/code handling>
2. **Objection**: <...> — **Mitigation**: <...>
3. **Objection**: <...> — **Mitigation**: <...>
