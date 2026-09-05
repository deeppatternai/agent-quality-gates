"""Depth must come from the policy, everywhere it is named.

`docs/policies/audit-trigger.md` is the single depth authority (Owner ruling
2026-08-11). Two guards already hold parts of that line: the router is compared
to the policy's `depth-by-stakes` marker by
`test_router_depth_matches_the_policy_single_source`, and the phase-transition
SKILL.md copy of the table by `test_skill_md_depth_table_matches_the_policy`.

Neither of them can see the two ways the authority actually leaked:

1. A skill tells the caller to run `/audit` and prescribes a depth itself,
   never naming the policy. `aqg-security-review` carried `mode=standard` while
   its own trigger list IS the policy's Gate A sensitivity list (which routes to
   `deep`, "and it wins"). `aqg-test-quality-review` and `aqg-multi-review` named
   no depth at all, so their `/audit` calls fell to the tool default with the
   policy never consulted. A depth stated — or silently defaulted — without a
   pointer to the policy is a second authority regardless of which value it lands
   on, so this file asserts the POINTER, not a wording.
2. A phase-transition script explains itself in a framing its own doctor judges
   retired. `aqg_doctor.RETIRED_RULES_MARKERS` fails an installed rules block for
   saying "Phase x Stakes", because depth is keyed on stakes alone; the router
   and emit docstrings said it about themselves. The marker is imported from the
   doctor rather than retyped here, so this test cannot become a fourth copy of
   the thing it is policing.

Scope: both skill trees. `skills/<X>/SKILL.md` is the hand-edited source and
`agent-packs/claude-code/skills/<X>/SKILL.md` is generated from it by
`aqg_skill_gen.py regen`, but the wrapper is what actually installs onto a
Claude Code machine, so it is scanned directly rather than trusted to match.

Four assertions, because the first one alone is not enough:
- the POINTER is present wherever /audit is invoked;
- no /audit line prescribes a single literal depth, so citing the policy and
  hard-coding a value anyway still fails;
- phase-transition scripts do not carry the retired framing;
- the detector itself matches something, so none of the above can pass vacuously.

NOT covered, stated rather than implied: whether a skill reads the policy
CORRECTLY. It can cite the path, name no depth, and still paraphrase the mapping
wrong -- that stays with the two marker-comparison tests above. Nor does this
cover `description:` frontmatter as a surface of its own: aqg-multi-review's
description still carries a `mode=<phase-transition recommended>` placeholder,
which is a placeholder rather than a literal depth and so is legal here by
design; rewriting descriptions (and re-syncing their skill.template.json
sidecars) is a separate change.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
POLICY_PATH_REF = "docs/policies/audit-trigger.md"

sys.path.insert(0, str(REPO / "scripts"))

# Two ways this detector can be wrong, both guarded by
# test_the_invocation_detector_is_not_vacuous below:
#   over-match — prose like `model/audit outputs` and paths like
#     `docs/policies/audit-trigger.md` are not invocations (excluded by the
#     lookbehind), and neither are the sibling SKILLS `/audit-adjudication` /
#     `/audit-brainstorming`, which have their own depth story (excluded by the
#     trailing class; `\b` alone let those through).
#   under-match — a detector that quietly stops matching turns the main test
#     into a vacuous pass, which is why the corpus floor is asserted.
AUDIT_INVOCATION = re.compile(r"(?<!\w)/audit(?![\w-])")

# A single literal depth is a prescription. `mode=fast|standard|deep` (usage
# syntax) and `mode=<phase-transition recommended>` (placeholder) are not, so the
# trailing class excludes the pipe and the angle-bracket forms never match.
LITERAL_DEPTH = re.compile(r"mode=`?(skip|fast|standard|deep)`?(?![|\w-])")

# The depth framing the code dropped, kept as one name so the scan and its
# anchor assertion cannot drift apart.
DEPTH_FRAMING_MARKER = "Phase × Stakes"


def _skill_docs() -> list[Path]:
    return sorted(
        list((REPO / "skills").glob("*/SKILL.md"))
        + list((REPO / "agent-packs").glob("*/skills/*/SKILL.md"))
    )


def test_every_skill_that_invokes_audit_names_the_depth_authority():
    offenders = []
    for doc in _skill_docs():
        text = doc.read_text(encoding="utf-8")
        if not AUDIT_INVOCATION.search(text):
            continue
        if POLICY_PATH_REF in text:
            continue
        first = next(
            line.strip()
            for line in text.splitlines()
            if AUDIT_INVOCATION.search(line)
        )
        offenders.append(f"{doc.relative_to(REPO)}: {first[:110]}")

    assert not offenders, (
        "these skills route the caller to /audit without naming "
        f"{POLICY_PATH_REF}, so each one decides depth on its own:\n  "
        + "\n  ".join(offenders)
    )


def test_phase_transition_scripts_do_not_carry_the_retired_depth_framing():
    """Scans for ONE marker, not the doctor's whole retired list.

    An earlier version iterated every entry in RETIRED_RULES_MARKERS against
    script source. Three auditors independently rejected that (aud_-YQUVbnXnvs4bYyu
    google-f1 / opus-f5 / xai-f2): the doctor's list governs an INSTALLED rules
    block, so a future entry that is perfectly fine to appear in Python source
    would fail this test under a banner about depth framing. Only the
    depth-framing entry is in scope here; the import survives so that retiring
    that entry in the doctor fails loudly instead of silently disarming the scan.
    """
    from aqg_doctor import RETIRED_RULES_MARKERS

    retired = [marker for marker, _why in RETIRED_RULES_MARKERS]
    assert DEPTH_FRAMING_MARKER in retired, (
        f"{DEPTH_FRAMING_MARKER!r} is no longer in aqg_doctor.RETIRED_RULES_MARKERS; "
        "re-derive this test's anchor from whatever replaced it rather than "
        "loosening the assertion"
    )

    scripts = sorted((REPO / "skills" / "aqg-phase-transition" / "scripts").glob("*.py"))
    assert scripts, "no phase-transition scripts found — the glob went stale"

    offenders = [
        f"{script.relative_to(REPO)}:{lineno}: {line.strip()[:110]}"
        for script in scripts
        for lineno, line in enumerate(
            script.read_text(encoding="utf-8").splitlines(), start=1
        )
        if DEPTH_FRAMING_MARKER in line
    ]

    assert not offenders, (
        f"phase-transition scripts describe themselves as {DEPTH_FRAMING_MARKER!r}, "
        "the framing aqg_doctor fails an installed rules block for:\n  "
        + "\n  ".join(offenders)
    )


def test_no_skill_prescribes_a_single_depth_on_an_audit_line():
    """Citing the policy and then hard-coding a depth anyway must still fail.

    The pointer test above is satisfied by the policy path appearing ANYWHERE in
    the file, so `/audit mode=standard as per docs/policies/audit-trigger.md`
    would pass it while re-creating the exact second authority (aud_-YQUVbnXnvs4bYyu
    google-f3, opus-f1). This closes that hole from the other side: no `/audit`
    line may carry a single literal depth.

    Two shapes are deliberately NOT prescriptions and stay legal: the
    pipe-enumerated usage syntax `mode=fast|standard|deep`, which shows the
    argument's form, and a `mode=<...>` placeholder.
    """
    # The depth-authority skill itself restates its own safety floor (high never
    # drops below deep). That is the policy's own rule, not a competing copy, and
    # its value is compared against the policy's marker by
    # test_skill_md_depth_table_matches_the_policy — a check no other skill has.
    EXEMPT_SKILL = "aqg-phase-transition"

    offenders = []
    for doc in _skill_docs():
        if doc.parent.name == EXEMPT_SKILL:
            continue
        for lineno, line in enumerate(
            doc.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if AUDIT_INVOCATION.search(line) and LITERAL_DEPTH.search(line):
                offenders.append(f"{doc.relative_to(REPO)}:{lineno}: {line.strip()[:130]}")

    assert not offenders, (
        "these lines invoke /audit with a depth of their own; depth belongs to "
        f"{POLICY_PATH_REF} alone:\n  " + "\n  ".join(offenders)
    )


def test_the_invocation_detector_is_not_vacuous():
    """A scan that matches nothing passes every assertion built on it.

    xai flagged exactly this on the first audit of this file (aud_-YQUVbnXnvs4bYyu):
    the main test iterates, skips every doc whose text does not match, and asserts
    an empty offender list — so a detector broken by a wording change reports
    green rather than red. Both halves are pinned here: the regex against a fixed
    positive/negative pair that does not depend on repo contents, and the live
    corpus against a floor.
    """
    for invocation in (
        "run `/audit` mode=standard",
        "# for each dim: /audit focus=<dim prompt>",
        "/audit mode=deep artifact=...",
    ):
        assert AUDIT_INVOCATION.search(invocation), invocation

    for not_an_invocation in (
        "before proposing a fix when model/audit outputs disagree",
        "recorded under docs/audit-evidence/2026-05-02-ci-adapter-review-a1.md",
        "depth per `docs/policies/audit-trigger.md`",
        "chain to `/audit-adjudication` with the audit_id",
        "for a hypothesis, use /audit-brainstorming instead",
    ):
        assert not AUDIT_INVOCATION.search(not_an_invocation), not_an_invocation

    scanned = [
        doc
        for doc in _skill_docs()
        if AUDIT_INVOCATION.search(doc.read_text(encoding="utf-8"))
    ]
    # Both trees carry aqg-code-construction and aqg-phase-transition, which route
    # to /audit as their whole point, plus the three this change fixed: 10 today.
    # The floor is deliberately well below that — it catches a detector that went
    # blind, not a skill that legitimately stopped naming /audit.
    assert len(scanned) >= 4, (
        f"only {len(scanned)} skill docs matched the /audit detector across "
        f"{len(_skill_docs())} scanned; the main assertions are passing "
        "vacuously — fix AUDIT_INVOCATION, do not lower this floor"
    )
