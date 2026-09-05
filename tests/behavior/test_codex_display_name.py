"""CI-gated guard: every shipped AQG skill carries agents/openai.yaml with a
non-empty interface.display_name, so the Codex skill picker never falls back to
the directory slug and renders "Aqg <Name>".

Lives in tests/behavior/ (collected by behavior-tests.yml) ON PURPOSE: the
validator unit tests in top-level tests/ are NOT collected by CI, so a guard
placed there cannot gate a PR (audit 57f41525 f2). This is the recurrence guard
for the 2026-05-28 memory audit — 4 skills (automation-audit, multi-review,
phase-transition, security-review) had shipped without openai.yaml because the
convention lived only in machine-local memory with no repo-side gate.

Reuses scripts/aqg_skill_validator.py::_validate_codex_interface so the gate and
the on-demand validator never disagree about what "has a display_name" means.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import aqg_skill_validator as asv  # noqa: E402

# Self-computing roster from disk (no hardcoded list to bump) — same anti-
# "forgettable touchpoint" discipline as the trigger canary + check_fixture_mix.
SKILL_DIRS = sorted(
    d for d in (REPO / "skills").glob("aqg-*") if d.is_dir()
)


def test_roster_nonempty():
    """Guard the guard: if the glob ever returns nothing the parametrized test
    would vacuously pass, leaving every skill uncovered."""
    assert SKILL_DIRS, "no skills/aqg-* directories found — roster guard is vacuous"


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda d: d.name)
def test_skill_has_codex_display_name(skill_dir: Path):
    violations = asv._validate_codex_interface(skill_dir)
    assert not violations, (
        f"{skill_dir.name}: {violations}\n"
        "  Every Codex-distributed skill needs agents/openai.yaml with a\n"
        "  non-empty interface.display_name, or the Codex picker shows the\n"
        "  'Aqg <Name>' slug fallback. The skill generator scaffolds this;\n"
        "  see docs/SKILL_AUTHORING_GUIDE.md touchpoint 9."
    )
