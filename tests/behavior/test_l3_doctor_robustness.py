"""L3 GD-08/09/10/11 regression suite for aqg_doctor.py robustness.

aqg_doctor.py is the *diagnostic* tool — it must degrade gracefully on the very
corruption it exists to surface, never crash on it. Four reachable gaps (all in
install-mode checks, no third-party deps):

- GD-08 (MED, check_aqg_root): `VERSION.read_text(encoding="utf-8")` was unguarded.
  A non-UTF-8 VERSION (corrupt checkout) raised UnicodeDecodeError (a ValueError
  subclass, not caught) → doctor traceback instead of a FAIL result.
- GD-09 (MED, check_skill_install copy-mode): the `.aqg-root` pointer
  `read_text(encoding="utf-8")` had the same gap → crash on a tampered/corrupt
  pointer. Should degrade to WARN.
- GD-10 (LOW, check_skill_install copy-mode): an *empty* skill dir (or one missing
  SKILL.md) returned PASS "copy mode (no .aqg-root)" — reporting a broken install
  as healthy. A copy-mode skill dir must contain SKILL.md to be usable.
- GD-11 (LOW): raw VERSION / .aqg-root content was echoed verbatim into the
  CheckResult.detail, so multi-line / control-char payloads broke the
  one-line-per-check output contract. Untrusted file content must be sanitized
  (one-line, control-char-strip, truncate) before echo.

Each repro FAILS against pre-fix source (stash-proven):
`git stash push -- scripts/aqg_doctor.py` -> RED.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import aqg_doctor as doc  # noqa: E402


def _result(results: list, name: str):
    matches = [r for r in results if r.name == name]
    assert matches, f"no CheckResult named {name!r} in {[r.name for r in results]}"
    return matches[0]


def _make_skill(skills_dir: Path, name: str, *, with_skill_md: bool = True) -> Path:
    skill = skills_dir / name
    skill.mkdir(parents=True)
    if with_skill_md:
        (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    return skill


# --------------------------------------------------------------------------- #
# GD-08 — non-UTF-8 VERSION must not crash check_aqg_root
# --------------------------------------------------------------------------- #
class TestGd08VersionDecode:
    def test_non_utf8_version_yields_fail_not_crash(self, tmp_path: Path) -> None:
        # \xff is an illegal UTF-8 start byte → read_text(utf-8) raises
        # UnicodeDecodeError pre-fix.
        (tmp_path / "VERSION").write_bytes(b"\xff\xfe garbage")
        results = doc.check_aqg_root(tmp_path, "test")  # pre-fix: UnicodeDecodeError
        version = _result(results, "version")
        assert version.status == "FAIL", version
        # detail must not leak the raw undecodable bytes content
        assert "garbage" not in version.detail

    def test_valid_version_still_passes(self, tmp_path: Path) -> None:
        (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        results = doc.check_aqg_root(tmp_path, "test")
        version = _result(results, "version")
        assert version.status == "PASS", version
        assert "9.9.9" in version.detail


# --------------------------------------------------------------------------- #
# GD-09 — non-UTF-8 .aqg-root pointer must not crash check_skill_install
# --------------------------------------------------------------------------- #
class TestGd09PointerDecode:
    def test_non_utf8_pointer_yields_warn_not_crash(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skill = _make_skill(skills_dir, "aqg-x")
        (skill / ".aqg-root").write_bytes(b"\xff\xfe /tampered")  # non-UTF-8
        results = doc.check_skill_install(
            label="t",
            target_dir=skills_dir,
            expected_names=("aqg-x",),
            aqg_root=tmp_path,
        )  # pre-fix: UnicodeDecodeError
        item = _result(results, "t:aqg-x")
        assert item.status == "WARN", item
        assert "tampered" not in item.detail


# --------------------------------------------------------------------------- #
# GD-10 — empty / SKILL.md-less copy-mode dir must not report PASS
# --------------------------------------------------------------------------- #
class TestGd10CopyModeContainment:
    def test_empty_copy_dir_is_not_pass(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "aqg-x", with_skill_md=False)  # empty dir
        results = doc.check_skill_install(
            label="t",
            target_dir=skills_dir,
            expected_names=("aqg-x",),
            aqg_root=tmp_path,
        )
        item = _result(results, "t:aqg-x")
        assert item.status == "FAIL", item  # pre-fix: PASS "copy mode (no .aqg-root)"
        assert "SKILL.md" in item.detail

    def test_copy_dir_missing_skill_md_with_pointer_is_not_pass(self, tmp_path: Path) -> None:
        # Even a valid .aqg-root pointer must not rescue a dir with no SKILL.md.
        skills_dir = tmp_path / "skills"
        skill = _make_skill(skills_dir, "aqg-x", with_skill_md=False)
        (skill / ".aqg-root").write_text(str(tmp_path) + "\n", encoding="utf-8")
        results = doc.check_skill_install(
            label="t",
            target_dir=skills_dir,
            expected_names=("aqg-x",),
            aqg_root=tmp_path,
        )
        item = _result(results, "t:aqg-x")
        assert item.status == "FAIL", item

    def test_unmanaged_plain_dir_with_skill_md_warns(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "aqg-x")  # has SKILL.md, no .aqg-root
        results = doc.check_skill_install(
            label="t",
            target_dir=skills_dir,
            expected_names=("aqg-x",),
            aqg_root=tmp_path,
        )
        item = _result(results, "t:aqg-x")
        assert item.status == "WARN", item
        assert "unmarked plain directory" in item.detail
        assert "legacy copy" in item.detail
        assert "link mode" in (item.fix or "")

    def test_marked_copy_dir_is_reported_as_copied(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skill = _make_skill(skills_dir, "aqg-x")
        (skill / ".aqg-root").write_text(str(tmp_path), encoding="utf-8")
        item = _result(
            doc.check_skill_install(
                label="t",
                target_dir=skills_dir,
                expected_names=("aqg-x",),
                aqg_root=tmp_path,
            ),
            "t:aqg-x",
        )
        assert item.status == "PASS", item
        assert "copied directory" in item.detail


# --------------------------------------------------------------------------- #
# GD-11 — untrusted file content must be sanitized before echo
# --------------------------------------------------------------------------- #
class TestGd11SanitizeEcho:
    def test_multiline_pointer_detail_is_single_line(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skill = _make_skill(skills_dir, "aqg-x")
        # invalid path (triggers WARN branch) + newline + control chars
        (skill / ".aqg-root").write_text(
            "/nonexistent\nINJECTED\x00\x07more", encoding="utf-8"
        )
        results = doc.check_skill_install(
            label="t",
            target_dir=skills_dir,
            expected_names=("aqg-x",),
            aqg_root=tmp_path,
        )
        item = _result(results, "t:aqg-x")
        assert item.status == "WARN", item
        assert "\n" not in item.detail
        assert "\x00" not in item.detail
        assert "\x07" not in item.detail

    def test_version_with_control_chars_sanitized(self, tmp_path: Path) -> None:
        # legal UTF-8 but contains control chars + embedded newline
        (tmp_path / "VERSION").write_text("1.0\x00\x07\n2.0", encoding="utf-8")
        results = doc.check_aqg_root(tmp_path, "test")
        version = _result(results, "version")
        assert version.status == "PASS", version
        assert "\n" not in version.detail
        assert "\x00" not in version.detail
        assert "\x07" not in version.detail

    def test_sanitize_echo_helper_collapses_and_truncates(self) -> None:
        fn = doc._sanitize_echo
        # newline/tab collapse to single space, token boundaries kept
        assert fn("a\nb\tc") == "a b c"
        # non-whitespace C0 control chars + DEL stripped (\x1c-\x1f are
        # str.isspace()==True so they collapse to a space, not stripped)
        assert fn("a\x00\x01\x02\x7fb") == "ab"
        # normal path is a no-op
        assert fn("/Users/x/repo") == "/Users/x/repo"
        # over-limit input is truncated with a marker
        out = fn("x" * 500, limit=200)
        assert len(out) <= 200 + len("…(truncated)")
        assert "(truncated)" in out

    def test_sanitize_echo_strips_ansi_and_unicode_separators(self) -> None:
        fn = doc._sanitize_echo
        # \x1b ESC — the ANSI terminal-manipulation vector — is stripped
        assert fn("a\x1b[31mb") == "a[31mb"
        assert "\x1b" not in fn("\x1b[2J\x1b[H pwned")
        # Unicode line/paragraph separators (NEL/LS/PS) collapse to a space
        for sep in ("\u0085", "\u2028", "\u2029"):
            assert sep not in fn("a" + sep + "b")
            assert fn("a" + sep + "b") == "a b"
        # Cf format chars (zero-width space, RTL override) are stripped
        for cf in ("\u200b", "\u202e"):
            assert cf not in fn("a" + cf + "b")
            assert fn("a" + cf + "b") == "ab"


# --------------------------------------------------------------------------- #
# GD-10 sister gap — symlink target must also contain SKILL.md
# --------------------------------------------------------------------------- #
class TestGd10SymlinkContainment:
    def _link_to(self, tmp_path: Path, *, with_skill_md: bool) -> list:
        # tmp_path.resolve() mirrors resolve_aqg_root()'s .resolve(); on macOS
        # /tmp -> /private/tmp, so the symlink target must live under the
        # resolved root for the in-AQG_ROOT branch to be exercised.
        base = tmp_path.resolve()
        root = base / "aqgroot"
        target = root / "skills" / "aqg-x"
        target.mkdir(parents=True)
        if with_skill_md:
            (target / "SKILL.md").write_text("# skill\n", encoding="utf-8")
        skills = base / "claude-skills"
        skills.mkdir()
        try:
            (skills / "aqg-x").symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlink creation is unavailable")
        return doc.check_skill_install(
            label="t",
            target_dir=skills,
            expected_names=("aqg-x",),
            aqg_root=root,
        )

    def test_symlink_target_without_skill_md_is_fail(self, tmp_path: Path) -> None:
        item = _result(self._link_to(tmp_path, with_skill_md=False), "t:aqg-x")
        assert item.status == "FAIL", item  # pre-fix: PASS "-> {resolved}"
        assert "SKILL.md" in item.detail

    def test_symlink_target_with_skill_md_passes(self, tmp_path: Path) -> None:
        item = _result(self._link_to(tmp_path, with_skill_md=True), "t:aqg-x")
        assert item.status == "PASS", item


def test_doctor_reports_junction_separately(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "aqg-root"
    skill = root / "skills" / "aqg-x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    monkeypatch.setattr(
        doc,
        "classify_install",
        lambda path: "junction" if path == skill else "missing",
    )

    item = _result(
        doc.check_skill_install(
            label="t",
            target_dir=root / "skills",
            expected_names=("aqg-x",),
            aqg_root=root,
        ),
        "t:aqg-x",
    )
    assert item.status == "PASS", item
    assert item.detail.startswith("junction ->")


# --------------------------------------------------------------------------- #
# Bounded read — oversized sentinel files must not be echoed whole
# --------------------------------------------------------------------------- #
class TestBoundedRead:
    def test_oversized_version_detail_is_bounded(self, tmp_path: Path) -> None:
        # Far larger than _MAX_PROBE_CHARS. NOTE: a true MemoryError needs
        # multi-GB input and is NOT exercised here, so this is a smoke/contract
        # guard, not a stash-proven RED (sanitize-truncate alone already bounds
        # the detail pre-fix). It pins the user-visible contract that doctor
        # never echoes the whole oversized file, defended by both bounded read
        # and sanitize-truncate.
        big = "9" * (doc._MAX_PROBE_CHARS * 8)
        (tmp_path / "VERSION").write_text(big, encoding="utf-8")
        version = _result(doc.check_aqg_root(tmp_path, "test"), "version")
        assert version.status == "PASS", version
        assert len(version.detail) < len(big)
