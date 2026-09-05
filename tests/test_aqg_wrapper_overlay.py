"""Unit tests for scripts/_aqg_wrapper_overlay.py — the pure wrapper-overlay core.

PR-1 of the SKILL.md-from-source lightweight source-overlay design.

`build_wrapper(source_text, sidecar)` returns the source text with each
`host_overrides["claude"]` anchor→replacement applied by str.replace, fail-closed
on ambiguous / stale / frontmatter / malformed anchors. `is_managed_skill(sidecar)`
is the single shared migration predicate (`wrapper_generated is True`, strict).

These tests are pure (no filesystem I/O); the generator owns reads/writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from _aqg_wrapper_overlay import build_wrapper, is_managed_skill  # noqa: E402


# A small but realistic source: frontmatter + body with a fenced invoke block.
_SOURCE = (
    "---\n"
    "name: aqg-demo\n"
    "description: A demo skill for the overlay core. Use when testing.\n"
    "---\n"
    "\n"
    "# AQG Demo\n"
    "\n"
    "## Workflow\n"
    "\n"
    "Run the helper:\n"
    "\n"
    "```bash\n"
    'python3 "$aqg_root/skills/aqg-demo/scripts/run.py"\n'
    "```\n"
    "\n"
    "## Boundary Rules\n"
    "\n"
    "- Read-only.\n"
)


def _sidecar(overrides, *, managed=True):
    """Build a sidecar dict with the given claude override list."""
    s = {"name": "aqg-demo"}
    if managed:
        s["wrapper_generated"] = True
    if overrides is not None:
        s["host_overrides"] = {"claude": overrides}
    return s


# ===== Slice 1: basic single-anchor swap =====


def test_single_anchor_swap():
    anchor = 'python3 "$aqg_root/skills/aqg-demo/scripts/run.py"'
    replacement = (
        'script="$aqg_root/skills/aqg-demo/scripts/run.py"\n'
        'python3 "$script" --repo "${CLAUDE_PROJECT_DIR:?CLAUDE_PROJECT_DIR is required}"'
    )
    sidecar = _sidecar([{"anchor": anchor, "replacement": replacement}])
    out = build_wrapper(_SOURCE, sidecar)
    assert replacement in out
    assert anchor not in out
    # everything else is preserved verbatim
    assert "## Boundary Rules" in out
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


# ===== Slice 2: multiple anchors applied in list order =====


def test_multiple_anchors_in_order():
    src = "alpha\nbeta\ngamma\n"
    sidecar = _sidecar([
        {"anchor": "alpha", "replacement": "A1"},
        {"anchor": "gamma", "replacement": "G3"},
    ], managed=True)
    out = build_wrapper(src, sidecar)
    assert out == "A1\nbeta\nG3\n"


def test_multiple_anchors_replacement_text_is_inert():
    """A replacement that itself contains another anchor's text is NOT re-scanned.

    Regression for audit 9c540100 f2 / gemini-f1: replacement must be span-based
    (slice the ORIGINAL source around validated spans), not sequential
    str.replace on the mutated output. Replace SOURCE 'one' with 'X-three-X' and
    SOURCE 'three' with 'Z'. Each anchor matches one span in the ORIGINAL source
    ('one' at 0-3, 'three' at 8-13), so assembly is
    source[:0] + 'X-three-X' + source[3:8](' two ') + 'Z' + source[13:]('\\n').
    The 'three' injected by the first replacement stays inert (it is not a source
    span).
    """
    src = "one two three\n"
    sidecar = _sidecar([
        {"anchor": "one", "replacement": "X-three-X"},
        {"anchor": "three", "replacement": "Z"},
    ])
    out = build_wrapper(src, sidecar)
    assert out == "X-three-X two Z\n"


# ===== Slice 3: replacement with ${VAR} + backticks survives verbatim =====


def test_replacement_with_var_and_backticks_survives():
    src = "---\nname: x\ndescription: d\n---\n\nrun PLACEHOLDER here\n"
    replacement = 'python3 `cmd` --repo "${CLAUDE_PROJECT_DIR:?required}" $aqg_root'
    sidecar = _sidecar([{"anchor": "PLACEHOLDER", "replacement": replacement}])
    out = build_wrapper(src, sidecar)
    # The replacement is inserted byte-for-byte; no shell expansion, no escaping.
    assert replacement in out
    assert "${CLAUDE_PROJECT_DIR:?required}" in out
    assert "`cmd`" in out


# ===== Slice 4: zero-override managed skill → verbatim copy =====


def test_zero_override_managed_is_verbatim_copy():
    sidecar = _sidecar([], managed=True)  # managed, empty claude list
    out = build_wrapper(_SOURCE, sidecar)
    assert out == _SOURCE  # _SOURCE already ends in exactly one '\n'


def test_zero_override_absent_host_overrides_is_verbatim_copy():
    sidecar = _sidecar(None, managed=True)  # managed, NO host_overrides key
    out = build_wrapper(_SOURCE, sidecar)
    assert out == _SOURCE


def test_no_trailing_newline_source_fails_closed():
    # audit 9c540100 f4: assert (not silently normalize) the one-newline
    # convention. A source with NO trailing newline violates it → raise, so the
    # convention is fixed at the source rather than papered over by the generator.
    src = "---\nname: x\ndescription: d\n---\n\nbody no newline"
    with pytest.raises(ValueError, match="exactly one newline"):
        build_wrapper(src, _sidecar([], managed=True))


def test_multi_trailing_newline_source_fails_closed():
    # A 2+-newline EOF also violates the convention → raise (no silent collapse).
    src = "---\nname: x\ndescription: d\n---\n\nbody\n\n\n"
    with pytest.raises(ValueError, match="exactly one newline"):
        build_wrapper(src, _sidecar([], managed=True))


# ===== Slice 5: fail-closed battery =====


def test_fail_closed_anchor_count_zero():
    sidecar = _sidecar([{"anchor": "NOT-IN-SOURCE", "replacement": "x"}])
    with pytest.raises(ValueError, match="NOT-IN-SOURCE"):
        build_wrapper(_SOURCE, sidecar)


def test_fail_closed_anchor_count_zero_message_says_stale():
    sidecar = _sidecar([{"anchor": "NOT-IN-SOURCE", "replacement": "x"}])
    with pytest.raises(ValueError, match="exactly"):
        build_wrapper(_SOURCE, sidecar)


def test_fail_closed_anchor_count_two():
    src = "dup\nmiddle\ndup\n"
    sidecar = _sidecar([{"anchor": "dup", "replacement": "x"}])
    with pytest.raises(ValueError, match="2 time"):
        build_wrapper(src, sidecar)


def test_fail_closed_anchor_substring_of_another():
    # 'run.py' is a substring of the longer invoke anchor; both occur once.
    src = "---\nname: x\ndescription: d\n---\n\ncall run.py now\n"
    sidecar = _sidecar([
        {"anchor": "call run.py now", "replacement": "A"},
        {"anchor": "run.py", "replacement": "B"},
    ])
    with pytest.raises(ValueError, match="substring of another"):
        build_wrapper(src, sidecar)


def test_fail_closed_anchor_inside_frontmatter():
    # 'aqg-demo' appears in the frontmatter `name:` line — anchoring it is rejected.
    src = "---\nname: aqg-frontmatter-anchor\ndescription: d\n---\n\nbody\n"
    sidecar = _sidecar([
        {"anchor": "aqg-frontmatter-anchor", "replacement": "x"},
    ])
    with pytest.raises(ValueError, match="frontmatter"):
        build_wrapper(src, sidecar)


def test_fail_closed_malformed_host_overrides_not_dict():
    sidecar = {"name": "x", "wrapper_generated": True, "host_overrides": ["bad"]}
    with pytest.raises(ValueError, match="host_overrides"):
        build_wrapper(_SOURCE, sidecar)


def test_fail_closed_malformed_claude_not_list():
    sidecar = {
        "name": "x", "wrapper_generated": True,
        "host_overrides": {"claude": "not-a-list"},
    }
    with pytest.raises(ValueError, match="claude"):
        build_wrapper(_SOURCE, sidecar)


def test_fail_closed_malformed_override_item_missing_anchor():
    sidecar = _sidecar([{"replacement": "x"}])  # no 'anchor'
    with pytest.raises(ValueError, match="anchor"):
        build_wrapper(_SOURCE, sidecar)


def test_fail_closed_malformed_override_item_replacement_not_str():
    sidecar = _sidecar([{"anchor": "## Boundary Rules", "replacement": 123}])
    with pytest.raises(ValueError, match="replacement"):
        build_wrapper(_SOURCE, sidecar)


def test_fail_closed_non_managed_sidecar_raises():
    sidecar = _sidecar([{"anchor": "## Boundary Rules", "replacement": "x"}],
                       managed=False)
    with pytest.raises(ValueError, match="non-managed"):
        build_wrapper(_SOURCE, sidecar)


def test_fail_closed_overlapping_anchors():
    # Two anchors whose single source occurrences overlap.
    src = "ABCDE\n"
    sidecar = _sidecar([
        {"anchor": "ABC", "replacement": "x"},
        {"anchor": "CDE", "replacement": "y"},
    ])
    # 'ABC' and 'CDE' are not substrings of each other but their spans share 'C'.
    with pytest.raises(ValueError, match="overlap"):
        build_wrapper(src, sidecar)


# ===== Slice 6: documented anchor-looking string is inert (no marker magic) =====


def test_documented_anchor_looking_string_is_inert():
    """A SKILL.md whose BODY documents an anchor-looking string inside a fenced
    code block — but that string is NOT listed as an anchor — regenerates
    unchanged. Proves there is no marker/placeholder magic: only the exact
    strings in host_overrides.claude are touched.
    """
    src = (
        "---\nname: x\ndescription: d\n---\n\n"
        "# Doc\n\n"
        "This skill's wrapper is generated. Example override:\n\n"
        "```json\n"
        '{ "anchor": "python3 foo.py", "replacement": "python3 bar.py" }\n'
        "```\n\n"
        "End.\n"
    )
    # Managed, but with NO overrides at all → must be a verbatim copy, even
    # though the body literally contains the words "anchor"/"replacement".
    out = build_wrapper(src, _sidecar([], managed=True))
    assert out == src
    assert '"anchor": "python3 foo.py"' in out  # documentation untouched


def test_documented_string_only_swapped_when_literally_an_anchor():
    """The same documented string CAN be swapped, but only because it is given
    verbatim as an anchor (and occurs exactly once). No placeholder inference."""
    src = (
        "---\nname: x\ndescription: d\n---\n\n"
        "run THE-EXACT-TOKEN once\n"
    )
    out = build_wrapper(src, _sidecar([
        {"anchor": "THE-EXACT-TOKEN", "replacement": "SWAPPED"},
    ]))
    assert "SWAPPED" in out
    assert "THE-EXACT-TOKEN" not in out


# ===== is_managed_skill strictness =====


def test_is_managed_strict_true():
    assert is_managed_skill({"wrapper_generated": True}) is True


def test_is_managed_strict_rejects_truthy_non_bool():
    assert is_managed_skill({"wrapper_generated": 1}) is False
    assert is_managed_skill({"wrapper_generated": "true"}) is False
    assert is_managed_skill({"wrapper_generated": "True"}) is False
    assert is_managed_skill({"wrapper_generated": [True]}) is False


def test_is_managed_false_and_absent():
    assert is_managed_skill({"wrapper_generated": False}) is False
    assert is_managed_skill({}) is False
    assert is_managed_skill({"name": "x"}) is False


def test_is_managed_non_dict_is_false():
    assert is_managed_skill(None) is False
    assert is_managed_skill("wrapper_generated") is False
    assert is_managed_skill(["wrapper_generated"]) is False
