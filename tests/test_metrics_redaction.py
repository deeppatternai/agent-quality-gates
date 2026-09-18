"""Tests for scripts/_metrics_redaction.py — metrics record schema + leak guard."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _metrics_redaction as mr  # noqa: E402


def _make_valid() -> dict:
    return {
        "schema_version": 1,
        "ts": "2026-05-03T12:34:56+00:00",
        "tool": "audit",
        "result": "pass",
        "duration_ms": 1234,
        "exit_code": 0,
        "tool_version": "0.2.6",
        "actor": "claude",
        "context": {
            "audit_id": "deadbeef",
            "audit_panel_size": 3,
            "findings_count": 14,
            "accepted_count": 12,
            "rejected_count": 2,
            "cwd_sha256_first8": "12345678",
            "git_branch_status": "present",
        },
        "marker": "auto-recorded-by-aqg-metrics",
    }


class TestSchema:
    def test_valid(self):
        assert mr.check_metrics_record(_make_valid()).is_safe

    def test_unknown_top_level_rejected(self):
        s = _make_valid(); s["leak_field"] = "x"
        assert not mr.check_metrics_record(s).is_safe

    def test_required_missing(self):
        s = _make_valid(); del s["tool"]
        assert not mr.check_metrics_record(s).is_safe

    def test_required_null_rejected(self):
        """gpt-5.5 #8: a required field cannot be None."""
        s = _make_valid(); s["ts"] = None
        assert not mr.check_metrics_record(s).is_safe


class TestEnums:
    def test_invalid_tool(self):
        s = _make_valid(); s["tool"] = "unknown"
        assert not mr.check_metrics_record(s).is_safe

    def test_invalid_result(self):
        s = _make_valid(); s["result"] = "succeeded"
        assert not mr.check_metrics_record(s).is_safe

    def test_actor_closed_enum(self):
        """gpt-5.5 #5: actor is a closed enum to prevent username leaks."""
        s = _make_valid(); s["actor"] = "a-username"
        assert not mr.check_metrics_record(s).is_safe

    @pytest.mark.parametrize("actor", ["claude", "codex", "gpt-5.5", "gemini", "o3", "human", "ci-bot", "other"])
    def test_actor_allowed(self, actor):
        s = _make_valid(); s["actor"] = actor
        assert mr.check_metrics_record(s).is_safe


class TestTokenSubstring:
    """gemini #3: token denylist substring search (regression guard from the #6 fix)."""

    @pytest.mark.parametrize("v", [
        "0.2.6-ghp_xxx",
        "v1-sk-ant-FAKE",
        "build-AKIAIOSFODNN7EXAM",
    ])
    def test_token_mid_string_caught(self, v):
        s = _make_valid(); s["tool_version"] = v[:32]
        assert not mr.check_metrics_record(s).is_safe


class TestNeverEchoRawValue:
    """gpt-5.5 #6: violation msg NEVER echoes raw value."""

    def test_token_value_not_in_violation_msg(self):
        leaked = "ghp_FAKE_TOKEN_XYZABCDEF"
        s = _make_valid(); s["tool_version"] = leaked[:32]
        result = mr.check_metrics_record(s)
        assert not result.is_safe
        joined = " ".join(result.violations)
        # 8-char fragment should NEVER appear
        assert "ghp_FAKE" not in joined, f"raw value leak in: {joined}"


class TestContextNested:
    def test_unknown_context_field_rejected(self):
        s = _make_valid(); s["context"] = dict(s["context"]); s["context"]["secret"] = "x"
        assert not mr.check_metrics_record(s).is_safe

    def test_invalid_branch_status(self):
        s = _make_valid(); s["context"] = dict(s["context"])
        s["context"]["git_branch_status"] = "garbage"
        assert not mr.check_metrics_record(s).is_safe

    def test_invalid_audit_id_hex(self):
        s = _make_valid(); s["context"] = dict(s["context"])
        s["context"]["audit_id"] = "DEADBEEF"  # uppercase
        assert not mr.check_metrics_record(s).is_safe

    def test_negative_count_rejected(self):
        s = _make_valid(); s["context"] = dict(s["context"])
        s["context"]["findings_count"] = -1
        assert not mr.check_metrics_record(s).is_safe


class TestRaiseAPI:
    def test_assert_safe_passes(self):
        mr.assert_safe_metrics_record(_make_valid())

    def test_assert_safe_raises(self):
        with pytest.raises(mr.MetricsRedactionError):
            mr.assert_safe_metrics_record({"schema_version": 99})
