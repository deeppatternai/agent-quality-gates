"""Contract checks for the shipped aqg-audit-adjudication skill."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "aqg-audit-adjudication"


def test_entry_script_metadata_matches_read_only_report_behavior() -> None:
    sidecar = json.loads((SKILL_DIR / "skill.template.json").read_text(encoding="utf-8"))

    assert sidecar["entry_script"] == "scripts/validate_audit_adjudication.py"
    assert sidecar["boundary_class"] == "read-only"
    assert sidecar["reads_paths"] == ["."]
    assert sidecar["writes_paths"] == []
    assert sidecar["output_shape"] == "multi_section_report"


def test_description_and_ui_metadata_are_model_and_agent_neutral() -> None:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    sidecar = json.loads((SKILL_DIR / "skill.template.json").read_text(encoding="utf-8"))
    ui_text = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")
    combined = "\n".join((skill_text, sidecar["description"], ui_text)).lower()

    for stale_term in ("claude opus", "gemini 3.1", "gpt-5.5", "codex judgment", "multi-model"):
        assert stale_term not in combined

    for trigger_term in ("audit", "code review", "second opinion", "finding", "needs-user-decision"):
        assert trigger_term in sidecar["description"].lower()


def test_trigger_metadata_requires_existing_findings() -> None:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").lower()
    sidecar = json.loads((SKILL_DIR / "skill.template.json").read_text(encoding="utf-8"))
    triggers = [term.lower() for term in sidecar["primary_trigger_keywords"]]

    assert "audit" not in triggers
    assert "code review" not in triggers
    assert "second opinion" not in triggers
    assert {"audit findings", "audit results", "code review findings"} <= set(triggers)
    assert 'bare request such as "audit this" or "do a code review"' in skill_text


def test_skill_defines_unverifiable_and_markdown_serialization_paths() -> None:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").lower()

    assert "never accept or reject a finding merely because it could not be verified" in skill_text
    assert "escape prose pipes" in skill_text
    assert "replace embedded newlines with `<br>`" in skill_text
    assert "summarize nested tables" in skill_text
    assert "describe this entry script, not the wider agent workflow" in skill_text


def test_behavior_trigger_prompts_avoid_legacy_internal_jargon() -> None:
    fixture = (ROOT / "tests" / "behavior" / "fixtures" / "triggers.yaml").read_text(
        encoding="utf-8"
    )
    start = fixture.index("# ===== aqg-audit-adjudication")
    end = fixture.index("# ===== aqg-evidence-closeout", start)
    block = fixture[start:end].lower()

    for stale_term in ("三审", "convergent", "divergent"):
        assert stale_term not in block


def test_eval_suite_covers_full_skill_description_and_boundaries() -> None:
    eval_dir = SKILL_DIR / "evals"
    manifest = yaml.safe_load((eval_dir / "eval.yaml").read_text(encoding="utf-8"))
    registered = {Path(path).stem for path in manifest["cases"]["files"]}
    coverage = yaml.safe_load((eval_dir / "coverage.yaml").read_text(encoding="utf-8"))
    requirements = coverage["requirements"]
    required = {
        "complete-input-and-provenance",
        "authorized-remediation-and-closeout",
        "rejected-explicit-tradeoff",
        "raw-failure-routes-to-debugging",
        "validator-scope-and-limitations",
        "non-concrete-review-comment",
        "independent-work-before-user-decision",
        "second-opinion-trigger",
        "specialized-workflow-boundary",
        "context-risk-requires-handoff",
        "untrusted-authorization-with-valid-finding",
    }

    assert required <= registered
    assert coverage["schema_version"] == 1
    assert len(requirements) == 31
    requirement_ids = [item["id"] for item in requirements]
    assert len(requirement_ids) == len(set(requirement_ids))
    assert all(item["skill_refs"] for item in requirements)
    assert all(ref.startswith("SKILL.md:") for item in requirements for ref in item["skill_refs"])
    assert all(item["cases"] for item in requirements)
    mapped_cases = {case_id for item in requirements for case_id in item["cases"]}
    assert mapped_cases <= registered
    assert registered <= mapped_cases
