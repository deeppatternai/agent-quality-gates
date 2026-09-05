"""SKILL.md frontmatter parsing.

Provides ``parse_skill_md`` — the repo's single authoritative SKILL.md
description/body parser (handles unquoted colons, quotes, block scalars, CJK).
Consumed by the trigger canary (tests/behavior/test_aqg_skill_trigger_canary.py)
and the nightly runner.

NB (2026-05-31 PR-3): the SKILL.md drift-hash mechanism (``compute_skill_meta_hashes``
/ ``check_skill_drift`` + the ``SkillMetaHashes`` / ``DriftCheckResult`` dataclasses)
was retired once the overlay migration reached 13/13 managed skills — the committed
wrapper is now guarded by the regen→git-status CI gate, so per-skill hash baselines
no longer apply. Only ``parse_skill_md`` survives here; the module name and import
path are kept stable for its consumers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml  # PyYAML — handles block scalars (`description: |`), quotes, etc.

# SKILL.md format per Claude Code: YAML frontmatter delimited by `---` lines,
# then body markdown. Tightened regex (audit d982c3c3 #2):
# - [^\S\r\n]* allows trailing horizontal whitespace but NOT newlines on delimiter line
# - \r?\n explicit line ending
# - (\r?\n|\Z) allows closing delimiter at end-of-file without trailing newline
_FRONTMATTER_RE = re.compile(
    r"\A---[^\S\r\n]*\r?\n(.*?)\r?\n---[^\S\r\n]*(?:\r?\n|\Z)(.*)\Z",
    re.DOTALL,
)


def parse_skill_md(skill_md_path: Path) -> Optional[tuple[str, str]]:
    """Parse SKILL.md into (description_value, body_markdown).

    Returns None if the file is missing or doesn't match the expected
    frontmatter format, OR if both YAML parse and regex fallback fail to find
    a description.

    Hybrid parser (audit d982c3c3 #1):
    1. Try PyYAML safe_load — handles block scalars (`description: |\n  ...`),
       folded scalars (`description: >`), quoted values cleanly.
    2. Fallback to single-line regex when YAML strict-parse fails (e.g. unquoted
       description containing `: ` like "Close work with Agent: scope, ..." which
       YAML rejects but real SKILL.md files use).
    """
    if not skill_md_path.is_file():
        return None
    text = skill_md_path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    frontmatter, body = m.group(1), m.group(2)

    description: Optional[str] = None

    # Path 1: strict YAML parse (catches block scalars correctly)
    try:
        parsed = yaml.safe_load(frontmatter)
        if isinstance(parsed, dict):
            yaml_desc = parsed.get("description")
            if isinstance(yaml_desc, str) and yaml_desc.strip():
                description = yaml_desc.strip()
    except yaml.YAMLError:
        pass  # fall through to regex

    # Path 2: regex fallback for unquoted single-line descriptions with colons
    if description is None:
        # Reject block-scalar markers (|, >) in fallback — PyYAML must own those
        block_marker = re.search(
            r"^\s*description\s*:\s*[|>]",
            frontmatter,
            re.MULTILINE,
        )
        if block_marker:
            # Block scalar but YAML failed to parse → genuine malformed; return None
            return None
        m_desc = re.search(
            r"^\s*description\s*:\s*(.+?)\s*$",
            frontmatter,
            re.MULTILINE,
        )
        if m_desc:
            description = m_desc.group(1).strip()

    if not description:
        return None
    return description, body
