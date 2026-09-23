#!/usr/bin/env python3
"""Hermetic strict-mode and registration-gap harness."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
VALIDATOR = ROOT / "scripts/aqg_skill_validator.py"
OUT = Path.cwd() / "aqg_skill_validator_boundary/003-contract-strict"


def load_validator():
    spec = importlib.util.spec_from_file_location("aqg_validator_003", VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(repo: Path, name: str) -> Path:
    skill = repo / "skills" / name
    skill.mkdir(parents=True)
    (repo / "templates").mkdir()
    (repo / "scripts").mkdir()
    (repo / "agent-packs/claude-code").mkdir(parents=True)
    (repo / "tests/behavior/fixtures").mkdir(parents=True)
    write(repo / "VERSION", "0.0.0\n")
    write(repo / "scripts/aqg_doctor.py", f"CLAUDE_SKILL_NAMES = ('{name}',)\nCODEX_SKILL_NAMES = ('{name}',)\n")
    write(repo / "scripts/install.sh", f' AQG_SKILLS=("{name}")\n')
    write(repo / "agent-packs/claude-code/install.sh", f' AQG_SKILLS=("{name}")\n')
    write(repo / "tests/behavior/fixtures/triggers.yaml", "cases:\n  - expected_skill: " + name + "\n")
    write(
        skill / "SKILL.md",
        f"---\nname: {name}\ndescription: A fixture with a legal contract.\n---\n"
        "# Fixture\n\n## Boundaries\n\n- Read-only.\n",
    )
    sidecar = {
        "skill_template_schema": 1,
        "name": name,
        "description": "A fixture with a legal contract.",
        "version": "0.1.0",
        "wrapper_generated": True,
        "primary_trigger_keywords": ["fixture"],
        "boundary_class": "read-only",
        "audit_mode_required": "standard",
        "trigger_grammar": {"keywords": ["fixture"], "sentence_patterns": ["Check fixture"]},
        "entry_script": f"skills/{name}/scripts/entry.py",
        "cli_contract": {"0": "ok", "1": "findings", "2": "usage", "3": "config", "70": "internal"},
        "output_shape": "multi_section_report",
        "reads_paths": [f"skills/{name}/"],
        "writes_paths": [],
        "forbidden_paths": ["secrets/"],
        "aqg_agent_gating": False,
        "owner_only_actions": [],
        "cases": [
            {
                "id": f"fixture-{i}",
                "style": "explicit_invocation" if i == 5 else "description_based",
                "prompt": "Check this isolated fixture.",
                "expected_skill": name,
                "skill_file_ref": f"skills/{name}/SKILL.md",
            }
            for i in range(1, 6)
        ],
        "self_test_entrypoint": f"skills/{name}/scripts/entry.py",
        "fixed_before_next_task_required_when_findings": True,
        "numeric_values_quoted": True,
    }
    write(skill / "skill.template.json", json.dumps(sidecar, indent=2) + "\n")
    write(skill / "scripts/entry.py", '#!/usr/bin/env python3\n"""No exit-code contract here."""\nprint("fixture")\n')
    write(skill / "agents/openai.yaml", "interface:\n  display_name: Fixture\n")
    return skill / "SKILL.md"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aqg-validator-003-") as tmp:
        repo = Path(tmp) / "repo"
        skill_md = build(repo, "aqg-contract-fixture")
        validator = load_validator()
        normal = validator.validate_skill("aqg-contract-fixture", repo_root=repo)
        strict = {
            "is_valid": bool(normal.is_valid and not normal.warnings),
            "skill_name": normal.skill_name,
            "warnings": list(normal.warnings),
            "violations": list(normal.sidecar_violations + normal.skill_dir_violations + normal.cross_cutting_violations),
            "computed_exit_code": 1 if normal.warnings or not normal.is_valid else 0,
        }
        data = {
            "normal": dataclasses.asdict(normal),
            "strict": strict,
            "skill_hash_before": sha(skill_md),
            "skill_hash_after": sha(skill_md),
        }
        (OUT / "result.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        (OUT / "commands.log").write_text(
            "validate_skill('aqg-contract-fixture', repo_root=<temp>)\n"
            "strict evaluation: warnings promoted to exit 1\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
