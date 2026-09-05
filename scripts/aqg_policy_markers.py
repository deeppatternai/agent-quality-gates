#!/usr/bin/env python3
"""The one parser for the machine-readable markers in the audit-trigger policy.

`docs/policies/audit-trigger.md` carries HTML comments that exist to be read by
code rather than by people:

    <!-- gate-a-tokens: auth, permissions, crypto, ... -->
    <!-- depth-by-stakes: trivial=skip, moderate=standard, high=deep -->

They are the mechanism that lets a guard test compare a carrier against the
policy instead of against a hand-maintained copy — which is the whole point,
because a hand-maintained copy is a second authority wearing a test's clothes.

Three call sites had each written the same regex: the cross-adapter Gate A drift
test and two depth tests. Three copies of a parser for a single-source marker is
the same defect one level up, so the regex lives here now and they import it.

Fail-closed on purpose: a missing marker raises rather than returning empty. A
parser that quietly yields nothing turns every assertion built on it into a
vacuous pass, which is exactly how a drift guard stops guarding.

Import note: `skills/aqg-phase-transition/tests/` reaches this module through the
repo root, the same way it already reaches the policy file itself. Both break
together outside a full checkout, which is the intended coupling — these are
repo-level guards, not part of the installed skill.
"""

from __future__ import annotations

import re
from pathlib import Path

POLICY_RELPATH = Path("docs") / "policies" / "audit-trigger.md"


def policy_path(repo: Path) -> Path:
    return Path(repo) / POLICY_RELPATH


def read_policy(repo: Path) -> str:
    return policy_path(repo).read_text(encoding="utf-8")


def marker_body(policy_text: str, marker: str) -> str:
    """The raw text after `<!-- <marker>:` and before `-->`."""
    match = re.search(
        rf"<!--\s*{re.escape(marker)}:\s*(.+?)\s*-->", policy_text, re.DOTALL
    )
    if not match:
        raise ValueError(
            f"docs/policies/audit-trigger.md is missing its {marker!r} marker; "
            "the guards that read it cannot run against a copy"
        )
    return match.group(1)


def marker_values(policy_text: str, marker: str) -> tuple[str, ...]:
    """A comma-separated marker as an ordered tuple, e.g. gate-a-tokens."""
    values = tuple(v.strip() for v in marker_body(policy_text, marker).split(",") if v.strip())
    if not values:
        raise ValueError(f"{marker!r} marker is present but empty")
    return values


def marker_mapping(policy_text: str, marker: str) -> dict[str, str]:
    """A comma-separated `k=v` marker as a dict, e.g. depth-by-stakes."""
    mapping = {}
    for item in marker_values(policy_text, marker):
        if "=" not in item:
            raise ValueError(f"{marker!r} entry {item!r} is not a k=v pair")
        key, value = item.split("=", 1)
        mapping[key.strip()] = value.strip()
    return mapping


def gate_a_tokens(repo: Path) -> tuple[str, ...]:
    """The Gate A sensitivity categories, in policy order."""
    return marker_values(read_policy(repo), "gate-a-tokens")


def depth_by_stakes(repo: Path) -> dict[str, str]:
    """The single depth mapping every caller uses: stakes -> depth."""
    return marker_mapping(read_policy(repo), "depth-by-stakes")


def missing_gate_a_tokens(text: str, repo: Path) -> list[str]:
    """Which Gate A categories are absent from `text`, in policy order.

    Token-bounded, not substring: a plain `in` reported success for "auth" while
    the emitted text had dropped the category, because the clause "the
    sensitivity list ... is authoritative" contains those four letters.

    This lives here so the drift guard and the negative fixture that proves the
    guard can fail run the SAME code. A fixture that re-implements the check only
    proves that some check has teeth, not that the shipping one does.
    """
    return [
        token
        for token in gate_a_tokens(repo)
        if not re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text)
    ]


# The reminder sentences a host adapter must emit verbatim, in the order they
# appear in an emitted gate. Adding a clause here without adding its marker to
# the policy fails loudly, which is the intended direction of that dependency.
REMINDER_CLAUSE_MARKERS = ("skip-clause", "gate-a-clause", "frequency-clause")


# The clause list is VISIBLE markdown fenced by two content-free comments, not
# text hidden inside a comment. Three auditors on aud_j7MGnqAIqMOD3h7F called the
# comment version a hazard, one as blocking: an adapter is required to reproduce
# these sentences, so a reader of the rendered policy must be able to see what
# their tools are telling them. The fence delimits; the page carries.
_CLAUSE_REGION = re.compile(
    r"<!--\s*reminder-clauses:\s*begin\s*-->(.*?)<!--\s*reminder-clauses:\s*end\s*-->",
    re.DOTALL,
)
_CLAUSE_ROW = re.compile(r"^-\s+\*\*([a-z0-9-]+)\*\*\s+—\s+(.+?)\s*$", re.MULTILINE)


def reminder_clauses(repo: Path) -> dict[str, str]:
    """Every reminder clause the policy publishes, name -> sentence.

    Before this list existed the guards could only assert that the two host
    carriers agreed with EACH OTHER. That is strictly weaker than agreeing with
    the policy: a coordinated edit to both carriers passed, and so did a policy
    edit with no carrier edit. The policy said as much about itself -- "Reminder
    wording at hook time | must be kept consistent with this file (manual today
    -- no automated derivation exists)". This list is that derivation.

    Fail-closed at three separate points, because a clause reader that returns
    less than it should turns every assertion built on it into a vacuous pass:
    a missing fence, a row that does not parse, and a name in
    REMINDER_CLAUSE_MARKERS with no row all raise.

    Sentences are whitespace-collapsed on read so the carriers stay free to wrap
    wherever their own format needs.
    """
    region = _CLAUSE_REGION.search(read_policy(repo))
    if not region:
        raise ValueError(
            "docs/policies/audit-trigger.md is missing its reminder-clauses "
            "begin/end fence; the guards that read it cannot run against a copy"
        )

    found = {name: " ".join(text.split()) for name, text in _CLAUSE_ROW.findall(region.group(1))}

    rows = [line for line in region.group(1).splitlines() if line.strip().startswith("-")]
    if len(rows) != len(found):
        raise ValueError(
            f"the reminder-clauses list has {len(rows)} bullet(s) but only "
            f"{len(found)} parsed as `- **name** — text`; fix the row rather than "
            "letting a clause drop out of the comparison"
        )

    # Both directions. The second one is the one that is easy to forget: a clause
    # the policy publishes but no carrier is required to emit is a rule with no
    # enforcement, and both sides of a code-side-only comparison would agree it
    # does not exist (aud_j7MGnqAIqMOD3h7F opus-f4).
    missing = [m for m in REMINDER_CLAUSE_MARKERS if m not in found]
    if missing:
        raise ValueError(
            f"the reminder-clauses list does not publish {missing}; every name in "
            "REMINDER_CLAUSE_MARKERS must have a row"
        )
    unclaimed = [name for name in found if name not in REMINDER_CLAUSE_MARKERS]
    if unclaimed:
        raise ValueError(
            f"the policy publishes reminder clause(s) {unclaimed} that no carrier is "
            "required to emit; add them to REMINDER_CLAUSE_MARKERS and to the guard, "
            "or remove them from the policy"
        )
    return {marker: found[marker] for marker in REMINDER_CLAUSE_MARKERS}


def reminder_clause(repo: Path, name: str) -> str:
    """One reminder clause by name."""
    return reminder_clauses(repo)[name]
