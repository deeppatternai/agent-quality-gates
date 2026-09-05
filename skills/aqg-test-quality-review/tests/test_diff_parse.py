"""Slice 1 tests: unified-diff parsing + file classification.

Run from skills/aqg-test-quality-review/:
    python3 -m pytest tests/
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from aqg_test_quality_review import (  # noqa: E402
    is_source_file,
    is_test_file,
    parse_diff,
)


# ---- file classification --------------------------------------------------


def test_is_test_file_python():
    assert is_test_file("tests/test_foo.py")
    assert is_test_file("src/pkg/foo_test.py")
    assert is_test_file("a/b/__tests__/widget.js")


def test_is_test_file_js_ts():
    assert is_test_file("src/widget.test.tsx")
    assert is_test_file("src/widget.spec.ts")


def test_is_test_file_other_langs():
    assert is_test_file("src/FooTest.java")
    assert is_test_file("pkg/foo_test.go")
    assert is_test_file("spec/models/user_spec.rb")


def test_is_source_file_excludes_tests():
    assert is_source_file("src/auth/session.py")
    assert not is_source_file("tests/test_session.py")
    assert not is_source_file("src/session.test.ts")


def test_is_source_file_excludes_non_code():
    assert not is_source_file("README.md")
    assert not is_source_file("config.yaml")
    assert not is_source_file("data.json")


# ---- diff parsing ---------------------------------------------------------


_DIFF_MODIFY = """\
diff --git a/src/foo.py b/src/foo.py
index 8c6733e..394acac 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,5 +1,7 @@
 def existing():
     pass
+def added_one():
+    return 1
 # trailing context
"""


def test_parse_modify_tracks_added_linenos():
    files = parse_diff(_DIFF_MODIFY)
    assert len(files) == 1
    f = files[0]
    assert f.path == "src/foo.py"
    assert not f.is_new and not f.is_deleted
    texts = [a.text for a in f.added]
    assert texts == ["def added_one():", "    return 1"]
    # hunk starts at new line 1; 2 context lines then the 2 added lines → 3,4
    linenos = [a.lineno for a in f.added]
    assert linenos == [3, 4]


_DIFF_NEW = """\
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
index 0000000..abc1234
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,2 @@
+def test_x():
+    assert f() == 3
"""


def test_parse_new_file():
    files = parse_diff(_DIFF_NEW)
    assert len(files) == 1
    f = files[0]
    assert f.path == "tests/test_new.py"
    assert f.is_new
    assert [a.text for a in f.added] == ["def test_x():", "    assert f() == 3"]
    assert f.removed == ()


_DIFF_DELETE = """\
diff --git a/tests/test_old.py b/tests/test_old.py
deleted file mode 100644
index abc1234..0000000
--- a/tests/test_old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_y():
-    assert g() == 7
"""


def test_parse_deleted_file_uses_old_path():
    files = parse_diff(_DIFF_DELETE)
    assert len(files) == 1
    f = files[0]
    assert f.path == "tests/test_old.py"
    assert f.is_deleted
    assert f.added == ()
    assert list(f.removed) == ["def test_y():", "    assert g() == 7"]


_DIFF_MULTI = _DIFF_MODIFY + _DIFF_NEW


def test_parse_multiple_files():
    files = parse_diff(_DIFF_MULTI)
    assert [f.path for f in files] == ["src/foo.py", "tests/test_new.py"]


def test_parse_empty():
    assert parse_diff("") == []
    assert parse_diff("not a diff\njust text\n") == []
