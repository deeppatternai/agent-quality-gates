"""Trigger canary — each AQG skill's SKILL.md description still contains the
keywords Claude Code needs to route a user prompt to that skill.

What this guards: the description text Claude Code routes on. It names the
specific trigger keyword and explains the routing impact when one goes missing
(more actionable than a bare "the description changed"). Pure static check —
reads the 16 pack SKILL.md files only; no API call, no subprocess, no fixture
dependency; runs in well under 0.5s.

Why the *pack* SKILL.md and not skills/<name> (the source): the AQG installer
symlinks each ~/.claude/skills/aqg-<name> to its
agent-packs/claude-code/skills/<name> directory, so the pack (wrapper)
description is the text the Claude Code runtime actually loads for prompt
routing. Since the SKILL.md-from-source overlay migration (2026-05-31 §6/§7),
the wrapper is a generated verbatim copy of the source frontmatter — there is
no frontmatter overlay, so the wrapper description == the source description
for every skill. The canary therefore reads the source's description through
the wrapper; either file would give the same keywords, and the wrapper is the
routing-authoritative one this canary guards.

Keyword discipline: every term below is a distinctive, trigger-bearing string
*actually present* in the current description, matched as a case-insensitive
word-boundary token/phrase (see _present — not a bare substring, so "accept"
inside "unacceptable" does not count). They are NOT aspirational — a term
Claude Code might route on but the description happens to omit does not belong
here, because the canary would be born red. Editing a description to drop one
of these should either (a) restore the term or (b) update this map with
reasoning.

Parser note: reuses parse_skill_md from tests.behavior.drift — the repo's
single authoritative SKILL.md description parser (handles unquoted colons,
quotes, block scalars, CJK). A hand-rolled regex here could disagree about
what "the description" even is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.behavior.drift import parse_skill_md

# Resolve from this file: tests/behavior/<file> -> parents[2] == repo root.
PACK_SKILLS = (
    Path(__file__).resolve().parents[2] / "agent-packs" / "claude-code" / "skills"
)

# skill -> trigger keywords (each verified present in the live description).
SKILL_TRIGGER_KEYWORDS: dict[str, list[str]] = {
    "aqg-audit-adjudication": [
        "accept", "reject", "needs-user-decision", "adjudication", "audit", "finding",
    ],
    "aqg-automation-audit": [
        "hooks", "MCP", "plugins", "overlap", "automation",
    ],
    "aqg-code-construction": [
        "Pattern Mining", "Behavior Lock", "Local Verification", "6-step",
        "code construction",
    ],
    "aqg-decision-capture": [
        "decisions", "Owner rulings", "adjudications", "redacted",
        "untrusted data", "amnesia",
    ],
    # NB: description now leads with "evidence closeout" and names "PR handoff",
    # so "closeout"/"handoff" are present and trigger-bearing — the follow-up
    # that resolves audit db93def2-f2, which flagged their earlier absence from
    # the wrapper. Still avoid bare generic nouns like "scope" that could
    # survive a non-closeout rewrite.
    "aqg-evidence-closeout": [
        "closeout", "handoff", "evidence", "verification", "audit adjudication",
        "durable state", "remaining blockers",
    ],
    "aqg-memory-hygiene": [
        "memory hygiene", "frontmatter", "staleness", "volatile", "superseded",
        "schema drift", "last_verified",
    ],
    "aqg-multi-review": [
        "5-dimension", "logic", "edge_cases", "security", "performance", "concurrency",
    ],
    "aqg-phase-transition": [
        "PLAN_DONE", "IMPL_DONE", "TESTS_WRITTEN", "recommended audit mode",
    ],
    "aqg-project-status": [
        "project status", "progress report", "Bug ledger", "defect ledger",
        "progress timeline",
    ],
    "aqg-re-anchor": [
        "re-anchor", "goal-drift", "attention decay", "restatement",
    ],
    "aqg-security-review": [
        "OWASP Top 10", "CWE Top 25", "authentication", "authorization", "credentials",
    ],
    "aqg-session-handoff": [
        "cross-session", "paste-ready", "cold-start", "session handoff",
        "eaf-session-handoff",
    ],
    "aqg-skill-validator": [
        "sidecar manifest", "SKILL.md", "frontmatter", "boundary section",
        "cross-cutting",
    ],
    "aqg-startup-preflight": [
        "worktree", "dirty", "behind remote", "SESSION START",
    ],
    # NB: description reads "... for failing checks, CI errors, preflight failures
    # ..." — it says "CI errors" (not "CI failures") and has no "test failures".
    "aqg-systematic-debugging": [
        "root-cause", "debugging", "failing checks", "CI errors", "preflight failures",
    ],
    "aqg-test-quality-review": [
        "BEHAVIOR", "SHAPE", "test-QUALITY", "flaky", "weakened",
    ],
}


def _description(skill_name: str) -> str:
    """Return the parsed SKILL.md description for a pack skill (fail loud if absent)."""
    parsed = parse_skill_md(PACK_SKILLS / skill_name / "SKILL.md")
    assert parsed is not None, (
        f"{skill_name}: SKILL.md missing or has no parseable frontmatter description"
    )
    description, _ = parsed
    return description


def _present(description: str, keyword: str) -> bool:
    """True if keyword occurs as a standalone token/phrase (word-boundary,
    case-insensitive).

    Word-boundary, not bare substring (audit db93def2-f1): otherwise a trigger
    term could be "found" embedded in an unrelated word — e.g. "accept" inside
    "unacceptable" — leaving the canary green after the real term is gone.
    ``\\b`` anchors only the ends, so multiword phrases still match.
    """
    pattern = rf"\b{re.escape(keyword)}\b"
    return re.search(pattern, description, re.IGNORECASE) is not None


@pytest.mark.parametrize(
    "skill_name, keywords", sorted(SKILL_TRIGGER_KEYWORDS.items())
)
def test_description_contains_trigger_keywords(skill_name: str, keywords: list[str]):
    desc = _description(skill_name)
    missing = [kw for kw in keywords if not _present(desc, kw)]
    assert not missing, (
        f"{skill_name}: description no longer contains trigger keyword(s): {missing}\n"
        f"  Claude Code routes prompts to this skill via these terms; dropping one\n"
        f"  weakens trigger matching. Restore the term, or update\n"
        f"  SKILL_TRIGGER_KEYWORDS with reasoning.\n"
        f"  current description: {desc[:200]}"
    )


def test_keyword_map_covers_skill_roster():
    """Self-computing: the canary map tracks the live skill roster 1:1.

    Derives the roster from disk (no hardcoded count to bump), so adding a new
    skill without a canary entry — or leaving a stale entry after a rename/removal
    — fails loudly here instead of silently going uncovered. Same anti-
    "forgettable touchpoint" discipline as scripts/check_fixture_mix.py
    (ADR 2026-05-26-self-computing-fixture-gate).
    """
    on_disk = {
        p.name
        for p in PACK_SKILLS.iterdir()
        if p.is_dir() and p.name.startswith("aqg-")
    }
    mapped = set(SKILL_TRIGGER_KEYWORDS)
    assert mapped == on_disk, (
        "trigger-canary map out of sync with skill roster:\n"
        f"  on disk but missing a canary entry: {sorted(on_disk - mapped)}\n"
        f"  canary entry with no skill on disk: {sorted(mapped - on_disk)}\n"
        "  Edit SKILL_TRIGGER_KEYWORDS to match agent-packs/claude-code/skills/."
    )


def test_every_mapped_skill_has_keywords():
    """No empty keyword lists (audit db93def2-f3).

    A skill mapped as ``"aqg-x": []`` would satisfy the roster check AND pass
    the parametrized keyword test (empty 'missing'), leaving the skill silently
    uncovered. Each entry must carry >=1 real trigger keyword.
    """
    empty = sorted(skill for skill, kws in SKILL_TRIGGER_KEYWORDS.items() if not kws)
    assert not empty, (
        f"skills mapped with an empty keyword list (no real canary): {empty}\n"
        "  Give each entry >=1 trigger keyword, or remove the skill from the map."
    )
