---
name: aqg-test-quality-review
description: Use when reviewing the test-QUALITY of a PR/git diff for BEHAVIOR vs SHAPE assertions, coverage gaps where source changed without paired tests, weakened/deleted/skipped tests, flaky patterns, or anti-horizontal slicing. Review-side validation of already-written tests, deeper than aqg-multi-review's edge_cases slice and complementary to aqg-code-construction's construction-side TDD. Signal only - emits a structured ledger, focus prompts, and `needs_llm_judgement`; the caller runs `/audit` only when `docs/policies/audit-trigger.md` requires external review. Not a test runner, coverage-percentage gate, or replacement for host `/review`.
---

# AQG Test-Quality Review (Signal Only)

Review-side analysis of test **quality**, not quantity. It parses a unified diff
(test files and the source they cover), runs precision-first mechanical scans,
and emits **candidates** plus focus prompts. Candidates are not verdicts. The
caller adjudicates them locally, or runs `/audit` at the depth selected by
`docs/policies/audit-trigger.md` when the policy says the logical change needs
external review.

## Quick Contract

- **When**: after tests already exist in a PR or branch diff, and you need to
  know whether they meaningfully cover behavior.
- **Input**: a unified git diff, optionally plus per-commit numstat data for
  real anti-horizontal-slicing detection.
- **Output**: a closeout-importable `test_quality_review` ledger with candidate
  IDs, confidence, dispositions, counts, audit findings, and a final decision.
- **Judgement model**: mechanical scans stay precision-first; semantic calls
  such as "behavior or shape" belong to the caller and, when policy requires it,
  the `/audit` result.
- **Non-goals**: it does not run tests, compute coverage percentages, modify
  files, replace host `/review`, or decide audit depth by itself.
- **Language hygiene**: keep the filled ledger in one working language unless
  quoting code, command output, existing identifiers, or audit IDs.

## Three-layer position

| layer | tool | scope |
|---|---|---|
| construction-side | aqg-code-construction | how to WRITE tests (vertical TDD) |
| **review-side (this)** | **aqg-test-quality-review** | verify ALREADY-written tests' quality |
| 5-dim general review | aqg-multi-review | test quality is only its `edge_cases` dim |

## Concerns Detected

Mechanical candidates are precision-first signals. The ledger disposition is
filled only after local review or `/audit` review.

| concern | mechanical signal (precision-first) | confidence |
|---|---|---|
| `shape_over_behavioral` | newly added test asserts only shape (at least one shape assertion and zero behavioral assertions) | high |
| `coverage_gap` | source changed without a paired changed test | high if the diff has zero test changes; otherwise low |
| `test_suppression` | skip/disable added, or a behavioral assertion deleted | high |
| `flaky` | sleep, wall-clock time, unseeded random, or live network in added test lines | high for sleep/clock, low for random/network |
| `bulk_shape_test_risk` | many shape-only tests in one diff | advisory only; never triggers `needs_llm_judgement` by itself |

`anti_horizontal` is a temporal signal. A single squashed diff cannot prove it,
because vertical and horizontal development can have the same final state. Real
detection requires `--commits-numstat` data so the helper can inspect per-commit
test/implementation interleaving. Single-diff mode only emits the advisory
`bulk_shape_test_risk`.

## How To Run

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script="$aqg_root/skills/aqg-test-quality-review/scripts/aqg_test_quality_review.py"

# 1. Analyze a diff into a ledger with pre-filled candidates and focus prompts.
git diff origin/main...HEAD | python3 "$script" analyze
python3 "$script" analyze --diff-file changes.diff --json   # machine-readable

# 1b. Optionally enable real anti-horizontal detection from commit history.
git log --numstat --format=COMMIT:%H --reverse origin/main..HEAD > /tmp/commits.txt
git diff origin/main...HEAD | python3 "$script" analyze --commits-numstat /tmp/commits.txt

# 2. Decide whether policy requires /audit.
#    If it does, run /audit at the depth selected by docs/policies/audit-trigger.md
#    for this logical change, using the relevant focus prompt(s). Fill candidate
#    dispositions and findings in the ledger.

# 3. Validate the filled ledger to emit needs_llm_judgement and next steps.
python3 "$script" validate --file /tmp/test-quality-ledger.md
```

Local helper needs `pip install pyyaml>=6.0` for `validate` only. Exit codes:
`0` ok / `1` ledger invalid / `2` usage error.

## Ledger Shape

The emitted `test_quality_review` block is designed for closeout evidence:

- `candidates`: stable IDs, concern, confidence, evidence, pattern, and
  disposition.
- `counts`: added-test counts and the test-level `shape_ratio`.
- `bulk_shape_test_risk`: single-diff advisory only.
- `anti_horizontal`: meaningful only when commit-history data is supplied.
- `findings`: caller- or audit-filled conclusions linked to candidate IDs.
- `decision`, `decision_reason`, and `audit_id`: the final adjudication trail.

## Boundaries

- **Read-only**: parses a diff and emits a ledger/signal. It does not modify
  test or source files, run the test suite, or call audit tooling (per ADR
  2026-05-08 section 5). The caller runs `/audit` when policy routes the logical
  change there.
- **Precision-first**: when a pattern is ambiguous the helper does not emit a
  candidate; it defers to judgement. A `mixed` test (type guard plus value assertion)
  is not a shape smell.
- Production, secrets, raw private data, branch protection, and Owner/admin
  actions remain separate authorization gates.

## When To Use Vs Alternatives

- **How to write tests before implementation**: use `aqg-code-construction`.
- **Balanced five-dimension code review**: use `aqg-multi-review`; this skill is
  the deeper test-quality slice behind its `edge_cases` dimension.
- **When and how deeply to audit**: use `aqg-phase-transition` when a phase
  boundary just completed, and use `docs/policies/audit-trigger.md` as the depth
  authority.
- **Whether already-written tests are meaningful**: use this skill.
