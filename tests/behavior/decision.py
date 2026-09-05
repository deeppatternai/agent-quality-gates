"""Status decision tree for behavior test results.

Implements ordered decision tree from `tests/behavior/fixtures/stream_json_schema_v1.md` §3
+ sketch v3 §3.4 (Q-Spike-8 termination shapes mapping).

Order matters: parse/schema -> infra (no result) -> auth -> model -> cost -> infra (other) -> behavior.

Status enum (10 values, schema v1 §3): see CaseStatus below.

Gating: only BEHAVIOR_PASS / BEHAVIOR_FAIL / OVER_TRIGGER count toward Q-Open-7 promote;
others are excluded (surface to maintainer but not pollute metrics).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

# Cost guard default (sketch v3 §3.1 baseline; per-run upper bound)
DEFAULT_PER_RUN_BUDGET_USD = 0.50


class CaseStatus(str, Enum):
    BEHAVIOR_PASS = "behavior_pass"
    BEHAVIOR_FAIL = "behavior_fail"
    OVER_TRIGGER = "over_trigger"
    INFRA_ERROR = "infra_error"
    PARSE_ERROR = "parse_error"
    SCHEMA_ERROR = "schema_error"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"
    AUTH_ERROR = "auth_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    COST_EXCEEDED = "cost_exceeded"
    DELEGATED_TRIGGER = "delegated_trigger"
    DRIFT_DETECTED = "drift_detected"
    SKIPPED = "skipped"


GATING_PASS = {CaseStatus.BEHAVIOR_PASS}
# DELEGATED_TRIGGER counts as FAIL (audit be5e71f8 gpt-5.5 #2): Q3 #10 tests the
# top-level direct trigger; the expected skill triggering only inside a sub-agent equals fail.
# DRIFT_DETECTED counts as FAIL (audit d982c3c3 #5): SKILL.md changed since
# fixture baseline must block gate so maintainer cannot ignore drift signal.
GATING_FAIL = {
    CaseStatus.BEHAVIOR_FAIL,
    CaseStatus.OVER_TRIGGER,
    CaseStatus.DELEGATED_TRIGGER,
    CaseStatus.DRIFT_DETECTED,
}
GATING_EXCLUDED = {
    CaseStatus.INFRA_ERROR,
    CaseStatus.PARSE_ERROR,
    CaseStatus.SCHEMA_ERROR,
    CaseStatus.SCHEMA_VERSION_MISMATCH,
    CaseStatus.AUTH_ERROR,
    CaseStatus.MODEL_UNAVAILABLE,
    CaseStatus.COST_EXCEEDED,
    CaseStatus.SKIPPED,
}

# AQG skill name prefix used to detect over-trigger of unrelated AQG skills.
AQG_SKILL_PREFIX = "aqg-"


def decide_status(
    diagnostics: dict[str, Any],
    top_level_calls: list[dict[str, Any]],
    expected_skill: str | None,
    allowed_extra_skills: list[str] | None = None,
    canary_passed: bool = True,
    drift_passed: bool = True,
    per_run_budget_usd: float = DEFAULT_PER_RUN_BUDGET_USD,
) -> CaseStatus:
    """Decide CaseStatus from extractor output.

    Args:
        diagnostics: extractor.extract_skill_calls() second return value.
        top_level_calls: extractor.extract_skill_calls() first return value.
        expected_skill: case fixture expected_skill (None for negative case).
        allowed_extra_skills: positive case opt-in extras (default empty -> only expected).
        canary_passed: whether canary fixture validation passed for this run
            (False means upstream stream-json schema drift; see canary.py).
        drift_passed: whether SKILL.md hash check passed for this case
            (False means SKILL.md changed since fixture baseline; see drift.py).
        per_run_budget_usd: cost guard ceiling.

    Returns:
        CaseStatus per ordered decision tree (schema v1 §3).
    """
    if allowed_extra_skills is None:
        allowed_extra_skills = []

    # 1. Parse / schema (JSONL itself broken)
    if diagnostics["parse_error_rate"] > 0.05:
        return CaseStatus.PARSE_ERROR
    if not diagnostics["init_event_present"]:
        return CaseStatus.SCHEMA_ERROR

    # 2. Schema version mismatch (canary fixture / SKILL.md drift)
    # canary failure = upstream Claude Code stream-json schema drift
    # drift failure = SKILL.md changed since fixture baseline (see drift.py)
    if not canary_passed:
        return CaseStatus.SCHEMA_VERSION_MISMATCH
    if not drift_passed:
        return CaseStatus.DRIFT_DETECTED

    # 3. Result-level errors (must precede init.skills baseline because
    #    auth/model failures cause empty init.skills as side-effect, not root cause)
    api_status = diagnostics.get("result_api_error_status")
    if api_status in (401, 403):
        return CaseStatus.AUTH_ERROR
    if api_status == 404:
        return CaseStatus.MODEL_UNAVAILABLE

    # 4. Cost guard (Q-Spike-8.1: subtype="error_max_budget_usd" + actual cost over)
    result_subtype = diagnostics.get("result_subtype")
    cost = diagnostics.get("result_total_cost_usd")
    if result_subtype == "error_max_budget_usd":
        return CaseStatus.COST_EXCEEDED
    if cost is not None and cost > per_run_budget_usd:
        return CaseStatus.COST_EXCEEDED

    # 5. Config / infra (audit be5e71f8 convergent: applies to all cases including negative)
    init_skills = diagnostics.get("init_skills_loaded") or []
    # 5a. Baseline: every case needs the AQG pack to actually load (otherwise negative cases also false PASS)
    if not any(s.startswith(AQG_SKILL_PREFIX) for s in init_skills):
        return CaseStatus.INFRA_ERROR
    # 5b. Positive case: expected skill must be in the init_skills list
    if expected_skill and expected_skill not in init_skills:
        return CaseStatus.INFRA_ERROR

    # 6. Infra fallback (no result event, or result is_error that is not auth/cost/model)
    if not diagnostics.get("result_event_present"):
        return CaseStatus.INFRA_ERROR
    if diagnostics.get("result_is_error"):
        return CaseStatus.INFRA_ERROR

    # 7. Behavior decision
    aqg_skills_called = {
        c["skill_name"]
        for c in top_level_calls
        if c.get("skill_name", "").startswith(AQG_SKILL_PREFIX)
    }

    # 7a. Negative case (expected_skill is None) - must not trigger any AQG skill
    # (top-level OR delegated; audit be5e71f8 gpt-5.5 #3: delegated AQG calls
    # in a negative case also count as over-trigger)
    if expected_skill is None:
        delegated_aqg_called = {
            (c.get("skill_name") or "")
            for c in diagnostics.get("delegated_skill_calls", [])
            if (c.get("skill_name") or "").startswith(AQG_SKILL_PREFIX)
        }
        if not aqg_skills_called and not delegated_aqg_called:
            return CaseStatus.BEHAVIOR_PASS
        return CaseStatus.OVER_TRIGGER

    # 7b. Positive case
    if expected_skill not in aqg_skills_called:
        # Check delegated for surface-only DELEGATED_TRIGGER (sketch v3 §3.5)
        delegated_skill_names = {
            c.get("skill_name") for c in diagnostics.get("delegated_skill_calls", [])
        }
        if expected_skill in delegated_skill_names:
            return CaseStatus.DELEGATED_TRIGGER
        return CaseStatus.BEHAVIOR_FAIL

    # Expected matched - check for over-trigger of other AQG skills
    unexpected = aqg_skills_called - {expected_skill} - set(allowed_extra_skills)
    if unexpected:
        return CaseStatus.OVER_TRIGGER

    return CaseStatus.BEHAVIOR_PASS
