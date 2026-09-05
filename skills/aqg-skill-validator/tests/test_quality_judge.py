"""Tests for the emit-only SKILL.md-quality judge (Tier 3-9).

aqg-skill-validator gains `build_quality_judge_prompt`: it EMITS (does not run) a
prompt scoring a skill's description against the §2 four-element grammar — the
"larger evaluation worth a separate pass" the GUIDE §2.3b flagged. Emit-only,
mirroring aqg-multi-review: the skill never calls an LLM; the caller runs it via
de_audit. CI-gated here (skills/*/tests is collected; top-level tests/ is not).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# aqg_skill_validator.py lives at the REPO scripts/ dir (not skills/<X>/scripts).
_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "scripts"))

from aqg_skill_validator import (  # noqa: E402
    QUALITY_JUDGE_RUBRIC,
    build_quality_judge_prompt,
)

_FOUR_ELEMENTS = {"lead_verb", "trigger_conditions", "boundary_clause", "no_competing_context"}


def test_rubric_has_the_four_elements():
    assert set(QUALITY_JUDGE_RUBRIC) == _FOUR_ELEMENTS


def test_payload_shape_and_emit_only():
    p = build_quality_judge_prompt("aqg-startup-preflight", repo_root=_REPO)
    assert p["skill"] == "aqg-startup-preflight"
    assert p["skill_md_path"] == "skills/aqg-startup-preflight/SKILL.md"
    assert p["error"] is None
    # description read from the authoritative SKILL.md frontmatter (audit f1)
    assert p["description_source"] == "SKILL.md frontmatter"
    assert "worktree" in p["description"]
    assert set(p["rubric"]) == _FOUR_ELEMENTS
    assert set(p["output_schema"]) == _FOUR_ELEMENTS
    # emit-only contract is stated, and nothing here ran an LLM
    assert "does NOT call an LLM" in p["how_to_run"]
    # Names the SKILL, not a tool name. `de_audit` was never a tool the server
    # exposes, and this assertion is what kept the wrong name pinned in place.
    assert "/audit" in p["how_to_run"]
    assert "de_audit" not in p["how_to_run"]


def test_missing_skill_reports_error_not_raise():
    p = build_quality_judge_prompt("aqg-does-not-exist", repo_root=_REPO)
    assert p["error"]  # non-empty error
    assert p["description"] == ""
    # rubric still present so the payload shape is stable
    assert set(p["rubric"]) == _FOUR_ELEMENTS


def test_pushiness_family_is_covered_by_rubric():
    # The no-competing-context element must name the four pushiness families so
    # the judge can catch them (parity with the static advisory + GUIDE §2.3b).
    text = QUALITY_JUDGE_RUBRIC["no_competing_context"].lower()
    assert "always use" in text
    assert "the only" in text or "exclusiv" in text


def test_cli_quality_judge_emits_json_and_does_not_validate():
    script = _REPO / "scripts" / "aqg_skill_validator.py"
    proc = subprocess.run(
        [sys.executable, str(script), "aqg-startup-preflight", "--quality-judge"],
        text=True, capture_output=True, cwd=str(_REPO),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["skill"] == "aqg-startup-preflight"
    assert set(payload["rubric"]) == _FOUR_ELEMENTS
    # emit-only: no validation verdict keys leak into the judge payload
    assert "is_valid" not in payload


def test_cli_error_path_nonexistent_skill_emits_payload_exit2():
    # audit 529d5d41 f2: the CLI error path must still emit a parseable payload
    # (non-null error, empty description, stable 4-key rubric) and exit 2 — never
    # crash and never silently judge nothing.
    script = _REPO / "scripts" / "aqg_skill_validator.py"
    proc = subprocess.run(
        [sys.executable, str(script), "aqg-nope-does-not-exist", "--quality-judge"],
        text=True, capture_output=True, cwd=str(_REPO),
    )
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    payload = json.loads(proc.stdout)  # still valid JSON on the error path
    assert payload["error"]            # non-null
    assert payload["description"] == ""
    assert payload["description_source"] == "SKILL.md frontmatter"
    assert set(payload["rubric"]) == _FOUR_ELEMENTS
