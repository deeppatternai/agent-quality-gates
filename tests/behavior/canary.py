"""Canary fixture verification for stream-json schema drift detection.

Sketch v3 §7 + audit 717918e8 finding #6 + audit 32f288ca: canary fixture is a
historical known-good stream-json file with at least one Skill call. Run
extractor against canary at start of every nightly run; if extractor returns
zero skills (against an input that previously yielded one), upstream Claude
Code stream-json schema has silently drifted → SCHEMA_VERSION_MISMATCH.

Distinguishes from PARSE_ERROR (JSONL syntactically broken) and SCHEMA_ERROR
(init missing): canary parses + structure intact, but Skill activation no
longer expressed as `tool_use { name: "Skill" }`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tests.behavior.extractor import extract_skill_calls


@dataclass(frozen=True)
class CanaryCheckResult:
    """Outcome of canary verification."""

    canary_passed: bool
    canary_path: Path
    expected_skill_names: list[str]
    actual_skill_names: list[str]
    reason: str  # human-readable diagnostic


def run_canary_check(
    canary_jsonl_path: Path,
    expected_path: Path,
) -> CanaryCheckResult:
    """Run extractor against canary fixture; verify exact expected Skill calls.

    Args:
        canary_jsonl_path: stream-json fixture file (sanitized historical capture).
        expected_path: JSON file with `{"expected_skill_names": [...]}` — list
            for exact-equality check (audit d982c3c3 #3: membership check is too
            permissive against extractor regressions).

    Returns:
        CanaryCheckResult; canary_passed=False = extractor result diverges from
        baseline. Possible causes (audit d982c3c3 #4): (a) upstream Claude Code
        stream-json schema drift, (b) extractor regression, (c) fixture
        corruption — investigate distinguishingly.
    """
    if not canary_jsonl_path.is_file():
        return CanaryCheckResult(
            canary_passed=False,
            canary_path=canary_jsonl_path,
            expected_skill_names=[],
            actual_skill_names=[],
            reason=f"canary fixture not found: {canary_jsonl_path}",
        )
    if not expected_path.is_file():
        return CanaryCheckResult(
            canary_passed=False,
            canary_path=canary_jsonl_path,
            expected_skill_names=[],
            actual_skill_names=[],
            reason=f"canary expected.json not found: {expected_path}",
        )

    try:
        expected_data = json.loads(expected_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return CanaryCheckResult(
            canary_passed=False,
            canary_path=canary_jsonl_path,
            expected_skill_names=[],
            actual_skill_names=[],
            reason=f"canary expected.json malformed: {exc}",
        )

    expected_skill_names = expected_data.get("expected_skill_names")
    if not isinstance(expected_skill_names, list) or not expected_skill_names:
        return CanaryCheckResult(
            canary_passed=False,
            canary_path=canary_jsonl_path,
            expected_skill_names=[],
            actual_skill_names=[],
            reason=(
                "canary expected.json missing or empty 'expected_skill_names' "
                "(must be a non-empty list of skill names for exact-equality match)"
            ),
        )

    top_level_calls, _diag = extract_skill_calls(canary_jsonl_path)
    actual_skill_names = [c["skill_name"] for c in top_level_calls]

    # Exact-equality (sorted) — audit d982c3c3 #3 fix
    if sorted(expected_skill_names) == sorted(actual_skill_names):
        return CanaryCheckResult(
            canary_passed=True,
            canary_path=canary_jsonl_path,
            expected_skill_names=expected_skill_names,
            actual_skill_names=actual_skill_names,
            reason="canary skill calls extracted exactly as expected",
        )

    return CanaryCheckResult(
        canary_passed=False,
        canary_path=canary_jsonl_path,
        expected_skill_names=expected_skill_names,
        actual_skill_names=actual_skill_names,
        reason=(
            f"canary mismatch: expected {sorted(expected_skill_names)!r}, got "
            f"{sorted(actual_skill_names)!r}. Possible causes (investigate "
            "distinguishingly): (a) upstream Claude Code stream-json schema "
            "drift — re-run new spike with same prompt, diff against canary, "
            "bump SCHEMA_VERSION + update extractor; (b) extractor regression "
            "— bisect recent extractor.py changes; (c) fixture corruption — "
            "verify canary jsonl + expected.json bytes unchanged."
        ),
    )
