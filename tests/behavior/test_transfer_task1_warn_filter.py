"""The fresh-checkout gate's WARN filter, which nothing tested.

`tests/transfer/tasks/task1_fresh_checkout.py` runs only under
`tests/transfer/runner.py` in the paths-filtered `transfer-test-pack` workflow,
so pytest never collected it. A review demonstrated the consequence: inverting
`_is_absent_rules_block` to `return True` — which makes the gate swallow the
STALE rules-block WARN as well, the exact failure the filter's comment says must
stay counted — left the entire suite green.

The distinction this file pins is the whole reason the helper exists:

- **absent block on claude/codex** — a documented MANUAL step (README has the
  user `cat >>` it). No installer writes it, so every fresh checkout and every CI
  runner WARNs by construction. Excluded, or a correct install reads as a failure.
- **absent block on cursor** — `install_cursor_support.py` DOES write `aqg.mdc`.
  Absence means the installer failed, so it must keep failing the gate. An
  earlier version of the filter keyed on the `rules_block:` prefix alone and
  swallowed this too.
- **stale block** — present but carrying retired framing. A real defect, counted.
- **anything else** — counted.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_TASK1 = REPO / "tests" / "transfer" / "tasks" / "task1_fresh_checkout.py"


def _load_task1():
    """Import the task module directly — it is not on any package path."""
    sys.path.insert(0, str(REPO / "tests" / "transfer"))
    spec = importlib.util.spec_from_file_location("aqg_task1_under_test", _TASK1)
    module = importlib.util.module_from_spec(spec)
    sys.modules["aqg_task1_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _warn(name: str, detail: str) -> dict:
    return {"name": name, "status": "WARN", "detail": detail}


ABSENT_CLAUDE = _warn(
    "rules_block:claude",
    "claude is installed (.claude exists) but has no AQG rules block: CLAUDE.md is missing",
)
ABSENT_CODEX = _warn(
    "rules_block:codex",
    "codex is installed (.codex exists) but has no AQG rules block: AGENTS.md is missing",
)
ABSENT_CURSOR = _warn(
    "rules_block:cursor",
    "cursor is installed (.cursor exists) but has no AQG rules block: aqg.mdc is missing",
)
STALE_CLAUDE = _warn(
    "rules_block:claude",
    "stale rules block: still references audit-self-routing — retired framing",
)
UNRELATED = _warn("cli:gh", "gh is not on PATH")


def test_manual_step_hosts_are_excluded_so_a_correct_install_is_not_a_failure() -> None:
    task1 = _load_task1()
    assert task1._is_absent_rules_block(ABSENT_CLAUDE) is True
    assert task1._is_absent_rules_block(ABSENT_CODEX) is True


def test_cursor_is_not_excluded_because_its_installer_writes_the_rule() -> None:
    """The distinction the first version of this filter got wrong."""
    task1 = _load_task1()
    assert task1._is_absent_rules_block(ABSENT_CURSOR) is False, (
        "cursor's rule IS installer-written (install_cursor_support.py writes "
        "aqg.mdc), so its absence is a real regression and must keep failing the gate"
    )


def test_a_stale_block_is_a_real_defect_and_stays_counted() -> None:
    task1 = _load_task1()
    assert task1._is_absent_rules_block(STALE_CLAUDE) is False


def test_unrelated_warns_are_never_swallowed() -> None:
    task1 = _load_task1()
    assert task1._is_absent_rules_block(UNRELATED) is False


def test_the_filter_changes_the_gate_outcome_it_is_supposed_to_change() -> None:
    """End-to-end on the counting expression, not just the predicate.

    Asserting the helper alone would not prove the gate uses it, which is the
    shape of bug that made the original filter invisible to the suite.
    """
    task1 = _load_task1()
    ci_like = [ABSENT_CLAUDE, ABSENT_CODEX]
    counted = sum(1 for r in ci_like if not task1._is_absent_rules_block(r))
    assert counted == 0, "a fresh CI checkout must not fail on the manual step"

    regressed = ci_like + [ABSENT_CURSOR]
    counted = sum(1 for r in regressed if not task1._is_absent_rules_block(r))
    assert counted == 1, "a cursor-installer regression must still fail the gate"

    stale = ci_like + [STALE_CLAUDE]
    counted = sum(1 for r in stale if not task1._is_absent_rules_block(r))
    assert counted == 1, "a stale block must still fail the gate"
