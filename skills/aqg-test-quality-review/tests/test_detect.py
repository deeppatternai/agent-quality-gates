"""Slice 3 tests: candidate detection (shape / flaky / suppression / coverage)
+ counts + bulk_shape advisory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from aqg_test_quality_review import (  # noqa: E402
    AddedLine,
    FileDiff,
    bulk_shape_test_risk,
    compute_counts,
    detect_coverage_gaps,
    detect_flaky_candidates,
    detect_shape_candidates,
    detect_suppression_candidates,
)


def _tf(path: str, lines: list[str], start: int = 1, removed=()) -> FileDiff:
    added = tuple(AddedLine(lineno=start + i, text=t) for i, t in enumerate(lines))
    return FileDiff(path=path, is_new=True, added=added, removed=tuple(removed))


# ---- shape candidates -----------------------------------------------------


def test_shape_candidate_from_shape_only_test():
    f = _tf("tests/test_x.py", [
        "def test_shape():",
        "    assert isinstance(make(), dict)",
    ])
    cands = detect_shape_candidates([f])
    assert len(cands) == 1
    assert cands[0].concern == "shape_over_behavioral"
    assert cands[0].confidence == "high"
    assert cands[0].evidence == "tests/test_x.py:2"


def test_no_shape_candidate_for_mixed_test():
    f = _tf("tests/test_x.py", [
        "def test_login():",
        "    assert isinstance(s, Session)",
        "    assert s.user_id == 'u'",
    ])
    assert detect_shape_candidates([f]) == []


# ---- flaky candidates -----------------------------------------------------


def test_flaky_high_sleep_and_walltime():
    f = _tf("tests/test_x.py", [
        "def test_t():",
        "    time.sleep(2)",
        "    now = datetime.now()",
    ])
    cands = detect_flaky_candidates([f])
    assert {c.confidence for c in cands} == {"high"}
    assert len(cands) == 2


def test_flaky_low_random_network():
    f = _tf("tests/test_x.py", [
        "    x = random.randint(1, 9)",
        "    r = requests.get('http://x')",
    ])
    cands = detect_flaky_candidates([f])
    assert {c.confidence for c in cands} == {"low"}
    assert len(cands) == 2


# ---- suppression candidates ----------------------------------------------


def test_suppression_skip_added():
    f = _tf("tests/test_x.py", ["@pytest.mark.skip(reason='flaky')", "def test_q():"])
    cands = detect_suppression_candidates([f])
    assert len(cands) == 1
    assert cands[0].subtype == "skip_added"
    assert cands[0].confidence == "high"


def test_suppression_js_skip():
    f = _tf("src/x.test.ts", ["  it.skip('later', () => {"])
    cands = detect_suppression_candidates([f])
    assert len(cands) == 1 and cands[0].subtype == "skip_added"


def test_suppression_assertion_deleted():
    f = _tf("tests/test_x.py", ["def test_q():", "    pass"],
            removed=["    assert compute() == 42"])
    cands = detect_suppression_candidates([f])
    sub = [c.subtype for c in cands]
    assert "assertion_deleted" in sub


# ---- coverage gaps --------------------------------------------------------


def test_coverage_gap_high_when_zero_tests():
    src = FileDiff(path="src/auth/session.py",
                   added=(AddedLine(1, "def login(): ..."),))
    cands = detect_coverage_gaps([src])
    assert len(cands) == 1
    assert cands[0].confidence == "high"


def test_coverage_gap_low_when_other_tests_changed():
    src = FileDiff(path="src/auth/session.py", added=(AddedLine(1, "def login(): ..."),))
    other_test = FileDiff(path="tests/test_other.py", is_new=True,
                          added=(AddedLine(1, "def test_other(): assert True"),))
    cands = detect_coverage_gaps([src, other_test])
    gaps = [c for c in cands if c.concern == "coverage_gap"]
    assert len(gaps) == 1 and gaps[0].confidence == "low"


def test_coverage_gap_none_when_paired():
    src = FileDiff(path="src/session.py", added=(AddedLine(1, "x"),))
    test = FileDiff(path="tests/test_session.py", is_new=True,
                    added=(AddedLine(1, "def test_session(): assert s() == 1"),))
    assert detect_coverage_gaps([src, test]) == []


# ---- counts + bulk advisory ----------------------------------------------


def test_counts_and_shape_ratio_test_level():
    f = _tf("tests/test_x.py", [
        "def test_a():",
        "    assert isinstance(a(), dict)",   # shape_only
        "def test_b():",
        "    assert b() == 2",                # behavioral_only
        "def test_c():",
        "    assert isinstance(c(), list)",   # mixed
        "    assert c() == []",
    ])
    c = compute_counts([f])
    assert c.test_funcs_added == 3
    assert c.shape_only_tests == 1
    assert c.mixed_tests == 1
    assert c.behavioral_only_tests == 1
    assert c.shape_ratio == round(1 / 3, 4)


def test_bulk_shape_risk_fires_and_not():
    # 6 shape-only tests → bulk + ratio 1.0 → advisory fires
    lines = []
    for i in range(6):
        lines += [f"def test_{i}():", f"    assert isinstance(f{i}(), dict)"]
    assert bulk_shape_test_risk(compute_counts([_tf("tests/t.py", lines)])) is True
    # 6 behavioral tests → no shape risk
    lines2 = []
    for i in range(6):
        lines2 += [f"def test_{i}():", f"    assert f{i}() == {i}"]
    assert bulk_shape_test_risk(compute_counts([_tf("tests/t.py", lines2)])) is False
