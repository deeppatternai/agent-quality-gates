"""Precision-first calibration fixtures (double-audit G4).

A WELL-WRITTEN PR (behavioral tests paired with source) must produce ZERO
high-confidence candidates — false positives erode trust. A LOW-QUALITY PR
must produce candidates. These lock the precision posture end-to-end.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from aqg_test_quality_review import analyze, parse_diff  # noqa: E402


# A good PR: source change + a paired behavioral test with a guard + value assert.
_GOOD_PR = """\
diff --git a/src/calc.py b/src/calc.py
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,5 @@
 def add(a, b):
     return a + b
+
+def total(items):
+    return sum(items)
diff --git a/tests/test_calc.py b/tests/test_calc.py
--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -1,3 +1,8 @@
 from calc import add, total
+
+def test_total_sums_items():
+    result = total([1, 2, 3])
+    assert isinstance(result, int)
+    assert result == 6
"""


def test_good_pr_zero_high_confidence_candidates():
    result = analyze(parse_diff(_GOOD_PR))
    high = [c for c in result.candidates if c.confidence == "high"]
    assert high == [], f"good PR tripped high-conf candidates: {[c.pattern for c in high]}"
    # the test_calc change pairs calc.py → no coverage gap; the guard+value test
    # is `mixed`, not shape_only → no shape candidate; no flaky / suppression.
    assert result.bulk_shape_risk is False


# A low-quality PR: source change with NO test + a shape-only test + a skip.
_BAD_PR = """\
diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,1 +1,3 @@
 def login(u, p):
+    # new behavior, untested
+    return Session(u)
diff --git a/tests/test_widget.py b/tests/test_widget.py
--- a/tests/test_widget.py
+++ b/tests/test_widget.py
@@ -1,2 +1,7 @@
 import pytest
+
+def test_widget_shape():
+    w = make_widget()
+    assert isinstance(w, dict)
+    assert hasattr(w, 'id')
"""


def test_bad_pr_produces_candidates():
    result = analyze(parse_diff(_BAD_PR))
    concerns = {c.concern for c in result.candidates if c.confidence == "high"}
    # auth.py changed but no test_auth → coverage_gap (low here, since other tests
    # changed); the shape-only widget test → shape_over_behavioral (high).
    assert "shape_over_behavioral" in concerns
    all_concerns = {c.concern for c in result.candidates}
    assert "coverage_gap" in all_concerns
