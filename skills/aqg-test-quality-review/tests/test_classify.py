"""Slice 2 tests: assertion classification + per-test grouping.

Locks the double-audit fixes:
- G5: toMatchSnapshot is BEHAVIORAL, not a shape smell.
- G1 false-positive: a test with an isinstance guard PLUS a value assertion is
  `mixed`, NOT `shape_only` — so it is not a shape candidate.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from aqg_test_quality_review import (  # noqa: E402
    AddedLine,
    FileDiff,
    classify_assertion,
    group_added_tests,
)


# ---- classify_assertion ---------------------------------------------------


def test_shape_isinstance():
    assert classify_assertion("    assert isinstance(x, dict)") == "shape"
    assert classify_assertion("    self.assertIsInstance(x, list)") == "shape"
    assert classify_assertion("    assert hasattr(obj, 'foo')") == "shape"


def test_shape_type_equality():
    assert classify_assertion("    assert type(x) == int") == "shape"
    assert classify_assertion("    assert type(x) is dict") == "shape"


def test_shape_js():
    assert classify_assertion("    expect(typeof x).toBe('string')") == "shape"
    assert classify_assertion("    expect(x).toBeInstanceOf(Array)") == "shape"
    assert classify_assertion("    expect(x).toHaveProperty('id')") == "shape"


def test_behavioral_value_assert():
    assert classify_assertion("    assert login(u) == expected") == "behavioral"
    assert classify_assertion("    self.assertEqual(total, 42)") == "behavioral"
    assert classify_assertion("    assert 'admin' in roles") == "behavioral"


def test_behavioral_raises():
    assert classify_assertion("    with pytest.raises(ValueError):") == "behavioral"
    assert classify_assertion("    self.assertRaises(KeyError, f)") == "behavioral"


def test_behavioral_snapshot_is_not_shape():
    # G5: snapshot captures serialized VALUES → behavioral, never shape.
    assert classify_assertion("    expect(rendered).toMatchSnapshot()") == "behavioral"
    assert classify_assertion("    expect(out).toMatchInlineSnapshot()") == "behavioral"


def test_behavioral_js_value():
    assert classify_assertion("    expect(sum(1, 2)).toBe(3)") == "behavioral"
    assert classify_assertion("    expect(fn).toThrow('bad')") == "behavioral"


def test_bare_assert_ambiguous_is_none():
    assert classify_assertion("    assert result") is None
    assert classify_assertion("    x = compute()") is None
    # A commented-out assertion is dead code, not a live assertion.
    assert classify_assertion("    # assert isinstance(x, dict)  # commented") is None


# ---- group_added_tests ----------------------------------------------------


def _filediff(path: str, lines: list[str], start: int = 1) -> FileDiff:
    added = tuple(AddedLine(lineno=start + i, text=t) for i, t in enumerate(lines))
    return FileDiff(path=path, is_new=True, added=added)


def test_group_shape_only_test():
    f = _filediff(
        "tests/test_x.py",
        [
            "def test_session_shape():",
            "    s = make_session()",
            "    assert isinstance(s, dict)",
            "    assert hasattr(s, 'id')",
        ],
    )
    regions = group_added_tests(f)
    assert len(regions) == 1
    r = regions[0]
    assert r.name == "test_session_shape"
    assert r.kind == "shape_only"
    assert len(r.shape_lines) == 2


def test_group_mixed_is_not_shape_only():
    # The isinstance is a guard; the value assertion makes this MIXED (G1).
    f = _filediff(
        "tests/test_x.py",
        [
            "def test_login_returns_session():",
            "    s = login('u', 'p')",
            "    assert isinstance(s, Session)",
            "    assert s.user_id == 'u'",
        ],
    )
    regions = group_added_tests(f)
    assert len(regions) == 1
    assert regions[0].kind == "mixed"


def test_group_behavioral_only():
    f = _filediff(
        "tests/test_x.py",
        [
            "def test_total():",
            "    assert total([1, 2, 3]) == 6",
        ],
    )
    assert group_added_tests(f)[0].kind == "behavioral_only"


def test_group_multiple_tests():
    f = _filediff(
        "tests/test_x.py",
        [
            "def test_a():",
            "    assert isinstance(a(), dict)",
            "def test_b():",
            "    assert b() == 2",
        ],
    )
    regions = group_added_tests(f)
    assert [r.kind for r in regions] == ["shape_only", "behavioral_only"]


def test_group_js_it_block():
    f = _filediff(
        "src/x.test.ts",
        [
            "  it('returns shape', () => {",
            "    expect(typeof out).toBe('object')",
            "    expect(out).toHaveProperty('id')",
            "  })",
        ],
    )
    regions = group_added_tests(f)
    assert len(regions) == 1
    assert regions[0].kind == "shape_only"


def test_group_assertions_before_def_ignored():
    # Lines added into a pre-existing test (no added def) are not grouped.
    f = _filediff(
        "tests/test_x.py",
        [
            "    assert isinstance(x, dict)",  # no enclosing added def
        ],
    )
    assert group_added_tests(f) == []
