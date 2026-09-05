"""The Gate A clause each carrier EMITS must be one sentence, not two dialects.

`test_audit_gate_sensitivity_list_does_not_drift_between_adapters` already
asserts that every policy category token survives into what each adapter emits.
Its own docstring names the hole it leaves: it checks the CATEGORY TOKENS only,
so "a wording difference between the two adapters would still pass". One had
already opened -- the shell hook emitted `size. The sensitivity list ...` while
the Cursor adapter emitted `size; the sensitivity list ...` -- and the suite was
green throughout. A drifting sentence is how two hosts end up giving an agent
subtly different rules while every test says they agree.

What is compared, and what is deliberately not:

- COMPARED: all three policy clauses -- the skip clause, the Gate A sensitivity
  clause, and the frequency guard -- after collapsing whitespace. They are policy
  content, so they may not differ by host.
- NOT COMPARED: the scaffolding around them, and the pointer line. The shell hook
  emits indented bullets over several lines; Cursor emits one pipe-joined line and
  names the edited file. The pointer emits a host-RESOLVED absolute path, so the
  two legitimately name different checkouts on one machine.
  `docs/policies/audit-trigger.md` ("What agent packs may and may not carry")
  permits exactly that: "Transport differences between hosts are legitimate and
  should be preserved ... Policy differences are not." Asserting raw equality
  over the whole emission would force one host's formatting onto the other and
  contradict the policy this file exists to serve.

The negative fixture at the bottom is here because a drift guard that cannot
fail is indistinguishable from one that passes.

Residual risk, stated rather than implied (aud_2PXuFzj3CUnZMS21 opus-f7): this
asserts the carriers agree with EACH OTHER, not that either agrees with the
policy's prose. Only the category tokens have a machine-readable source
(`gate-a-tokens`); the sentence around them does not, and the policy says so
itself -- "Reminder wording at hook time | must be kept consistent with this file
(**manual today** -- no automated derivation exists)". A coordinated edit to both
carriers, or a policy edit with no carrier edit, passes everything here. Closing
that would mean giving the policy a marker carrying the canonical sentence and
asserting both carriers against it; that adds a third marker to the authority
file and is an owner decision, not a test-side one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHELL_HOOK = (
    REPO / "agent-packs" / "claude-code" / "hooks"
    / "posttooluse_code_construction_reminder.sh"
)
CURSOR_ADAPTER = REPO / "scripts" / "cursor_aqg_hook.py"

sys.path.insert(0, str(REPO / "scripts"))
from aqg_policy_markers import (  # noqa: E402
    depth_by_stakes,
    gate_a_tokens,
    missing_gate_a_tokens,
    reminder_clause,
    reminder_clauses,
)

GATE_HEADING = "[aqg audit-before-commit gate]"

# Anchor width: enough words to be unambiguous inside one emission, few enough
# that rewording the middle of a clause still lets the span be FOUND and then
# fail on comparison, which is a far clearer failure than "no clause emitted".
_ANCHOR_WORDS = 3


def _extractor(canonical: str) -> re.Pattern[str]:
    """Build the span regex from the policy's own sentence, not from a copy.

    The first version hardcoded `SKIP if trivial\b.*?not the reflex\.` and two
    siblings, which left the opening and closing words of every clause
    triple-maintained inside a file whose claim is that the policy is the source
    (aud_j7MGnqAIqMOD3h7F opus-f3). Anchors now come from the canonical text, so
    rewording a clause in the policy moves the anchors with it.
    """
    words = canonical.split()
    assert len(words) > 2 * _ANCHOR_WORDS, f"clause too short to anchor: {canonical!r}"
    head = re.escape(" ".join(words[:_ANCHOR_WORDS]))
    tail = re.escape(" ".join(words[-_ANCHOR_WORDS:]))
    return re.compile(rf"({head}.*?{tail})", re.DOTALL)


def _policy_clauses() -> tuple[tuple[str, str, re.Pattern[str]], ...]:
    """(name, canonical sentence, extractor) for every clause the policy publishes."""
    return tuple(
        (name, canonical, _extractor(canonical))
        for name, canonical in reminder_clauses(REPO).items()
    )

# The pointer line is NOT compared, and the reason is not "it drifted": it emits
# a host-RESOLVED absolute path, so the two carriers legitimately name different
# checkouts on the same machine. That it resolves at all, and never degrades to
# the UNRESOLVED marker in a normal checkout, is asserted by
# test_audit_gate_pointer_resolves_and_is_not_the_unresolved_marker.

# `fast` is a depth name the policy lists in prose ("Depth names") but carries in
# no marker, so it is the one entry that cannot be derived. Everything else comes
# from the policy itself, below.
_UNMARKERED_DEPTH_NAMES = ("fast",)


def _unsourced_depths(repo: Path) -> tuple[str, ...]:
    """Depths a carrier may not name, derived from the policy, not typed here.

    An earlier version hardcoded `("standard", "fast")`, which an auditor called
    a fourth copy of the depth vocabulary in a file whose whole subject is not
    keeping copies (aud_2PXuFzj3CUnZMS21 opus-f3). The `depth-by-stakes` marker
    IS the vocabulary; Gate A routes to the high-stakes depth, so the carriers
    repeat exactly that one as policy content and every other depth in the
    vocabulary is a value no carrier is entitled to choose.

    Case matters and is load-bearing: the carriers write the ladder keyword as
    uppercase `SKIP` ("SKIP if trivial", "outranks SKIP") while the DEPTH is
    lowercase `skip`. The checks below are case-sensitive so the keyword is not
    mistaken for a depth. Verified against both carriers: they emit `SKIP` twice
    and lowercase `deep` once, and no lowercase `skip` at all.
    """
    depths = depth_by_stakes(repo)
    gate_a_depth = depths["high"]
    derived = tuple(d for d in dict.fromkeys(depths.values()) if d != gate_a_depth)
    covered = set(derived) | {gate_a_depth}
    assert covered >= set(depths.values()), (
        "a depth in the policy's depth-by-stakes marker is neither the Gate A "
        f"depth nor blocked: {set(depths.values()) - covered}. Classify it rather "
        "than letting a carrier name it unchallenged"
    )
    return derived + _UNMARKERED_DEPTH_NAMES


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _emitted_shell_gate() -> str:
    proc = subprocess.run(
        ["bash", str(SHELL_HOOK)],
        input=json.dumps({"tool_input": {"file_path": "/repo/src/lib.py"}}),
        text=True,
        capture_output=True,
        env={**os.environ, "AQG_ROOT": str(REPO)},
        check=False,
    )
    assert GATE_HEADING in proc.stderr, proc.stderr
    return proc.stderr.split(GATE_HEADING, 1)[-1]


def _emitted_cursor_gate() -> str:
    proc = subprocess.run(
        [sys.executable, str(CURSOR_ADAPTER), "postToolUse", "--aqg-root", str(REPO)],
        cwd=REPO,
        input=json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": str(REPO / "scripts" / "service.py"),
                    "content": "def add(a, b):\n    return a + b\n",
                },
                "cwd": str(REPO),
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    context = json.loads(proc.stdout)["additional_context"]
    return context.split("AQG audit-before-commit gate:", 1)[-1]


def _clause(emitted: str, carrier: str, name: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(_collapse(emitted))
    assert match, (
        f"{carrier} emits no complete {name}:\n{_collapse(emitted)[:400]}"
    )
    return match.group(1)


def test_every_carrier_emits_the_policys_own_reminder_clauses():
    """Each carrier is compared to the POLICY, not to the other carrier.

    Carrier-vs-carrier was the first version, and it is strictly weaker: a
    coordinated edit to both, or a policy edit with no carrier edit, passed
    (aud_2PXuFzj3CUnZMS21 opus-f7). The policy now publishes each reminder
    sentence in a marker, so the comparison has an authority instead of a
    quorum. Cross-carrier agreement still follows -- both equal the same string.
    """
    clauses = _policy_clauses()
    assert clauses, "the policy publishes no reminder clauses to compare against"

    for carrier, emitted in (
        ("shell hook", _emitted_shell_gate()),
        ("cursor adapter", _emitted_cursor_gate()),
    ):
        for name, canonical, pattern in clauses:
            actual = _clause(emitted, carrier, name, pattern)
            assert actual == canonical, (
                f"{carrier} does not emit the policy's {name}; formatting may "
                "differ between hosts, policy text may not:\n"
                f"  policy : {canonical}\n"
                f"  emitted: {actual}"
            )


# The category run inside the clause marker: everything the EXCEPT introduces, up
# to the colon that starts the consequence.
CLAUSE_CATEGORY_RUN = re.compile(r"EXCEPT (.+?): deep regardless")


def test_the_two_gate_a_markers_agree_in_both_directions():
    """`gate-a-tokens` and `gate-a-clause` name the same categories, in order.

    The sentence necessarily repeats the list, so the policy holds it twice. A
    one-directional check -- every token appears somewhere in the clause -- was
    the first version, and three auditors called the missing direction
    (aud_j7MGnqAIqMOD3h7F): a category present in the CLAUSE but absent from the
    tokens marker would ship to every carrier while every guard stayed green.

    Position and count are compared, not just membership. `startswith` rather
    than equality because a clause entry may elaborate its token -- the token is
    `trust-boundary` and the carriers emit `trust-boundary input` -- which is
    also why the list is not templated from the tokens marker by construction.
    """
    clause = reminder_clause(REPO, "gate-a-clause")
    tokens = gate_a_tokens(REPO)

    # Direction 1, kept: no token missing from the clause.
    absent = missing_gate_a_tokens(clause, REPO)
    assert not absent, (
        "the policy's gate-a-clause marker omits categories its own "
        f"gate-a-tokens marker lists: {absent}"
    )

    # Direction 2, added: no entry in the clause that is not a token.
    run = CLAUSE_CATEGORY_RUN.search(clause)
    assert run, (
        "the gate-a-clause marker no longer has an extractable category run "
        f"(EXCEPT ... : deep regardless): {clause[:160]}"
    )
    entries = [e.strip() for e in run.group(1).split(",") if e.strip()]

    assert len(entries) == len(tokens), (
        "the gate-a-clause marker names a different number of categories than "
        f"gate-a-tokens lists: clause has {len(entries)} {entries}, "
        f"tokens has {len(tokens)} {list(tokens)}"
    )
    mismatched = [
        f"position {i}: clause says {entry!r}, tokens says {token!r}"
        for i, (entry, token) in enumerate(zip(entries, tokens))
        if not entry.startswith(token)
    ]
    assert not mismatched, (
        "the two Gate A markers disagree on categories or their order:\n  "
        + "\n  ".join(mismatched)
    )


def test_carriers_do_not_introduce_a_depth_the_policy_did_not():
    """`deep` comes from Gate A. Any other depth word is a carrier deciding.

    This is the same defect class that had `aqg-security-review` prescribing
    `mode=standard`, checked on the hook surface rather than the skill surface.
    """
    offenders = []

    # The policy's own markers are checked first. They are the source the
    # carriers now copy from, so a stray depth there would propagate to every
    # host and the carrier checks below would all agree with it.
    clauses = _policy_clauses()
    for name, canonical, _pattern in clauses:
        for depth in _unsourced_depths(REPO):
            if re.search(rf"(?<![\w-]){re.escape(depth)}(?![\w-])", canonical):
                offenders.append(f"policy clause: {name} names depth {depth!r}")

    for carrier, emitted in (
        ("shell hook", _emitted_shell_gate()),
        ("cursor adapter", _emitted_cursor_gate()),
    ):
        # Scan the policy clauses, NOT the raw emission. The emission carries two
        # absolute filesystem paths — the resolved policy pointer, and (on Cursor)
        # the edited file — and `/` is a word boundary, so a checkout under
        # `/mnt/fast/...` or a directory literally named `standard` would report a
        # carrier "naming a depth" it never named (aud_2PXuFzj3CUnZMS21 opus-f6).
        for name, _canonical, pattern in clauses:
            clause = _clause(emitted, carrier, name, pattern)
            for depth in _unsourced_depths(REPO):
                if re.search(rf"(?<![\w-]){re.escape(depth)}(?![\w-])", clause):
                    offenders.append(f"{carrier}: {name} names depth {depth!r}")

    assert not offenders, (
        "a carrier names a depth the policy did not put in the Gate A clause; "
        "depth belongs to docs/policies/audit-trigger.md alone:\n  "
        + "\n  ".join(offenders)
    )


def test_the_token_guard_actually_fails_on_a_drifted_clause():
    """The negative fixture: prove the category check can fail.

    `docs/policies/audit-trigger.md` says `cross-repo`. An early draft of this
    work wrote "spanning repositories" instead -- same meaning to a human, a
    different token to the guard. Feeding that draft through the same assertion
    the real carriers face proves the assertion has teeth; without this, a guard
    whose regex quietly stopped matching would look identical to a green one.
    """
    tokens = gate_a_tokens(REPO)
    assert "cross-repo" in tokens, (
        "this fixture is anchored to the `cross-repo` category; the policy "
        "renamed it, so re-anchor the fixture rather than deleting the test"
    )

    # Comment lines are stripped BY CODE, not by asking the fixture's author
    # nicely. The first draft's header named the very token it was supposed to
    # omit, the check found it there, and the fixture passed while proving
    # nothing; a comment must not be able to answer on the clause's behalf
    # (aud_2PXuFzj3CUnZMS21 opus-f5).
    fixture = (
        REPO / "tests" / "behavior" / "fixtures" / "gate_a_clause_drifted.txt"
    ).read_text(encoding="utf-8")
    drifted = _collapse(
        "\n".join(
            line for line in fixture.splitlines() if not line.lstrip().startswith("#")
        )
    )
    assert drifted.startswith("EXCEPT"), (
        "comment stripping left something other than the clause body; the fixture "
        f"reduced to: {drifted[:120]!r}"
    )

    # `missing_gate_a_tokens` is the SHIPPING check — the same function
    # test_audit_gate_sensitivity_list_does_not_drift_between_adapters runs
    # against the live carriers. A fixture that re-implemented it would only
    # prove that some check has teeth (aud_2PXuFzj3CUnZMS21 opus-f2).
    missing = missing_gate_a_tokens(drifted, REPO)
    assert missing == ["cross-repo"], (
        "the drifted fixture must fail on exactly the renamed category; "
        f"got {missing}. If this changed, the guard's sensitivity changed with it"
    )
    gate_a = next(c for c in _policy_clauses() if c[0] == "gate-a-clause")
    assert not missing_gate_a_tokens(
        _clause(_emitted_shell_gate(), "shell hook", gate_a[0], gate_a[2]), REPO
    ), (
        "the same check must pass on the real carrier — otherwise the fixture "
        "proves nothing about the guard's behaviour on live text"
    )
