"""Tests for scripts/_orchestration_redaction.py + validate_orchestration_manifest.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _orchestration_redaction as ore  # noqa: E402


REPO = Path(__file__).resolve().parent.parent


def _make_valid() -> dict:
    return {
        "schema_version": 1,
        "name": "test-flow",
        "description": "test multi-step\nworkflow",
        "steps": [
            {"id": "review", "actor": "claude", "intent": "review PR", "inputs": [],
             "timeout_seconds": 600, "retry_max": 0, "on_fail": "abort"},
            {"id": "implement", "actor": "codex", "intent": "implement", "inputs": ["review"],
             "timeout_seconds": 1800, "retry_max": 1, "on_fail": "abort",
             "prompt_ref": "docs/prompts/x.md"},
        ],
        "boundaries": "AQG validates schema only",
        "marker": "orchestration-helper-manifest",
    }


class TestSchema:
    def test_valid_passes(self):
        assert ore.check_orchestration_manifest(_make_valid()).is_safe

    def test_unknown_top_level(self):
        s = _make_valid(); s["leak"] = "x"
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_steps_empty_rejected(self):
        s = _make_valid(); s["steps"] = []
        assert not ore.check_orchestration_manifest(s).is_safe


class TestStepFields:
    def test_unknown_step_field(self):
        s = _make_valid()
        s["steps"][0]["unknown_field"] = "x"
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_missing_step_required(self):
        s = _make_valid()
        del s["steps"][0]["id"]
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_invalid_actor(self):
        s = _make_valid()
        s["steps"][0]["actor"] = "jeff"
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_intent_too_long(self):
        s = _make_valid()
        s["steps"][0]["intent"] = "x" * 201
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_intent_with_newline_rejected(self):
        s = _make_valid()
        s["steps"][0]["intent"] = "line1\nline2"
        assert not ore.check_orchestration_manifest(s).is_safe


class TestOnFailEnum:
    def test_abort_ok(self):
        s = _make_valid(); s["steps"][0]["on_fail"] = "abort"
        assert ore.check_orchestration_manifest(s).is_safe

    def test_continue_ok(self):
        s = _make_valid(); s["steps"][0]["on_fail"] = "continue"
        assert ore.check_orchestration_manifest(s).is_safe

    def test_retry_rejected(self):
        """3-auditor accepted: on_fail no longer accepts retry (use retry_max instead)."""
        s = _make_valid(); s["steps"][0]["on_fail"] = "retry"
        assert not ore.check_orchestration_manifest(s).is_safe


class TestRetryMax:
    def test_zero_ok(self):
        s = _make_valid(); s["steps"][0]["retry_max"] = 0
        assert ore.check_orchestration_manifest(s).is_safe

    def test_negative_rejected(self):
        s = _make_valid(); s["steps"][0]["retry_max"] = -1
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_over_max_rejected(self):
        s = _make_valid(); s["steps"][0]["retry_max"] = 99
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_bool_rejected(self):
        s = _make_valid(); s["steps"][0]["retry_max"] = True
        assert not ore.check_orchestration_manifest(s).is_safe


class TestTimeoutBounds:
    def test_in_bounds_ok(self):
        s = _make_valid(); s["steps"][0]["timeout_seconds"] = 100
        assert ore.check_orchestration_manifest(s).is_safe

    def test_too_large_rejected(self):
        """o3 #6 nit: timeout clamp."""
        s = _make_valid(); s["steps"][0]["timeout_seconds"] = 100000
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_zero_rejected(self):
        s = _make_valid(); s["steps"][0]["timeout_seconds"] = 0
        assert not ore.check_orchestration_manifest(s).is_safe


class TestStepIds:
    def test_duplicate_id_rejected(self):
        s = _make_valid()
        s["steps"][1]["id"] = "review"
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_invalid_slug_rejected(self):
        s = _make_valid()
        s["steps"][0]["id"] = "Review"
        assert not ore.check_orchestration_manifest(s).is_safe


class TestInputsAndDAG:
    def test_input_unknown_id_rejected(self):
        s = _make_valid()
        s["steps"][0]["inputs"] = ["nonexistent"]
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_self_loop_rejected(self):
        s = _make_valid()
        s["steps"][0]["inputs"] = ["review"]  # review references self
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_cycle_rejected(self):
        s = _make_valid()
        s["steps"] = [
            {"id": "a", "actor": "claude", "intent": "x", "inputs": ["b"],
             "timeout_seconds": 1, "retry_max": 0, "on_fail": "abort"},
            {"id": "b", "actor": "claude", "intent": "x", "inputs": ["a"],
             "timeout_seconds": 1, "retry_max": 0, "on_fail": "abort"},
        ]
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_duplicate_inputs_rejected(self):
        """gpt-5.5 #6: duplicate inputs in a step."""
        s = _make_valid()
        s["steps"] = [
            {"id": "a", "actor": "claude", "intent": "x", "inputs": [],
             "timeout_seconds": 1, "retry_max": 0, "on_fail": "abort"},
            {"id": "b", "actor": "claude", "intent": "x", "inputs": ["a", "a"],
             "timeout_seconds": 1, "retry_max": 0, "on_fail": "abort"},
        ]
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_too_many_inputs_rejected(self):
        s = _make_valid()
        s["steps"][1]["inputs"] = ["x"] * 30
        assert not ore.check_orchestration_manifest(s).is_safe


class TestPromptRef:
    """2-auditor accepted: optional prompt_ref field."""
    def test_relative_path_ok(self):
        s = _make_valid()
        s["steps"][1]["prompt_ref"] = "docs/prompts/foo.md"
        assert ore.check_orchestration_manifest(s).is_safe

    def test_absolute_path_rejected(self):
        s = _make_valid()
        s["steps"][1]["prompt_ref"] = "/etc/passwd"
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_parent_traversal_rejected(self):
        s = _make_valid()
        s["steps"][1]["prompt_ref"] = "docs/../etc/x.md"
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_omitted_ok(self):
        s = _make_valid()
        if "prompt_ref" in s["steps"][1]:
            del s["steps"][1]["prompt_ref"]
        assert ore.check_orchestration_manifest(s).is_safe


class TestRedactionPatterns:
    def test_token_in_intent_rejected(self):
        # Real-length ghp_ token (36 chars after prefix) — _secret_patterns is strict
        # so we use a realistic-shape fake token to assert detection.
        s = _make_valid()
        s["steps"][0]["intent"] = "use ghp_aabbccddeeff112233445566778899AABBCC in step"
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_task_runner_not_false_positive(self):
        s = _make_valid()
        s["description"] = "uses task-runner and disk-usage tools"
        assert ore.check_orchestration_manifest(s).is_safe


class TestMultilineFields:
    def test_description_newline_ok(self):
        s = _make_valid()
        s["description"] = "line1\nline2"
        assert ore.check_orchestration_manifest(s).is_safe

    def test_boundaries_newline_ok(self):
        s = _make_valid()
        s["boundaries"] = "AQG schema only\nno spawn"
        assert ore.check_orchestration_manifest(s).is_safe


class TestPostImplFixes:
    """Post-impl Deep audit (audit_id bf7e773b) 4 accepted findings regression."""

    def test_intent_dict_rejected_critical(self):
        """convergent CRITICAL: type evasion bypass — dict intent must be rejected."""
        s = _make_valid()
        s["steps"][0]["intent"] = {"raw_prompt": "ghp_LEAK"}
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_description_list_rejected_critical(self):
        s = _make_valid(); s["description"] = ["multi", "lines"]
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_boundaries_dict_rejected_critical(self):
        s = _make_valid(); s["boundaries"] = {"a": "b"}
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_dup_inputs_no_early_return(self):
        """gemini #3 major: dup inputs must not skip subsequent step field validation."""
        s = _make_valid()
        s["steps"] = [
            {"id": "a", "actor": "claude", "intent": "x", "inputs": [],
             "timeout_seconds": 1, "retry_max": 0, "on_fail": "abort"},
            {"id": "b", "actor": "claude", "intent": "x", "inputs": ["a", "a"],
             "timeout_seconds": 100000,  # invalid timeout - must still be reported
             "retry_max": 0, "on_fail": "abort"},
        ]
        result = ore.check_orchestration_manifest(s)
        assert not result.is_safe
        # Both violations should appear
        joined = " ".join(result.violations)
        assert "duplicate" in joined.lower()
        assert "timeout_seconds" in joined


class TestL1RedactionBypass:
    """L1 Deep audit 4f0c48c0 — prose fields now funnel through scan_leaks, gaining
    abs-path / email / URL / IP / auth-header / markdown-heading coverage that
    the prior token-only _check_no_secret missed (findings #5/#8)."""

    def test_abs_path_in_description_rejected(self):
        """#5 (a): an absolute POSIX path (e.g. an SSH key path) must reject."""
        s = _make_valid()
        s["description"] = "see key at /Users/x/.ssh/id_rsa for access"
        result = ore.check_orchestration_manifest(s)
        assert not result.is_safe
        assert any("absolute POSIX path" in v for v in result.violations)

    def test_email_in_description_rejected(self):
        """#5 (b): an email address (PII) must reject."""
        s = _make_valid()
        s["description"] = "ping a@b.com to escalate"
        result = ore.check_orchestration_manifest(s)
        assert not result.is_safe
        assert any("email" in v for v in result.violations)

    def test_url_in_intent_rejected(self):
        s = _make_valid()
        s["steps"][0]["intent"] = "fetch https://evil.example/x"
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_abs_path_in_boundaries_rejected(self):
        s = _make_valid()
        s["boundaries"] = "do not read /etc/shadow"
        assert not ore.check_orchestration_manifest(s).is_safe

    def test_markdown_heading_in_description_rejected(self):
        """#8: multiline prose now rejects a markdown heading line (injection guard)."""
        s = _make_valid()
        s["description"] = "intro line\n# Injected Heading"
        result = ore.check_orchestration_manifest(s)
        assert not result.is_safe
        assert any("heading" in v for v in result.violations)

    def test_task_runner_still_passes(self):
        """Regression: word-boundary belt must NOT false-positive 'sk-' in 'task-runner'."""
        s = _make_valid()
        s["description"] = "uses task-runner and disk-usage tools\nsecond line"
        assert ore.check_orchestration_manifest(s).is_safe


class TestEnumTypeGuard:
    """#7: enum membership on a non-str / non-hashable value must not raise
    TypeError — it must append a violation and return a Result."""

    def test_non_str_actor_no_crash(self):
        s = _make_valid()
        s["steps"][0]["actor"] = ["claude"]  # list is non-hashable
        result = ore.check_orchestration_manifest(s)
        assert not result.is_safe
        assert any("actor" in v for v in result.violations)

    def test_non_str_on_fail_no_crash(self):
        s = _make_valid()
        s["steps"][0]["on_fail"] = {"x": 1}  # dict is non-hashable
        result = ore.check_orchestration_manifest(s)
        assert not result.is_safe
        assert any("on_fail" in v for v in result.violations)

    def test_non_str_top_actor_no_crash(self):
        s = _make_valid()
        s["actor"] = {"a": "b"}
        result = ore.check_orchestration_manifest(s)
        assert not result.is_safe
        assert any("actor" in v for v in result.violations)

    def test_non_str_marker_no_crash(self):
        s = _make_valid()
        s["marker"] = ["x"]
        result = ore.check_orchestration_manifest(s)
        assert not result.is_safe
        assert any("marker" in v for v in result.violations)


class TestRaiseAPI:
    def test_assert_safe_passes(self):
        ore.assert_safe_orchestration_manifest(_make_valid())

    def test_assert_safe_raises(self):
        with pytest.raises(ore.OrchestrationRedactionError):
            ore.assert_safe_orchestration_manifest({"schema_version": 99})


class TestCLIValidator:
    def test_sample_validates(self):
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "validate_orchestration_manifest.py"),
             str(REPO / "docs" / "manifests" / "orchestration" / "_example_clean.json")],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0

    def test_invalid_returns_1(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schema_version": 99}))
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "validate_orchestration_manifest.py"), str(bad)],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 1
