"""L2 pre-launch hardening regression suite for the v2 ledger hook writer.

Each test pins one finding from audit bbf4ca5d (gpt-5.5 + gemini) + the Claude
review (workflow PR-B). Failing-before / passing-after — every bug-repro test
FAILS against the pre-fix scripts/ledger_hook_writer.py.
"""
from __future__ import annotations

import sys

import ledger_hook_writer as w
from ledger.conformance import validate_incoming_event

_SEP = w._FIELD_SEP
_NUL = w._NUL


def _rec(sha, ct, subject="s", body="b") -> str:
    return _SEP.join((sha, ct, subject, body))


# --- B1: out-of-range / non-ASCII committer ts crashes + drops the whole batch ---

def test_b1_parse_git_log_skips_out_of_range_timestamp():
    out = _rec("abc1234", "1700000000") + _NUL + _rec("def5678", "99999999999999")
    recs = w.parse_git_log(out)
    assert [r.sha for r in recs] == ["abc1234"]  # 14-digit ts skipped, not admitted


def test_b1_parse_git_log_skips_unicode_digit_timestamp():
    # str.isdigit() is True for fullwidth/superscript digits but int()/datetime would
    # choke; the ASCII-bounded regex skips them.
    out = _rec("abc1234", "1700000000") + _NUL + _rec("def5678", "１２")  # fullwidth 12
    recs = w.parse_git_log(out)
    assert [r.sha for r in recs] == ["abc1234"]


def test_b1_build_event_handles_admitted_timestamps():
    # every ts parse_git_log now admits (<=11 ASCII digits) is safe for build_event.
    for ct in ("0", "1700000000", "99999999999"):
        ev = w.build_event(project_id="o/r", session_id="s", commit=w.CommitRec("abc1234", ct, "fix", ""))
        ok, errors = validate_incoming_event(ev)
        assert ok, (ct, errors)


# --- B2: commit text with a secret must be redacted before it lands in the ledger ---

def test_b2_secret_in_subject_is_redacted():
    secret_subject = "ghp_0123456789abcdefghijklmnopqrstuvwxyz1"  # GitHub PAT shape
    ev = w.build_event(project_id="o/r", session_id="s",
                       commit=w.CommitRec("abc1234", "1700000000", secret_subject, ""))
    assert "ghp_" not in ev["payload"]["title"]
    assert "redacted" in ev["payload"]["title"]


def test_b2_secret_in_body_is_redacted():
    ev = w.build_event(project_id="o/r", session_id="s",
                       commit=w.CommitRec("abc1234", "1700000000", "fix: bug",
                                          "deploy key AKIAIOSFODNN7EXAMPLE rotated"))
    assert "AKIA" not in ev["payload"].get("detail", "")


def test_b2_ordinary_commit_text_not_redacted():
    # a normal subject + a file path/email (legitimate in commits) must NOT be redacted.
    ev = w.build_event(project_id="o/r", session_id="s",
                       commit=w.CommitRec("abc1234", "1700000000",
                                          "fix(auth): handle src/login.py edge case", "by a@b.com"))
    assert ev["payload"]["title"] == "fix(auth): handle src/login.py edge case"
    assert ev["payload"].get("detail") == "by a@b.com"


# --- B6: a set-but-invalid env dir must still fall through to event.cwd ---

def test_b6_resolve_cwd_invalid_env_falls_through_to_event_cwd(monkeypatch, tmp_path):
    valid = tmp_path / "real"
    valid.mkdir()
    monkeypatch.setenv("AQG_HOOK_PROJECT_DIR", str(tmp_path / "does-not-exist"))
    assert w._resolve_cwd({"cwd": str(valid)}) == valid


def test_b6_resolve_cwd_valid_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AQG_HOOK_PROJECT_DIR", str(tmp_path))
    assert w._resolve_cwd({"cwd": "/nonexistent"}) == tmp_path


def test_b6_resolve_cwd_no_env_uses_event_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("AQG_HOOK_PROJECT_DIR", raising=False)
    assert w._resolve_cwd({"cwd": str(tmp_path)}) == tmp_path


# --- B7: invalid UTF-8 in subprocess stdout must not crash _safe_run ---

def test_b7_safe_run_tolerates_invalid_utf8(tmp_path):
    rc, out = w._safe_run(
        [sys.executable, "-c", "import os; os.write(1, b'\\xff\\xfe bad bytes')"],
        cwd=tmp_path,
    )
    assert rc == 0
    assert isinstance(out, str)  # decoded with errors='replace', no UnicodeDecodeError


# --- B8: _anchor_unix must never raise (incl. OverflowError on extreme dates) ---

def test_b8_anchor_unix_never_raises_on_extreme_or_malformed():
    for val in ("0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59Z", "garbage", "", None):
        result = w._anchor_unix(val)  # must not raise
        assert result is None or isinstance(result, int)
