"""Security-boundary canary — each AQG skill that promises "I do not touch
production / secrets / Owner-admin myself" still carries that promise verbatim
in its SKILL.md *body*.

What this guards, and why the existing checks do not:

- The *trigger* canary (test_aqg_skill_trigger_canary.py) guards the frontmatter
  `description` routing keywords. It never reads the body, so deleting a body
  boundary line leaves it green.
- The skill validator (scripts/aqg_skill_validator.py `_validate_boundary_section`)
  asserts a boundary-section H2 *exists* (`## Boundaries` etc.) via a header
  regex; it never reads the text under that header. A diff that keeps the header
  and drops the "Production / secrets / Owner-admin ... remain separate
  authorization gates" line passes validation today.

This canary closes that gap. It is the AQG analogue of ponytail's
`check-rule-copies.js` INVARIANTS check: pin the load-bearing safety phrases so
a reword or an over-eager edit can't silently drop one. AQG skills are
"surface only — the human acts"; the body promise that the skill does not itself
reach production / secrets / Owner-admin is exactly the safety invariant that
must survive every SKILL.md edit.

Why a signature substring, not boundary-section parsing: each promise has one
distinctive signature phrase that appears ONLY in the boundary declaration and
nowhere else in the file —

    "remain separate authorization gates"   (8 skills incl aqg-session-handoff)
    "remain separate checks"                (aqg-startup-preflight variant)
    "Forbidden touch"                       (aqg-skill-validator, read-only validator)

so a plain substring check catches its deletion without having to locate the
section or normalise the four accepted H2 spellings. Substring (not the trigger
canary's word-boundary regex) is used deliberately: these are long multi-word /
CJK phrases with no embedded-substring ambiguity, and `\b` does not work on CJK.

Pack vs source: reads the pack (wrapper) SKILL.md — the file the Claude Code
runtime actually loads — same as the trigger canary. The wrapper body is a
verbatim regen of the source body, so pack and source carry the phrase
identically (verified: 7/1/1 file counts match across both trees).

Roster discipline: every aqg-* skill on disk must be partitioned into exactly
one of SECURITY_BOUNDARY_SIGNATURES (it makes the promise — pin it) or
KNOWN_NO_SECURITY_BOUNDARY (it makes no such promise — say why). A new skill that
is in neither fails test_roster_is_partitioned, forcing a deliberate call rather
than silent non-coverage. Same anti-"forgettable touchpoint" discipline as the
trigger canary's test_keyword_map_covers_skill_roster.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Resolve from this file: tests/behavior/<file> -> parents[2] == repo root.
PACK_SKILLS = (
    Path(__file__).resolve().parents[2] / "agent-packs" / "claude-code" / "skills"
)

# skill -> distinctive signature phrase(s) of its body safety-boundary promise,
# each verified present in the live pack SKILL.md body. Dropping the phrase from
# the boundary section turns the canary red.
SECURITY_BOUNDARY_SIGNATURES: dict[str, list[str]] = {
    "aqg-automation-audit": ["remain separate authorization gates"],
    # write-capable (commit) since 2026-07-01: pin the write-scoping invariants
    # (only LOG.md, append-only, no production touch) instead of the old
    # "read-only boundary" phrase.
    "aqg-decision-capture": [
        "Writes ONLY docs/decisions/LOG.md",
        "Does not touch production / secrets / .env / Owner-admin",
    ],
    "aqg-evidence-closeout": ["remain separate authorization gates"],
    "aqg-memory-hygiene": ["remain separate authorization gates"],
    "aqg-project-status": ["remain separate authorization gates"],
    "aqg-re-anchor": ["remain separate authorization gates"],
    "aqg-security-review": ["remain separate authorization gates"],
    "aqg-test-quality-review": ["remain separate authorization gates"],
    # phrasing variants of the same promise:
    "aqg-startup-preflight": ["remain separate checks"],
    "aqg-session-handoff": ["remain separate authorization gates"],
    # read-only validator: its promise is a "do not touch" disclaimer, not an
    # authorization-gate sentence.
    "aqg-skill-validator": ["Forbidden touch"],
}

# skill -> reason it carries no "separate authorization gates" body promise.
# These are NOT unprotected gaps: each names the skill's actual safety model so
# the exemption is a deliberate, reviewable decision.
KNOWN_NO_SECURITY_BOUNDARY: dict[str, str] = {
    "aqg-audit-adjudication": (
        "safety model is needs-user-decision escalation (Owner-only / "
        "destructive / production routed UP to the human), guarded by the "
        "trigger canary's 'needs-user-decision' keyword — there is no "
        "self-disclaiming 'separate authorization gates' promise to pin"
    ),
    "aqg-code-construction": (
        "safety is enforced in code, not prose: the ledger secret-pattern scan "
        "(exit-2) and read-only checker are guarded by the skill's self_test.py, "
        "so there is no authorization-gate sentence in the SKILL.md body"
    ),
    "aqg-multi-review": (
        "pure 5-dimension review-prompt generator; touches no system resource "
        "and makes no production/secret promise to protect"
    ),
    "aqg-phase-transition": (
        "pure phase-signal emitter (PLAN_DONE / IMPL_DONE / TESTS_WRITTEN); "
        "touches no system resource and makes no production/secret promise"
    ),
    "aqg-systematic-debugging": (
        "debugging-flow skill; it names production write/deploy/restart/rollback "
        "as a debugging-checklist caution, not as a self-disclaiming "
        "authorization-gate promise"
    ),
}


def _body(skill_name: str) -> str:
    """Return the full pack SKILL.md text (fail loud if absent)."""
    skill_md = PACK_SKILLS / skill_name / "SKILL.md"
    assert skill_md.is_file(), f"{skill_name}: pack SKILL.md missing at {skill_md}"
    return skill_md.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "skill_name, signatures", sorted(SECURITY_BOUNDARY_SIGNATURES.items())
)
def test_skill_retains_security_boundary_signature(
    skill_name: str, signatures: list[str]
):
    """The skill's body still contains its safety-boundary signature phrase."""
    body = _body(skill_name)
    missing = [sig for sig in signatures if sig not in body]
    assert not missing, (
        f"{skill_name}: SKILL.md body no longer contains safety-boundary "
        f"signature(s): {missing}\n"
        f"  This phrase is the skill's promise that it does NOT itself reach "
        f"production / secrets / Owner-admin.\n"
        f"  Dropping it removes a load-bearing safety invariant that the H2-only "
        f"validator check cannot catch.\n"
        f"  Restore the phrase, or — if the boundary was deliberately reworded — "
        f"update SECURITY_BOUNDARY_SIGNATURES with the new signature and reasoning."
    )


def test_roster_is_partitioned():
    """Self-computing: every aqg-* skill is in exactly one of the two maps.

    Derives the roster from disk (no hardcoded count), so a new skill that is
    neither pinned nor explicitly exempted fails here instead of silently going
    uncovered. Mirrors test_keyword_map_covers_skill_roster in the trigger canary.
    """
    on_disk = {
        p.name
        for p in PACK_SKILLS.iterdir()
        if p.is_dir() and p.name.startswith("aqg-")
    }
    pinned = set(SECURITY_BOUNDARY_SIGNATURES)
    exempt = set(KNOWN_NO_SECURITY_BOUNDARY)

    overlap = pinned & exempt
    assert not overlap, (
        f"skills both pinned AND exempted (pick one): {sorted(overlap)}"
    )

    covered = pinned | exempt
    assert covered == on_disk, (
        "security-boundary canary roster out of sync with skill roster:\n"
        f"  on disk but neither pinned nor exempted: {sorted(on_disk - covered)}\n"
        f"  in a map but no skill on disk: {sorted(covered - on_disk)}\n"
        "  Add each new skill to SECURITY_BOUNDARY_SIGNATURES (pin its boundary "
        "phrase) or KNOWN_NO_SECURITY_BOUNDARY (explain why it has none)."
    )


def test_no_empty_signature_lists():
    """A skill mapped with an empty list would pass the parametrized test
    vacuously (empty 'missing'), leaving it silently uncovered. Each pinned
    skill must carry >=1 real signature phrase."""
    empty = sorted(s for s, sigs in SECURITY_BOUNDARY_SIGNATURES.items() if not sigs)
    assert not empty, (
        f"skills pinned with an empty signature list (no real canary): {empty}\n"
        "  Give each entry >=1 signature phrase, or move it to "
        "KNOWN_NO_SECURITY_BOUNDARY with a reason."
    )


def test_exemptions_carry_a_reason():
    """Every exemption must state the skill's actual safety model — an empty or
    whitespace reason is not a deliberate decision."""
    blank = sorted(s for s, why in KNOWN_NO_SECURITY_BOUNDARY.items() if not why.strip())
    assert not blank, (
        f"exempted skills with a blank reason: {blank}\n"
        "  State why the skill carries no 'separate authorization gates' promise."
    )
