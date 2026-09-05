#!/usr/bin/env python3
"""Minimal self-test for aqg-skill-validator skill.

The actual validator entry script is at repo-level
`scripts/aqg_skill_validator.py` (see skill.template.json entry_script).
This self-test is a smoke check that the skill's wrapper machinery is in
place: sidecar parses, SKILL.md frontmatter exists, repo-level entry
script can be invoked.

Run with: python3 skills/aqg-skill-validator/scripts/self_test.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_self() -> int:
    skill_dir = Path(__file__).resolve().parent.parent
    repo_root = skill_dir.parent.parent

    # 1. Sidecar parses and schema is valid
    sidecar_path = skill_dir / "skill.template.json"
    assert sidecar_path.is_file(), f"missing sidecar: {sidecar_path}"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sys.path.insert(0, str(repo_root / "scripts"))
    from _skill_template_schema import check_skill_template
    result = check_skill_template(sidecar)
    assert result.is_safe, f"sidecar schema invalid: {result.violations}"

    # 2. SKILL.md exists with name + description frontmatter
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"missing SKILL.md: {skill_md}"
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must start with frontmatter"
    assert "name: aqg-skill-validator" in text, "SKILL.md missing name"
    assert "description:" in text, "SKILL.md missing description"

    # 3. Repo-level entry script invokable with --help
    entry = repo_root / sidecar["entry_script"]
    assert entry.is_file(), f"entry_script missing: {entry}"
    proc = subprocess.run(
        [sys.executable, str(entry), "--help"],
        text=True, capture_output=True,
    )
    assert proc.returncode == 0, f"--help failed: {proc.stderr}"

    print("OK: aqg-skill-validator self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(test_self())
