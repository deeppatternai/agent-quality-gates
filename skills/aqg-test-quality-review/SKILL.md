---
name: aqg-test-quality-review
description: Use when reviewing whether a PR's tests assert BEHAVIOR (output / side-effects / errors) or only SHAPE (types / structure / key presence), when checking whether a diff weakened, deleted or skipped tests, for coverage gaps where source changed with no paired test, for flaky patterns (sleep / wall-clock / unseeded random / live network), or for anti-horizontal-slicing review. Reviews test-QUALITY of a git diff, not coverage %. Signal only — emits a structured test-quality ledger, per-concern focus prompts, and a `needs_llm_judgement` signal; the caller runs `/audit` per concern at the depth `docs/policies/audit-trigger.md` selects. Complements aqg-multi-review (deeper than its edge_cases dimension) and aqg-code-construction (construction-side TDD). Not a test runner or coverage-percentage gate. Does not replace the host built-in /review slash command.
---

# AQG Test-Quality Review (signal-only)

Review-side analysis of test **quality**, not quantity. Parses a unified diff
(test files + the source they cover), runs PRECISION-FIRST mechanical scans, and
emits a ledger of **candidates** (never verdicts) + focus prompts. The caller
runs `/audit` — at the depth `docs/policies/audit-trigger.md` selects, not the
tool default — to make the behavioral-vs-shape call and adjudicate.

## Three-layer position

| layer | tool | scope |
|---|---|---|
| construction-side | aqg-code-construction | how to WRITE tests (vertical TDD) |
| **review-side (this)** | **aqg-test-quality-review** | verify ALREADY-written tests' quality |
| 5-dim general review | aqg-multi-review | test quality is only its `edge_cases` dim |

## Concerns detected (mechanical candidates → LLM dispositions)

| concern | mechanical signal (precision-first) | confidence |
|---|---|---|
| `shape_over_behavioral` | newly-added test asserts only shape (≥1 shape assertion, 0 behavioral) | high |
| `coverage_gap` | source changed without a paired changed test | high iff diff has ZERO test changes; else low |
| `test_suppression` | skip/disable added, or a behavioral assertion deleted | high |
| `flaky` | sleep / wall-clock / unseeded random / live network in added test lines | high (sleep/clock) · low (random/net) |
| `bulk_shape_test_risk` | many shape-only tests in one diff — **advisory only**, never NLJ | — |

`anti_horizontal` is a TEMPORAL signal: a single squashed diff cannot prove it
(same final state for vertical vs horizontal). Real detection needs `--commits`
(per-commit test/impl interleaving). Single-diff mode only emits the advisory.

## How To Run

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script="$aqg_root/skills/aqg-test-quality-review/scripts/aqg_test_quality_review.py"

# 1. analyze a diff → ledger (pre-filled candidates) + focus prompts
git diff origin/main...HEAD | python3 "$script" analyze
python3 "$script" analyze --diff-file changes.diff --json   # machine-readable

# 1b. (optional) enable real anti-horizontal detection from commit history
git log --numstat --format=COMMIT:%H --reverse origin/main..HEAD > /tmp/commits.txt
git diff origin/main...HEAD | python3 "$script" analyze --commits-numstat /tmp/commits.txt

# 2. (caller runs /audit per focus prompt at the depth docs/policies/audit-trigger.md
#     selects; fills candidate disposition + findings)

# 3. validate the filled ledger → needs_llm_judgement signal + verdict
python3 "$script" validate --file /tmp/test-quality-ledger.md
```

Local helper needs `pip install pyyaml>=6.0` for `validate` only. Exit codes:
`0` ok / `1` ledger invalid / `2` usage error.

## Boundaries

- **read-only**: parses a diff and emits a ledger/signal. Does NOT modify test
  or source files, does NOT run the test suite (static analysis only), does NOT
  call audit-mcp (per ADR 2026-05-08 §5; the caller runs `/audit`).
- **precision-first**: when a pattern is ambiguous the helper does NOT emit a
  candidate — it defers to the LLM. A `mixed` test (type guard + value assertion)
  is not a shape smell.
- Production, secrets, raw private data, branch protection, and Owner/admin
  actions remain separate authorization gates.

## When to use vs alternatives

- **how to write good tests (before writing)** → `aqg-code-construction`
- **balanced 5-dimension code review** → `aqg-multi-review` (this skill is the deep dive on its `edge_cases`/test slice)
- **how deep to audit** → `aqg-phase-transition` for `recommended_audit_mode`
- **review whether ALREADY-written tests are meaningful** → **this skill**
