"""Tests for scripts/_wip_redaction.py — WIP snapshot allowlist guard.

Each test is annotated with the accepted finding it corresponds to from the three-round audit (audit_id 656913cb).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _wip_redaction as wr  # noqa: E402


def _make_valid_snapshot() -> dict:
    return {
        "schema_version": 1,
        "session_id": "abc123def456",
        "saved_at_iso": "2026-05-03T12:34:56+00:00",
        "trigger": "PreCompact",
        "cwd_sha256_first8": "deadbeef",
        "cwd_status": {
            "exists": True,
            "is_directory": True,
            "git_branch_sha256_first8": "12345678",
            "git_is_default_branch": False,
            "git_branch_status": "present",
            "git_dirty": True,
            "git_ahead_count": 1,
            "git_behind_count": 0,
            "modified_files_count": 3,
            "untracked_files_count": 2,
        },
        "recent_open_pr_count": 1,
        "todo_state": {
            "items_count": 5,
            "in_progress_count": 1,
            "completed_count": 4,
            "pending_count": 0,
        },
        "marker": "auto-saved-precompact",
    }


class TestSchema:
    def test_valid_snapshot_passes(self):
        assert wr.check_wip_snapshot(_make_valid_snapshot()).is_safe

    def test_missing_required_field(self):
        s = _make_valid_snapshot()
        del s["session_id"]
        result = wr.check_wip_snapshot(s)
        assert not result.is_safe
        assert any("session_id" in v for v in result.violations)

    def test_unknown_top_level_field_rejected(self):
        """o3 #2 spirit + my own contract: adding a new field must go through audit; the allowlist enforces it."""
        s = _make_valid_snapshot()
        s["secret_field"] = "anything"
        result = wr.check_wip_snapshot(s)
        assert not result.is_safe
        assert any("secret_field" in v for v in result.violations)

    def test_session_id_path_traversal_rejected(self):
        """gpt-5.5 #1 accepted: the session_id regex guards against path traversal."""
        s = _make_valid_snapshot()
        s["session_id"] = "../../etc/passwd"
        result = wr.check_wip_snapshot(s)
        assert not result.is_safe
        assert any("session_id" in v for v in result.violations)

    def test_session_id_with_slash_rejected(self):
        s = _make_valid_snapshot()
        s["session_id"] = "good/bad"
        assert not wr.check_wip_snapshot(s).is_safe

    def test_session_id_too_long_rejected(self):
        s = _make_valid_snapshot()
        s["session_id"] = "a" * 65
        assert not wr.check_wip_snapshot(s).is_safe

    def test_unknown_trigger_rejected(self):
        s = _make_valid_snapshot()
        s["trigger"] = "Stop"
        assert not wr.check_wip_snapshot(s).is_safe

    def test_unknown_schema_version_rejected(self):
        """gpt-5.5 #7 accepted: handling of schema_version evolution."""
        s = _make_valid_snapshot()
        s["schema_version"] = 99
        assert not wr.check_wip_snapshot(s).is_safe

    def test_invalid_iso_rejected(self):
        s = _make_valid_snapshot()
        s["saved_at_iso"] = "not-a-date"
        assert not wr.check_wip_snapshot(s).is_safe

    def test_sha256_first8_wrong_format_rejected(self):
        s = _make_valid_snapshot()
        s["cwd_sha256_first8"] = "ABCDEFGH"  # uppercase
        assert not wr.check_wip_snapshot(s).is_safe

    def test_sha256_first8_wrong_length_rejected(self):
        s = _make_valid_snapshot()
        s["cwd_sha256_first8"] = "deadbee"  # 7 chars
        assert not wr.check_wip_snapshot(s).is_safe

    def test_count_negative_rejected(self):
        s = _make_valid_snapshot()
        s["recent_open_pr_count"] = -1
        assert not wr.check_wip_snapshot(s).is_safe

    def test_count_bool_rejected(self):
        s = _make_valid_snapshot()
        s["recent_open_pr_count"] = True
        assert not wr.check_wip_snapshot(s).is_safe


class TestCwdStatusNested:
    def test_unknown_cwd_status_field_rejected(self):
        """gpt-5.5 #3 + o3 #1: raw branch name not allowed; new fields must audit."""
        s = _make_valid_snapshot()
        s["cwd_status"]["raw_branch_name"] = "secret-hotfix/john-payroll"
        result = wr.check_wip_snapshot(s)
        assert not result.is_safe
        assert any("raw_branch_name" in v for v in result.violations)

    def test_cwd_status_bool_field_must_be_bool(self):
        s = _make_valid_snapshot()
        s["cwd_status"]["git_dirty"] = "yes"
        assert not wr.check_wip_snapshot(s).is_safe

    def test_cwd_status_branch_status_enum(self):
        s = _make_valid_snapshot()
        s["cwd_status"]["git_branch_status"] = "garbage"
        assert not wr.check_wip_snapshot(s).is_safe

    def test_cwd_status_branch_status_detached_ok(self):
        s = _make_valid_snapshot()
        s["cwd_status"]["git_branch_status"] = "detached"
        s["cwd_status"]["git_branch_sha256_first8"] = None
        s["cwd_status"]["git_is_default_branch"] = None
        assert wr.check_wip_snapshot(s).is_safe


class TestTodoStateNested:
    def test_unknown_todo_field_rejected(self):
        s = _make_valid_snapshot()
        s["todo_state"]["items"] = ["raw item text"]  # raw text must not enter the snapshot
        assert not wr.check_wip_snapshot(s).is_safe

    def test_todo_state_optional(self):
        s = _make_valid_snapshot()
        del s["todo_state"]
        assert wr.check_wip_snapshot(s).is_safe

    def test_todo_state_null(self):
        s = _make_valid_snapshot()
        s["todo_state"] = None
        assert wr.check_wip_snapshot(s).is_safe


class TestPostImplTokenDenylist:
    """Post-impl two-round audit, gpt-5.5 #2 accepted: session_id rejects a token-shape prefix."""

    @pytest.mark.parametrize(
        "token_sid",
        [
            "ghp_FAKE_TOKEN_SHAPED_xxx",
            "gho_FAKE_OAUTH",
            "github_pat_11AAAAAAA0",
            "sk-ant-FAKE",
            "sk-FAKE_OPENAI",
            "AKIAIOSFODNN7EXAMPLE",
            "AIzaSyFAKE_GOOGLE",
            "xoxb-fake-slack-bot",
        ],
    )
    def test_token_shaped_session_id_rejected(self, token_sid):
        s = _make_valid_snapshot()
        s["session_id"] = token_sid
        result = wr.check_wip_snapshot(s)
        assert not result.is_safe
        assert any("token-shape" in v for v in result.violations)

    def test_normal_session_id_passes(self):
        s = _make_valid_snapshot()
        s["session_id"] = "abc123_DEF-456"
        assert wr.check_wip_snapshot(s).is_safe

    def test_mid_string_embedded_token_rejected(self):
        """Three-round audit 4f0c48c0 #15: the old _looks_like_token used startswith,
        so a token embedded mid-string in session_id (sess-ghp_...) was missed. After switching
        to scan_identifier (substring belt) it must be rejected."""
        s = _make_valid_snapshot()
        s["session_id"] = "sess-ghp_" + "a" * 16
        result = wr.check_wip_snapshot(s)
        assert not result.is_safe
        assert any("token-shape" in v for v in result.violations)


class TestTypeGuards:
    """Three-round audit 4f0c48c0 #7: add an isinstance guard before the enum membership check, so a non-str
    yields a clear type violation instead of falling to an enum-mismatch (and prevents a potential TypeError)."""

    def test_trigger_non_str_rejected(self):
        s = _make_valid_snapshot()
        s["trigger"] = ["PreCompact"]
        result = wr.check_wip_snapshot(s)
        assert not result.is_safe
        assert any("trigger: must be str" in v for v in result.violations)

    def test_git_branch_status_non_str_rejected(self):
        s = _make_valid_snapshot()
        s["cwd_status"]["git_branch_status"] = 123
        result = wr.check_wip_snapshot(s)
        assert not result.is_safe
        assert any(
            "git_branch_status: must be str" in v for v in result.violations
        )

    def test_marker_non_str_rejected(self):
        s = _make_valid_snapshot()
        s["marker"] = ["auto-saved-precompact"]
        result = wr.check_wip_snapshot(s)
        assert not result.is_safe
        assert any("marker: must be str" in v for v in result.violations)


class TestRaiseAPI:
    def test_assert_safe_passes_silently(self):
        wr.assert_safe_wip_snapshot(_make_valid_snapshot())

    def test_assert_safe_raises_on_violation(self):
        with pytest.raises(wr.WipRedactionError) as exc_info:
            wr.assert_safe_wip_snapshot({"schema_version": 99})
        assert "schema_version" in str(exc_info.value)

    def test_assert_safe_raises_on_non_mapping(self):
        with pytest.raises(wr.WipRedactionError):
            wr.assert_safe_wip_snapshot([1, 2, 3])
