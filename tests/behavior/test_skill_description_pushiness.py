"""CI-gated guard: no shipped AQG skill description uses competing-context /
pushiness phrasing (#183 Tier B5; Anthropic skill-creator).

Lives in tests/behavior/ (collected by behavior-tests.yml) ON PURPOSE: the
validator unit tests in top-level tests/ are NOT collected by CI (audit
57f41525 f2), so a guard placed there cannot gate a PR. Mirrors the
test_codex_display_name.py roster guard.

A description should scope WHEN to use the skill (triggers, e.g. "Use
PROACTIVELY when …") — NOT claim universal priority ("always use", "best
skill", "for all tasks"). Pushy descriptions dilute the whole pack's
auto-trigger signal: if every skill shouts "always use me", the model's
selection gets noisier.

Two SEPARATE scopes (audit f93ffaab gpt-5.5 f1 — not a contradiction):
  - the **validator** treats pushiness as an ADVISORY warning for *any* author
    validating *any* skill (wording is a judgment call → never blocks
    `validate_skill`);
  - this **CI guard** separately hard-gates AQG's OWN shipped pack at zero
    pushiness, so a regression in our descriptions is visible — exactly the
    `test_codex_display_name.py` roster-guard precedent.
Reuses aqg_skill_validator._check_description_pushiness so the gate and the
on-demand validator never disagree about what "pushy" means.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import aqg_skill_validator as asv  # noqa: E402

# Self-computing roster from disk (no hardcoded list to bump) — same anti-
# "forgettable touchpoint" discipline as the codex display_name guard.
SKILL_DIRS = sorted(d for d in (REPO / "skills").glob("aqg-*") if d.is_dir())


def _description(skill_dir: Path) -> str:
    sidecar = skill_dir / "skill.template.json"
    if not sidecar.is_file():
        return ""
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    desc = data.get("description")
    return desc if isinstance(desc, str) else ""


def test_roster_nonempty():
    """Guard the guard: an empty glob would make the parametrized test pass
    vacuously, leaving every shipped description uncovered."""
    assert SKILL_DIRS, "no skills/aqg-* directories found — roster guard is vacuous"


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda d: d.name)
def test_shipped_description_is_not_pushy(skill_dir: Path):
    advisories = asv._check_description_pushiness(_description(skill_dir))
    assert not advisories, (
        f"{skill_dir.name}: {advisories}\n"
        "  A skill description should scope WHEN to use it (triggers), not claim\n"
        "  universal priority. Rephrase the flagged wording (Anthropic\n"
        "  skill-creator; docs/SKILL_AUTHORING_GUIDE.md §2.3b)."
    )


def test_check_catches_known_pushy_phrasing():
    """Prove the guard is live, not vacuous: known-pushy phrasings must flag."""
    for pushy in (
        "ALWAYS use this skill for all code changes.",
        "The best skill for reviewing code — use it for everything.",
        "The only tool you need; never use any other reviewer.",
        "You must always run this on every task.",
    ):
        assert asv._check_description_pushiness(pushy), pushy
