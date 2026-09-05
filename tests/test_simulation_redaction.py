"""Tests for scripts/_simulation_redaction.py + validate_simulation_manifest.py."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _simulation_redaction as sr  # noqa: E402


REPO = Path(__file__).resolve().parent.parent


def _make_valid() -> dict:
    return {
        "schema_version": 1,
        "name": "test-sim",
        "description": "test simulation\nmulti line",
        "kind": "api",
        "duration_seconds": 100,
        "seed": 42,
        "parameters": {"failure_rate_percent": 30, "targets": ["a", "b"]},
        "expected_artifacts": ["docs/sim/x.log"],
        "boundaries": "no production",
        "actor": "claude",
        "marker": "simulation-mock-manifest",
    }


class TestSchema:
    def test_valid_passes(self):
        assert sr.check_simulation_manifest(_make_valid()).is_safe

    def test_unknown_top_level(self):
        s = _make_valid(); s["leak_field"] = "x"
        assert not sr.check_simulation_manifest(s).is_safe

    def test_required_missing(self):
        s = _make_valid(); del s["kind"]
        assert not sr.check_simulation_manifest(s).is_safe

    def test_required_null(self):
        s = _make_valid(); s["kind"] = None
        assert not sr.check_simulation_manifest(s).is_safe


class TestStrictTypes:
    def test_bool_as_schema_version_rejected(self):
        s = _make_valid(); s["schema_version"] = True
        assert not sr.check_simulation_manifest(s).is_safe

    def test_bool_as_duration_rejected(self):
        s = _make_valid(); s["duration_seconds"] = True
        assert not sr.check_simulation_manifest(s).is_safe

    def test_nan_in_parameter_rejected(self):
        s = _make_valid(); s["parameters"] = {"k": math.nan}
        assert not sr.check_simulation_manifest(s).is_safe

    def test_inf_in_parameter_rejected(self):
        s = _make_valid(); s["parameters"] = {"k": math.inf}
        assert not sr.check_simulation_manifest(s).is_safe


class TestParameters:
    def test_scalar_values_ok(self):
        s = _make_valid(); s["parameters"] = {"a": 1, "b": "x", "c": True, "d": 1.5}
        assert sr.check_simulation_manifest(s).is_safe

    def test_list_of_scalar_ok(self):
        """3-auditor accepted: allow list of scalar."""
        s = _make_valid(); s["parameters"] = {"targets": ["a", "b", "c"]}
        assert sr.check_simulation_manifest(s).is_safe

    def test_nested_dict_rejected(self):
        s = _make_valid(); s["parameters"] = {"k": {"nested": 1}}
        assert not sr.check_simulation_manifest(s).is_safe

    def test_list_of_dict_rejected(self):
        s = _make_valid(); s["parameters"] = {"k": [{"a": 1}]}
        assert not sr.check_simulation_manifest(s).is_safe

    def test_sensitive_key_rejected(self):
        """gpt-5.5 #1 critical: sensitive key denylist."""
        for key in ["password", "secret", "TOKEN", "api_key", "credential", "dsn"]:
            s = _make_valid(); s["parameters"] = {key: "x"}
            assert not sr.check_simulation_manifest(s).is_safe, f"key {key} should be rejected"


class TestRedactionPatterns:
    def test_token_in_description_rejected(self):
        """uses _secret_patterns shared regex (which requires real-length tokens)."""
        s = _make_valid(); s["description"] = "found ghp_aabbccddeeff112233445566778899AABBCC in code"
        assert not sr.check_simulation_manifest(s).is_safe

    def test_task_runner_not_false_positive(self):
        """gemini #1 critical: 'sk-' regex must use word boundary, not match 'task-runner'."""
        s = _make_valid(); s["description"] = "this involves task-runner with disk-usage tools"
        assert sr.check_simulation_manifest(s).is_safe

    def test_disk_usage_not_false_positive(self):
        s = _make_valid(); s["boundaries"] = "uses disk-usage stats; no real secrets"
        assert sr.check_simulation_manifest(s).is_safe


class TestMultilineFields:
    def test_description_newline_ok(self):
        """gemini #2: multiline fields allow \\n."""
        s = _make_valid(); s["description"] = "line1\nline2\nline3"
        assert sr.check_simulation_manifest(s).is_safe

    def test_description_control_char_rejected(self):
        s = _make_valid(); s["description"] = "ok\x00"
        assert not sr.check_simulation_manifest(s).is_safe

    def test_name_inline_no_newline(self):
        s = _make_valid(); s["name"] = "test\nbad"
        assert not sr.check_simulation_manifest(s).is_safe


class TestPathValidation:
    def test_absolute_path_artifact_rejected(self):
        s = _make_valid(); s["expected_artifacts"] = ["/etc/passwd"]
        assert not sr.check_simulation_manifest(s).is_safe

    def test_parent_traversal_rejected(self):
        """o3 #2: explicit reject .. parent."""
        s = _make_valid(); s["expected_artifacts"] = ["docs/../etc/x"]
        assert not sr.check_simulation_manifest(s).is_safe


class TestPostImplFixes:
    """Post-impl Deep audit (audit_id bf7e773b) 4 accepted findings regression."""

    def test_description_dict_rejected_critical(self):
        """convergent CRITICAL: type evasion bypass — dict description must be rejected."""
        s = _make_valid(); s["description"] = {"raw_prompt": "ghp_LEAK"}
        assert not sr.check_simulation_manifest(s).is_safe

    def test_boundaries_list_rejected_critical(self):
        s = _make_valid(); s["boundaries"] = ["item1", "item2"]
        assert not sr.check_simulation_manifest(s).is_safe

    def test_secret_in_artifact_path_rejected(self):
        """gpt-5.5 #3 major: paths now also run secret check (belt-and-suspenders).

        Use a real-shape ghp_ token (36 chars after prefix) since _secret_patterns
        validates by length.
        """
        s = _make_valid()
        s["expected_artifacts"] = ["docs/ghp_aabbccddeeff112233445566778899AABBCC.log"]
        assert not sr.check_simulation_manifest(s).is_safe


class TestL1RedactionBypass:
    """L1 Deep audit 4f0c48c0 — prose fields + parameter string values now funnel
    through scan_leaks, gaining abs-path / email / URL / IP / auth-header /
    markdown-heading coverage the prior token-only _check_no_secret missed
    (findings #4/#8)."""

    def test_abs_path_in_description_rejected(self):
        s = _make_valid()
        s["description"] = "key at /Users/x/.ssh/id_rsa"
        result = sr.check_simulation_manifest(s)
        assert not result.is_safe
        assert any("absolute POSIX path" in v for v in result.violations)

    def test_email_in_description_rejected(self):
        s = _make_valid()
        s["description"] = "owner a@b.com"
        result = sr.check_simulation_manifest(s)
        assert not result.is_safe
        assert any("email" in v for v in result.violations)

    def test_abs_path_in_parameter_value_rejected(self):
        """parameter string values are inline prose now → path/PII scanned."""
        s = _make_valid()
        s["parameters"] = {"endpoint": "/Users/x/.aws/credentials"}
        assert not sr.check_simulation_manifest(s).is_safe

    def test_email_in_parameter_value_rejected(self):
        s = _make_valid()
        s["parameters"] = {"contact": "ops@b.com"}
        assert not sr.check_simulation_manifest(s).is_safe

    def test_markdown_heading_in_boundaries_rejected(self):
        """#8: multiline prose now rejects markdown heading lines."""
        s = _make_valid()
        s["boundaries"] = "scope line\n## Injected"
        result = sr.check_simulation_manifest(s)
        assert not result.is_safe
        assert any("heading" in v for v in result.violations)

    def test_task_runner_still_passes(self):
        """Regression: word-boundary belt must NOT false-positive 'sk-' in 'task-runner'."""
        s = _make_valid()
        s["description"] = "task-runner with disk-usage tools\nsecond line"
        assert sr.check_simulation_manifest(s).is_safe


class TestSensitiveKeySubstring:
    """#14: SENSITIVE_PARAM_KEYS now matches as a substring, so prefixed /
    suffixed / versioned keys no longer bypass the denylist."""

    def test_db_password_rejected(self):
        """(c): 'db_password' contains 'password' → reject via substring match."""
        s = _make_valid()
        s["parameters"] = {"db_password": "x"}
        result = sr.check_simulation_manifest(s)
        assert not result.is_safe
        assert any("sensitive key name" in v for v in result.violations)

    def test_user_token_rejected(self):
        s = _make_valid()
        s["parameters"] = {"user_token": "x"}
        assert not sr.check_simulation_manifest(s).is_safe

    def test_api_key_versioned_rejected(self):
        s = _make_valid()
        s["parameters"] = {"api_key_v2": "x"}
        assert not sr.check_simulation_manifest(s).is_safe

    def test_benign_key_still_passes(self):
        """Substring tightening must not over-reject an innocuous key."""
        s = _make_valid()
        s["parameters"] = {"failure_rate_percent": 30, "targets": ["a", "b"]}
        assert sr.check_simulation_manifest(s).is_safe


class TestEnumTypeGuard:
    """#7: enum membership on a non-str / non-hashable value must not raise
    TypeError — it must append a violation and return a Result."""

    def test_non_str_kind_no_crash(self):
        """(d): a non-str enum value returns a Result, does not crash."""
        s = _make_valid()
        s["kind"] = ["api"]  # list is non-hashable
        result = sr.check_simulation_manifest(s)
        assert not result.is_safe
        assert any("kind" in v for v in result.violations)

    def test_non_str_actor_no_crash(self):
        s = _make_valid()
        s["actor"] = {"a": "b"}
        result = sr.check_simulation_manifest(s)
        assert not result.is_safe
        assert any("actor" in v for v in result.violations)

    def test_non_str_marker_no_crash(self):
        s = _make_valid()
        s["marker"] = ["x"]
        result = sr.check_simulation_manifest(s)
        assert not result.is_safe
        assert any("marker" in v for v in result.violations)


class TestRaiseAPI:
    def test_assert_safe_passes(self):
        sr.assert_safe_simulation_manifest(_make_valid())

    def test_assert_safe_raises(self):
        with pytest.raises(sr.SimulationRedactionError):
            sr.assert_safe_simulation_manifest({"schema_version": 99})


class TestCLIValidator:
    def test_sample_validates(self):
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "validate_simulation_manifest.py"),
             str(REPO / "docs" / "manifests" / "simulation" / "_example_clean.json")],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0

    def test_invalid_returns_1(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schema_version": 99}))
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "validate_simulation_manifest.py"), str(bad)],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 1

    def test_missing_file_returns_2(self):
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "validate_simulation_manifest.py"), "/nonexistent.json"],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 2
