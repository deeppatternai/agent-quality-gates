"""Unit tests for tests/behavior/extractor.py.

Inline minimal stream-json fixtures (sanitized; not real spike output).
Validates extractor algorithm against schema v1 §2.2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.behavior.extractor import (
    SCHEMA_VERSION,
    extract_skill_calls,
    normalize_skill_name,
)


# ===== Helpers =====


def write_jsonl(tmp_path: Path, events: list[dict]) -> Path:
    """Write list of events to JSONL file at tmp_path/stream.jsonl."""
    p = tmp_path / "stream.jsonl"
    p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events))
    return p


def init_event(skills: list[str] | None = None) -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "skills": skills or [],
    }


def assistant_skill_call(
    skill_name: str,
    parent_tool_use_id: str | None = None,
    caller_type: str = "direct",
    args: str | None = None,
    tool_use_id: str = "toolu_test_001",
) -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": parent_tool_use_id,
        "message": {
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Skill",
                    "input": {"skill": skill_name, **({"args": args} if args else {})},
                    "caller": {"type": caller_type},
                }
            ],
        },
    }


def result_event(
    is_error: bool = False,
    subtype: str = "success",
    api_error_status: int | None = None,
    total_cost_usd: float = 0.10,
    num_turns: int = 4,
    duration_ms: int = 30000,
) -> dict:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "api_error_status": api_error_status,
        "total_cost_usd": total_cost_usd,
        "num_turns": num_turns,
        "duration_ms": duration_ms,
    }


# ===== normalize_skill_name =====


class TestNormalize:
    def test_basic(self):
        assert normalize_skill_name("aqg-startup-preflight") == "aqg-startup-preflight"

    def test_underscore_to_hyphen(self):
        assert normalize_skill_name("AQG_Startup_Preflight") == "aqg-startup-preflight"

    def test_whitespace_to_hyphen(self):
        assert normalize_skill_name("aqg startup preflight") == "aqg-startup-preflight"

    def test_strip_outer_whitespace(self):
        assert normalize_skill_name("  aqg-foo  ") == "aqg-foo"

    def test_empty(self):
        assert normalize_skill_name("") == ""
        assert normalize_skill_name("  ") == ""


# ===== extract_skill_calls =====


class TestExtractSkillCalls:
    def test_schema_version_constant(self):
        assert SCHEMA_VERSION == 1

    def test_missing_file_returns_empty(self, tmp_path):
        nonexistent = tmp_path / "nope.jsonl"
        calls, diag = extract_skill_calls(nonexistent)
        assert calls == []
        assert diag["total_nonempty_lines"] == 0
        assert diag["init_event_present"] is False
        assert diag["result_event_present"] is False

    def test_top_level_skill_extracted(self, tmp_path):
        events = [
            init_event(skills=["aqg-startup-preflight", "other-skill"]),
            assistant_skill_call("aqg-startup-preflight"),
            result_event(),
        ]
        jsonl = write_jsonl(tmp_path, events)
        calls, diag = extract_skill_calls(jsonl)

        assert len(calls) == 1
        assert calls[0]["skill_name"] == "aqg-startup-preflight"
        assert calls[0]["caller_type"] == "direct"
        assert calls[0]["tool_use_id"] == "toolu_test_001"
        assert diag["init_event_present"] is True
        assert "aqg-startup-preflight" in diag["init_skills_loaded"]
        assert diag["result_event_present"] is True
        assert diag["result_is_error"] is False
        assert diag["result_subtype"] == "success"
        assert diag["delegated_skill_calls"] == []

    def test_delegated_skill_routed_to_diagnostics(self, tmp_path):
        events = [
            init_event(skills=["aqg-startup-preflight"]),
            assistant_skill_call(
                "aqg-startup-preflight",
                parent_tool_use_id="toolu_parent_001",
                tool_use_id="toolu_child_001",
            ),
            result_event(),
        ]
        jsonl = write_jsonl(tmp_path, events)
        calls, diag = extract_skill_calls(jsonl)

        # top_level_calls should be empty
        assert calls == []
        # Delegated call surfaced in diag
        assert len(diag["delegated_skill_calls"]) == 1
        assert diag["delegated_skill_calls"][0]["skill_name"] == "aqg-startup-preflight"
        assert diag["delegated_skill_calls"][0]["tool_use_id"] == "toolu_child_001"

    def test_mixed_top_and_delegated(self, tmp_path):
        events = [
            init_event(skills=["aqg-startup-preflight", "aqg-systematic-debugging"]),
            assistant_skill_call("aqg-startup-preflight", tool_use_id="toolu_top"),
            assistant_skill_call(
                "aqg-systematic-debugging",
                parent_tool_use_id="toolu_top",
                tool_use_id="toolu_sub",
            ),
            result_event(),
        ]
        jsonl = write_jsonl(tmp_path, events)
        calls, diag = extract_skill_calls(jsonl)

        assert len(calls) == 1
        assert calls[0]["skill_name"] == "aqg-startup-preflight"
        assert len(diag["delegated_skill_calls"]) == 1
        assert diag["delegated_skill_calls"][0]["skill_name"] == "aqg-systematic-debugging"

    def test_non_skill_tool_use_ignored(self, tmp_path):
        events = [
            init_event(skills=["aqg-startup-preflight"]),
            {
                "type": "assistant",
                "parent_tool_use_id": None,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_bash",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ],
                },
            },
            result_event(),
        ]
        jsonl = write_jsonl(tmp_path, events)
        calls, diag = extract_skill_calls(jsonl)
        assert calls == []
        assert diag["init_event_present"] is True
        assert diag["result_event_present"] is True

    def test_text_content_ignored(self, tmp_path):
        events = [
            init_event(skills=["aqg-startup-preflight"]),
            {
                "type": "assistant",
                "parent_tool_use_id": None,
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me think about this"}
                    ],
                },
            },
            result_event(),
        ]
        jsonl = write_jsonl(tmp_path, events)
        calls, _ = extract_skill_calls(jsonl)
        assert calls == []

    def test_malformed_skill_block_skipped(self, tmp_path):
        events = [
            init_event(skills=["aqg-startup-preflight"]),
            # Skill block with no skill name in input
            {
                "type": "assistant",
                "parent_tool_use_id": None,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_bad",
                            "name": "Skill",
                            "input": {},  # no "skill" field
                        }
                    ],
                },
            },
            result_event(),
        ]
        jsonl = write_jsonl(tmp_path, events)
        calls, _ = extract_skill_calls(jsonl)
        assert calls == []

    def test_json_decode_error_counted(self, tmp_path):
        # Manually write a stream with malformed line
        p = tmp_path / "stream.jsonl"
        p.write_text(
            json.dumps(init_event(skills=["aqg-startup-preflight"]))
            + "\n"
            + "not json at all\n"
            + json.dumps(assistant_skill_call("aqg-startup-preflight"))
            + "\n"
            + json.dumps(result_event())
            + "\n"
        )
        calls, diag = extract_skill_calls(p)

        assert len(calls) == 1
        assert diag["json_decode_errors"] == 1
        assert diag["total_nonempty_lines"] == 4
        assert diag["parse_error_rate"] == pytest.approx(0.25)

    def test_empty_lines_skipped(self, tmp_path):
        p = tmp_path / "stream.jsonl"
        p.write_text(
            json.dumps(init_event(skills=["aqg-startup-preflight"]))
            + "\n\n\n"
            + json.dumps(result_event())
            + "\n"
        )
        _, diag = extract_skill_calls(p)
        assert diag["total_nonempty_lines"] == 2
        assert diag["json_decode_errors"] == 0

    def test_result_event_captures_termination_metadata(self, tmp_path):
        events = [
            init_event(skills=["aqg-startup-preflight"]),
            result_event(
                is_error=True,
                subtype="error_max_budget_usd",
                total_cost_usd=0.067,
                num_turns=1,
                duration_ms=4937,
            ),
        ]
        jsonl = write_jsonl(tmp_path, events)
        _, diag = extract_skill_calls(jsonl)

        assert diag["result_is_error"] is True
        assert diag["result_subtype"] == "error_max_budget_usd"
        assert diag["result_total_cost_usd"] == 0.067
        assert diag["result_num_turns"] == 1
        assert diag["result_duration_ms"] == 4937

    def test_invalid_model_404_captured(self, tmp_path):
        events = [
            init_event(skills=[]),
            result_event(
                is_error=True,
                subtype="success",  # Q-Spike-8.2 confirmed: subtype="success" but is_error=true
                api_error_status=404,
                total_cost_usd=0,
            ),
        ]
        jsonl = write_jsonl(tmp_path, events)
        _, diag = extract_skill_calls(jsonl)

        assert diag["result_api_error_status"] == 404
        assert diag["result_is_error"] is True

    def test_no_result_event_simulates_timeout(self, tmp_path):
        # SIGTERM/wall-clock kill leaves stream truncated without result event
        events = [
            init_event(skills=["aqg-startup-preflight"]),
            assistant_skill_call("aqg-startup-preflight"),
        ]
        jsonl = write_jsonl(tmp_path, events)
        calls, diag = extract_skill_calls(jsonl)

        assert len(calls) == 1
        assert diag["result_event_present"] is False
        assert diag["init_event_present"] is True
