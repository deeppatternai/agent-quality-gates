"""Corpus guard: every managed AQG skill description stays within the
validate_agent_pack hard limit (DESCRIPTION_MAX_CHARS = 1024), across all
three description locations.

Why this lives in tests/behavior/: validate_agent_pack.py is NOT wired into PR
CI, so a wrapper whose description exceeded 1024 chars (aqg-memory-hygiene
shipped at 1057 via PR #253) went undetected. behavior-tests.yml DOES gate
tests/behavior/, so these corpus assertions now fail the PR if any managed
skill's description breaches the limit — closing the gap that let the
regression land.

Three description locations (audit 5257123e f1):
  - wrapper  agent-packs/claude-code/skills/<X>/SKILL.md — the text the Claude
             Code runtime actually loads for prompt routing (regenerated from
             source by aqg_skill_gen.py).
  - source   skills/<X>/SKILL.md — the hand-edited truth `regen` reads.
  - sidecar  skills/<X>/skill.template.json — the full-gen scaffold input;
             NOT read by `regen` (wrapper comes from source), so it sits
             outside the CI regen-no-diff gate and needs its own length guard.
The wrapper<->source byte-identity is already enforced by the CI
"regen --all → git-status clean" gate; this file additionally pins that
aqg-memory-hygiene stays byte-identical across all three locations, since the
sidecar is the one location no existing gate covers.

Single source of truth: the 1024 ceiling is imported from validate_agent_pack
(not hardcoded). Descriptions are read through tests/behavior/drift.parse_skill_md
— the repo's authoritative SKILL.md frontmatter parser (also used by the
trigger canary), which handles unquoted colons / quotes / block scalars / CJK.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.behavior.drift import parse_skill_md

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import validate_agent_pack  # noqa: E402  (module-level constant; import is side-effect-free)

PACK_SKILLS = REPO / "agent-packs" / "claude-code" / "skills"
SRC_SKILLS = REPO / "skills"
MAX_CHARS = validate_agent_pack.DESCRIPTION_MAX_CHARS


def _wrapper_skill_dirs() -> list[Path]:
    """Every managed Claude-pack wrapper skill dir that ships a SKILL.md."""
    if not PACK_SKILLS.is_dir():
        return []
    return sorted(
        p for p in PACK_SKILLS.iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file()
    )


def _source_skill_dirs() -> list[Path]:
    """Every managed source skill dir (skills/aqg-*) that ships a SKILL.md."""
    if not SRC_SKILLS.is_dir():
        return []
    return sorted(
        p for p in SRC_SKILLS.glob("aqg-*")
        if p.is_dir() and (p / "SKILL.md").is_file()
    )


def _skill_md_description(skill_md: Path) -> str:
    parsed = parse_skill_md(skill_md)
    assert parsed is not None, (
        f"{skill_md}: missing or has no parseable frontmatter description"
    )
    return parsed[0]


def _sidecar_description(sidecar: Path) -> str:
    return json.loads(sidecar.read_text(encoding="utf-8")).get("description", "")


# --- wrapper: the text Claude Code runtime actually loads ------------------
@pytest.mark.parametrize("skill_dir", _wrapper_skill_dirs(), ids=lambda p: p.name)
def test_wrapper_description_within_hard_limit(skill_dir: Path) -> None:
    length = len(_skill_md_description(skill_dir / "SKILL.md"))
    assert length <= MAX_CHARS, (
        f"{skill_dir.name}: wrapper description length {length} exceeds "
        f"{MAX_CHARS}-char hard limit (validate_agent_pack.DESCRIPTION_MAX_CHARS)"
    )


# --- source SKILL.md: the hand-edited truth that regen reads ---------------
@pytest.mark.parametrize("skill_dir", _source_skill_dirs(), ids=lambda p: p.name)
def test_source_description_within_hard_limit(skill_dir: Path) -> None:
    length = len(_skill_md_description(skill_dir / "SKILL.md"))
    assert length <= MAX_CHARS, (
        f"{skill_dir.name}: source SKILL.md description length {length} exceeds "
        f"{MAX_CHARS}-char hard limit"
    )


# --- sidecar skill.template.json: full-gen input, outside the regen gate ----
@pytest.mark.parametrize(
    "skill_dir",
    [d for d in _source_skill_dirs() if (d / "skill.template.json").is_file()],
    ids=lambda p: p.name,
)
def test_sidecar_description_within_hard_limit(skill_dir: Path) -> None:
    length = len(_sidecar_description(skill_dir / "skill.template.json"))
    assert length <= MAX_CHARS, (
        f"{skill_dir.name}: sidecar description length {length} exceeds "
        f"{MAX_CHARS}-char hard limit"
    )


# --- three-location byte-identity for the skill this guard protects --------
def test_memory_hygiene_description_synced_across_locations() -> None:
    """aqg-memory-hygiene description must stay byte-identical across source
    SKILL.md, generated wrapper SKILL.md, and sidecar — the contract this fix
    established. Addresses audit 5257123e f1: the sidecar sits outside the CI
    regen-no-diff gate (which only guards wrapper<->source), so a future
    source/sidecar divergence would otherwise go uncaught."""
    name = "aqg-memory-hygiene"
    source = _skill_md_description(SRC_SKILLS / name / "SKILL.md")
    wrapper = _skill_md_description(PACK_SKILLS / name / "SKILL.md")
    sidecar = _sidecar_description(SRC_SKILLS / name / "skill.template.json")
    assert source == wrapper == sidecar, (
        f"{name}: description diverged across locations — "
        f"source={len(source)} wrapper={len(wrapper)} sidecar={len(sidecar)}"
    )


def test_at_least_one_pack_skill_discovered() -> None:
    """Guard against the parametrize source silently going empty (e.g. a moved
    PACK_SKILLS path), which would make the limit tests vacuously pass."""
    assert _wrapper_skill_dirs(), (
        f"no pack skill dirs with SKILL.md found under {PACK_SKILLS} — "
        f"the corpus guard would be vacuous"
    )
