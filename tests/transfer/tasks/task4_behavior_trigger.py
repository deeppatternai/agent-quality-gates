"""Task 4 — 5-skill behavior trigger sanity (structure check).

Pinned 5-case set per §3 Task 4 (4 description-based + 1 explicit
≈ 70/30 GUIDE §7 minimum). Loads cases by id from
tests/behavior/fixtures/triggers.yaml and asserts each pinned case is
well-formed (expected_skill + style match the pin, skill_file_ref
resolves to a real SKILL.md).

NB (2026-05-31 PR-3): the SKILL.md drift-hash mechanism was retired once
the overlay migration reached 13/13 managed skills — the committed wrapper
is now guarded by the regen→git-status CI gate, so per-case hash baselines
no longer apply. This task previously cross-checked per-case drift hashes
via check_skill_drift; that function no longer exists, so the task now
validates fixture structure only.

Sketch a2 §3 used a placeholder case_id naming (e.g. `adjudication-...`);
this implementation uses the real triggers.yaml case_ids
(`adjudicate-...` / `construction-...`).

Pass criteria: every pinned case loadable + expected_skill/style match +
skill_file_ref points at an existing SKILL.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner import TaskOutcome  # noqa: E402


PINNED_CASES = (
    ("preflight-desc-001", "aqg-startup-preflight", "description_based"),
    ("debug-desc-001", "aqg-systematic-debugging", "description_based"),
    ("adjudicate-desc-001", "aqg-audit-adjudication", "description_based"),
    ("closeout-desc-004", "aqg-evidence-closeout", "description_based"),
    ("construction-explicit-001", "aqg-code-construction", "explicit_invocation"),
)


def _load_yaml(path: Path) -> Any:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run(repo: Path) -> TaskOutcome:
    triggers = repo / "tests" / "behavior" / "fixtures" / "triggers.yaml"
    if not triggers.is_file():
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=len(PINNED_CASES),
            notes=f"triggers.yaml missing at {triggers}",
        )

    try:
        loaded = _load_yaml(triggers)
    except Exception as exc:
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=len(PINNED_CASES),
            notes=f"yaml parse error: {exc}",
        )

    cases_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(loaded, dict):
        case_list = loaded.get("cases", [])
    else:
        case_list = loaded if isinstance(loaded, list) else []
    for c in case_list:
        if isinstance(c, dict) and isinstance(c.get("id"), str):
            cases_by_id[c["id"]] = c

    checks_total = len(PINNED_CASES)
    checks_passed = 0
    notes_lines: list[str] = []

    for case_id, expected_skill, expected_style in PINNED_CASES:
        case = cases_by_id.get(case_id)
        if case is None:
            notes_lines.append(f"{case_id}: missing in triggers.yaml")
            continue
        actual_skill = case.get("expected_skill")
        if actual_skill != expected_skill:
            notes_lines.append(
                f"{case_id}: expected_skill {actual_skill!r} != pinned {expected_skill!r}"
            )
            continue
        actual_style = case.get("style")
        if actual_style != expected_style:
            notes_lines.append(
                f"{case_id}: style {actual_style!r} != pinned {expected_style!r}"
            )
            continue
        skill_ref = case.get("skill_file_ref")
        if not isinstance(skill_ref, str) or not skill_ref:
            notes_lines.append(f"{case_id}: missing skill_file_ref")
            continue
        skill_md = repo / skill_ref
        if not skill_md.is_file():
            notes_lines.append(
                f"{case_id}: skill_file_ref target missing: {skill_ref}"
            )
            continue
        checks_passed += 1
        notes_lines.append(f"{case_id}: structure ok")

    if checks_passed == checks_total:
        return TaskOutcome(
            result="pass",
            exit_code=0,
            required_checks_passed=checks_passed,
            required_checks_total=checks_total,
            notes="; ".join(notes_lines),
        )
    return TaskOutcome(
        result="fail",
        exit_code=1,
        required_checks_passed=checks_passed,
        required_checks_total=checks_total,
        notes="; ".join(notes_lines),
    )
