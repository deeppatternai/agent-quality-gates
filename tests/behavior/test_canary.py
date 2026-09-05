"""Unit tests for tests/behavior/canary.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.behavior.canary import CanaryCheckResult, run_canary_check


# ===== Fixtures =====


def write_canary_pair(
    tmp_path: Path,
    skill_name: str = "aqg-startup-preflight",
    include_skill: bool = True,
):
    """Write a minimal stream-json canary + expected.json pair."""
    jsonl_path = tmp_path / "canary.jsonl"
    expected_path = tmp_path / "canary.expected.json"

    init_event = {
        "type": "system",
        "subtype": "init",
        "skills": [skill_name],
    }
    result_event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "total_cost_usd": 0.05,
        "num_turns": 1,
    }
    events = [init_event]
    if include_skill:
        events.append({
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_canary",
                        "name": "Skill",
                        "input": {"skill": skill_name},
                        "caller": {"type": "direct"},
                    }
                ],
            },
        })
    events.append(result_event)

    jsonl_path.write_text("\n".join(json.dumps(e) for e in events))
    expected_path.write_text(json.dumps({"expected_skill_names": [skill_name]}))
    return jsonl_path, expected_path


# ===== run_canary_check =====


class TestCanaryHappy:
    def test_canary_passes_when_skill_extracted(self, tmp_path):
        jsonl, expected = write_canary_pair(tmp_path)
        result = run_canary_check(jsonl, expected)

        assert isinstance(result, CanaryCheckResult)
        assert result.canary_passed is True
        assert result.expected_skill_names == ["aqg-startup-preflight"]
        assert "aqg-startup-preflight" in result.actual_skill_names

    def test_immutable_dataclass(self, tmp_path):
        jsonl, expected = write_canary_pair(tmp_path)
        result = run_canary_check(jsonl, expected)
        with pytest.raises((AttributeError, Exception)):
            result.canary_passed = False  # type: ignore[misc]


class TestCanaryFail:
    def test_canary_fails_when_skill_missing(self, tmp_path):
        # Stream has no Skill call → mismatch detected
        jsonl, expected = write_canary_pair(tmp_path, include_skill=False)
        result = run_canary_check(jsonl, expected)

        assert result.canary_passed is False
        assert result.actual_skill_names == []
        assert "mismatch" in result.reason.lower()
        # Error message must distinguish 3 possible causes (audit d982c3c3 #4)
        assert "schema drift" in result.reason
        assert "extractor regression" in result.reason
        assert "fixture corruption" in result.reason

    def test_canary_fails_when_different_skill(self, tmp_path):
        jsonl, expected = write_canary_pair(
            tmp_path, skill_name="aqg-startup-preflight"
        )
        # Tamper expected to claim a different skill
        expected.write_text(
            json.dumps({"expected_skill_names": ["aqg-evidence-closeout"]})
        )
        result = run_canary_check(jsonl, expected)

        assert result.canary_passed is False
        assert "aqg-evidence-closeout" not in result.actual_skill_names

    def test_canary_fails_when_extra_skill_present(self, tmp_path):
        # audit d982c3c3 #3: exact equality, not membership
        # Build a stream with TWO skill calls but expected has only one
        from tests.behavior.test_canary import write_canary_pair
        jsonl_path = tmp_path / "canary.jsonl"
        expected_path = tmp_path / "canary.expected.json"

        events = [
            {"type": "system", "subtype": "init", "skills": ["aqg-startup-preflight", "aqg-systematic-debugging"]},
            {
                "type": "assistant", "parent_tool_use_id": None,
                "message": {"content": [
                    {"type": "tool_use", "id": "tu1", "name": "Skill",
                     "input": {"skill": "aqg-startup-preflight"},
                     "caller": {"type": "direct"}},
                ]},
            },
            {
                "type": "assistant", "parent_tool_use_id": None,
                "message": {"content": [
                    {"type": "tool_use", "id": "tu2", "name": "Skill",
                     "input": {"skill": "aqg-systematic-debugging"},
                     "caller": {"type": "direct"}},
                ]},
            },
            {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.05, "num_turns": 2},
        ]
        jsonl_path.write_text("\n".join(json.dumps(e) for e in events))
        # Expected only the first one — would pass under old `in` membership; must FAIL under exact equality
        expected_path.write_text(json.dumps({"expected_skill_names": ["aqg-startup-preflight"]}))

        result = run_canary_check(jsonl_path, expected_path)
        assert result.canary_passed is False, "exact-equality must reject extra extracted skills"

    def test_missing_canary_file(self, tmp_path):
        result = run_canary_check(
            tmp_path / "nope.jsonl",
            tmp_path / "nope.expected.json",
        )
        assert result.canary_passed is False
        assert "not found" in result.reason

    def test_missing_expected_file(self, tmp_path):
        jsonl, expected = write_canary_pair(tmp_path)
        expected.unlink()
        result = run_canary_check(jsonl, expected)
        assert result.canary_passed is False
        assert "expected.json not found" in result.reason

    def test_malformed_expected_json(self, tmp_path):
        jsonl, expected = write_canary_pair(tmp_path)
        expected.write_text("not json {")
        result = run_canary_check(jsonl, expected)
        assert result.canary_passed is False
        assert "malformed" in result.reason

    def test_expected_missing_skill_names_key(self, tmp_path):
        jsonl, expected = write_canary_pair(tmp_path)
        expected.write_text(json.dumps({"other_key": "value"}))
        result = run_canary_check(jsonl, expected)
        assert result.canary_passed is False
        assert "expected_skill_names" in result.reason

    def test_expected_skill_names_must_be_nonempty_list(self, tmp_path):
        jsonl, expected = write_canary_pair(tmp_path)
        expected.write_text(json.dumps({"expected_skill_names": []}))
        result = run_canary_check(jsonl, expected)
        assert result.canary_passed is False
        assert "non-empty list" in result.reason

    def test_expected_skill_names_string_rejected(self, tmp_path):
        # Old single-name string format must be explicitly rejected
        jsonl, expected = write_canary_pair(tmp_path)
        expected.write_text(json.dumps({"expected_skill_names": "aqg-startup-preflight"}))
        result = run_canary_check(jsonl, expected)
        assert result.canary_passed is False
        assert "non-empty list" in result.reason


# ===== Integration: real canary fixture file =====


class TestRealCanaryFixture:
    """Verify the actual canary fixture committed to the repo passes."""

    REPO_ROOT = Path(__file__).resolve().parents[2]

    def test_real_preflight_canary_passes(self):
        canary_jsonl = (
            self.REPO_ROOT
            / "tests"
            / "behavior"
            / "fixtures"
            / "canary"
            / "preflight_known_skill.jsonl"
        )
        expected = (
            self.REPO_ROOT
            / "tests"
            / "behavior"
            / "fixtures"
            / "canary"
            / "preflight_known_skill.expected.json"
        )
        result = run_canary_check(canary_jsonl, expected)
        assert result.canary_passed is True, (
            f"Real canary fixture failed; if upstream Claude Code stream-json "
            f"schema changed, bump SCHEMA_VERSION + regenerate canary. "
            f"reason: {result.reason}"
        )
