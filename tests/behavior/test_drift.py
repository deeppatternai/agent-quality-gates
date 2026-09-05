"""Unit tests for tests/behavior/drift.py (parse_skill_md).

NB (2026-05-31 PR-3): the drift-hash helpers (compute_skill_meta_hashes /
check_skill_drift + the SkillMetaHashes / DriftCheckResult dataclasses) were
retired when the overlay migration reached 13/13 managed skills, so the tests
that exercised them (TestComputeSkillMetaHashes / TestCheckSkillDrift /
TestRealAqgSkills) were removed. parse_skill_md survives and is covered here.
"""

from __future__ import annotations

from pathlib import Path

from tests.behavior.drift import parse_skill_md


SKILL_TEMPLATE = """---
name: test-skill
description: A test skill that does something specific
---

# Test Skill

## How To Run

Use this skill when X happens.
"""


def write_skill(tmp_path: Path, content: str = SKILL_TEMPLATE) -> Path:
    p = tmp_path / "SKILL.md"
    p.write_text(content)
    return p


# ===== parse_skill_md =====


class TestParseSkillMd:
    def test_basic(self, tmp_path):
        p = write_skill(tmp_path)
        result = parse_skill_md(p)
        assert result is not None
        desc, body = result
        assert desc == "A test skill that does something specific"
        # body has leading "\n" because regex captures everything after the
        # closing `---\n` delimiter (including the blank line before "# Test Skill")
        assert "# Test Skill" in body
        assert "## How To Run" in body

    def test_missing_file(self, tmp_path):
        result = parse_skill_md(tmp_path / "nonexistent.md")
        assert result is None

    def test_no_frontmatter(self, tmp_path):
        p = write_skill(tmp_path, "# Just a body, no frontmatter\n")
        result = parse_skill_md(p)
        assert result is None

    def test_frontmatter_no_description(self, tmp_path):
        content = "---\nname: test\n---\n\n# Body\n"
        p = write_skill(tmp_path, content)
        result = parse_skill_md(p)
        assert result is None

    def test_description_with_special_chars(self, tmp_path):
        # Quoted YAML so the colon doesn't break parsing
        content = (
            '---\nname: test-skill\n'
            'description: "Skill with: colons, commas, and 中文 characters"\n'
            '---\n\n# Body\n'
        )
        p = write_skill(tmp_path, content)
        result = parse_skill_md(p)
        assert result is not None
        desc, _ = result
        assert desc == "Skill with: colons, commas, and 中文 characters"

    def test_yaml_block_scalar_description(self, tmp_path):
        # audit d982c3c3 #1: block scalar `description: |` must parse actual lines
        content = (
            "---\n"
            "name: test-skill\n"
            "description: |\n"
            "  Multi-line description.\n"
            "  Second line.\n"
            "---\n\n# Body\n"
        )
        p = write_skill(tmp_path, content)
        result = parse_skill_md(p)
        assert result is not None
        desc, _ = result
        # Block scalar produces a string with newline preserved
        assert "Multi-line description." in desc
        assert "Second line." in desc

    def test_yaml_block_scalar_content_change_detected(self, tmp_path):
        # If two SKILL.md differ only in block-scalar description content, the
        # parsed description must differ (not stuck at the | marker).
        c1 = "---\nname: x\ndescription: |\n  one\n  two\n---\n\nbody\n"
        c2 = "---\nname: x\ndescription: |\n  one\n  three\n---\n\nbody\n"
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        p1 = tmp_path / "a" / "SKILL.md"
        p2 = tmp_path / "b" / "SKILL.md"
        p1.write_text(c1)
        p2.write_text(c2)
        r1 = parse_skill_md(p1)
        r2 = parse_skill_md(p2)
        assert r1 is not None and r2 is not None
        assert r1[0] != r2[0]
