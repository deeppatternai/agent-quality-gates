"""Skill call extractor for Claude Code stream-json output.

Implements algorithm from `tests/behavior/fixtures/stream_json_schema_v1.md` §2.2.

Returns (top_level_skill_calls, diagnostics) tuple:
- top_level_skill_calls: only Skill tool_use blocks where parent_tool_use_id is None
  (sub-agent delegated calls go to diagnostics.delegated_skill_calls instead).
- diagnostics: parse stats + init/result event metadata for status decision tree.

Schema version: 1 (Claude Code v2.1.119, AQG v0.4.x).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def normalize_skill_name(name: str) -> str:
    """Normalize skill name for comparison (per schema v1 §2.3).

    Lowercase + underscore/whitespace -> hyphen. Strip whitespace.
    """
    if not name:
        return ""
    return name.strip().lower().replace("_", "-").replace(" ", "-")


def extract_skill_calls(jsonl_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract Skill tool_use calls from stream-json output.

    Args:
        jsonl_path: path to claude -p --output-format stream-json output file.

    Returns:
        (top_level_skill_calls, diagnostics) tuple.
        top_level_skill_calls: list of {skill_name, tool_use_id, caller_type, args}
            only for parent_tool_use_id is None (top-level direct calls).
        diagnostics: see schema_v1.md §2.2 docstring.
    """
    top_level_calls: list[dict[str, Any]] = []
    delegated_calls: list[dict[str, Any]] = []
    diag: dict[str, Any] = {
        "total_nonempty_lines": 0,
        "json_decode_errors": 0,
        "parse_error_rate": 0.0,
        "delegated_skill_calls": [],
        "init_event_present": False,
        "init_skills_loaded": None,
        "result_event_present": False,
        "result_is_error": None,
        "result_subtype": None,
        "result_api_error_status": None,
        "result_total_cost_usd": None,
        "result_num_turns": None,
        "result_duration_ms": None,
    }

    if not jsonl_path.exists():
        return top_level_calls, diag

    for line in jsonl_path.read_text().splitlines():
        if not line.strip():
            continue
        diag["total_nonempty_lines"] += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            diag["json_decode_errors"] += 1
            continue
        # Guard against valid JSON that's not an object (audit be5e71f8 gemini #3)
        if not isinstance(event, dict):
            continue

        evtype = event.get("type")

        if evtype == "system" and event.get("subtype") == "init":
            diag["init_event_present"] = True
            diag["init_skills_loaded"] = event.get("skills", [])
            continue

        if evtype == "result":
            diag["result_event_present"] = True
            diag["result_is_error"] = event.get("is_error", False)
            diag["result_subtype"] = event.get("subtype")
            diag["result_api_error_status"] = event.get("api_error_status")
            diag["result_total_cost_usd"] = event.get("total_cost_usd")
            diag["result_num_turns"] = event.get("num_turns")
            diag["result_duration_ms"] = event.get("duration_ms")
            continue

        if evtype != "assistant":
            continue

        is_delegated = event.get("parent_tool_use_id") is not None
        message = event.get("message")
        # Guard against malformed message (audit be5e71f8 gpt-5.5 #4)
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use" or block.get("name") != "Skill":
                continue
            input_obj = block.get("input")
            # Guard against malformed input (audit be5e71f8 gpt-5.5 #4)
            if not isinstance(input_obj, dict):
                continue
            raw_skill = input_obj.get("skill")
            if not isinstance(raw_skill, str) or not raw_skill:
                continue
            # Apply normalization at extraction (audit be5e71f8 gemini #1: normalize_skill_name was dead code)
            skill_name = normalize_skill_name(raw_skill)
            if not skill_name:
                continue
            caller = block.get("caller") if isinstance(block.get("caller"), dict) else {}
            call_record = {
                "skill_name": skill_name,
                "tool_use_id": block.get("id"),
                "caller_type": caller.get("type"),
                "args": input_obj.get("args"),
            }
            if is_delegated:
                delegated_calls.append(call_record)
            else:
                top_level_calls.append(call_record)

    diag["delegated_skill_calls"] = delegated_calls
    if diag["total_nonempty_lines"] > 0:
        diag["parse_error_rate"] = diag["json_decode_errors"] / diag["total_nonempty_lines"]

    return top_level_calls, diag
