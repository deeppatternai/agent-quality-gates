"""Unit tests for tests/behavior/decision.py.

Validates ordered decision tree (sketch v3 §3.4 + schema v1 §3) with all enum branches.
"""

from __future__ import annotations

import pytest

from tests.behavior.decision import (
    DEFAULT_PER_RUN_BUDGET_USD,
    GATING_EXCLUDED,
    GATING_FAIL,
    GATING_PASS,
    CaseStatus,
    decide_status,
)


# ===== Helpers =====


def baseline_diag(**overrides) -> dict:
    """Default diagnostics: clean run, init present, result success, no errors."""
    diag = {
        "total_nonempty_lines": 50,
        "json_decode_errors": 0,
        "parse_error_rate": 0.0,
        "delegated_skill_calls": [],
        "init_event_present": True,
        "init_skills_loaded": [
            "aqg-startup-preflight",
            "aqg-systematic-debugging",
            "aqg-audit-adjudication",
            "aqg-evidence-closeout",
        ],
        "result_event_present": True,
        "result_is_error": False,
        "result_subtype": "success",
        "result_api_error_status": None,
        "result_total_cost_usd": 0.15,
        "result_num_turns": 5,
        "result_duration_ms": 30000,
    }
    diag.update(overrides)
    return diag


def skill_call(name: str) -> dict:
    return {"skill_name": name, "tool_use_id": f"toolu_{name}", "caller_type": "direct"}


# ===== Status enum / gating sets =====


class TestEnumSets:
    def test_pass_set(self):
        assert CaseStatus.BEHAVIOR_PASS in GATING_PASS
        assert len(GATING_PASS) == 1

    def test_fail_set(self):
        # DELEGATED_TRIGGER counts as FAIL (audit be5e71f8 gpt-5.5 #2)
        # DRIFT_DETECTED counts as FAIL (audit d982c3c3 #5: must block gate)
        assert CaseStatus.BEHAVIOR_FAIL in GATING_FAIL
        assert CaseStatus.OVER_TRIGGER in GATING_FAIL
        assert CaseStatus.DELEGATED_TRIGGER in GATING_FAIL
        assert CaseStatus.DRIFT_DETECTED in GATING_FAIL

    def test_excluded_set(self):
        for status in (
            CaseStatus.INFRA_ERROR,
            CaseStatus.PARSE_ERROR,
            CaseStatus.SCHEMA_ERROR,
            CaseStatus.SCHEMA_VERSION_MISMATCH,
            CaseStatus.AUTH_ERROR,
            CaseStatus.MODEL_UNAVAILABLE,
            CaseStatus.COST_EXCEEDED,
            CaseStatus.SKIPPED,
        ):
            assert status in GATING_EXCLUDED
        # DELEGATED_TRIGGER + DRIFT_DETECTED moved to FAIL (must block gate)
        assert CaseStatus.DELEGATED_TRIGGER not in GATING_EXCLUDED
        assert CaseStatus.DRIFT_DETECTED not in GATING_EXCLUDED

    def test_gating_sets_disjoint(self):
        assert not (GATING_PASS & GATING_FAIL)
        assert not (GATING_PASS & GATING_EXCLUDED)
        assert not (GATING_FAIL & GATING_EXCLUDED)

    def test_all_enum_categorized(self):
        all_categorized = GATING_PASS | GATING_FAIL | GATING_EXCLUDED
        for status in CaseStatus:
            assert status in all_categorized, f"{status} not in any gating set"


# ===== Ordered decision tree priority =====


class TestParseErrorPriority:
    def test_high_parse_error_rate_wins(self):
        # parse_error_rate > 5% even with valid result → PARSE_ERROR
        diag = baseline_diag(parse_error_rate=0.10)
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.PARSE_ERROR

    def test_5pct_exact_not_triggered(self):
        diag = baseline_diag(parse_error_rate=0.05)
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill="aqg-startup-preflight",
        )
        # ≤5% allowed; should fall through to behavior decision
        assert result == CaseStatus.BEHAVIOR_PASS


class TestSchemaErrorPriority:
    def test_no_init_event_returns_schema_error(self):
        diag = baseline_diag(init_event_present=False)
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.SCHEMA_ERROR


class TestSchemaVersionMismatchPriority:
    def test_canary_failed_returns_mismatch(self):
        diag = baseline_diag()
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill="aqg-startup-preflight",
            canary_passed=False,
        )
        assert result == CaseStatus.SCHEMA_VERSION_MISMATCH


class TestDriftDetectedPriority:
    def test_drift_failed_returns_drift_detected(self):
        diag = baseline_diag()
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill="aqg-startup-preflight",
            drift_passed=False,
        )
        assert result == CaseStatus.DRIFT_DETECTED

    def test_drift_passed_default_true(self):
        # When drift_passed not provided, defaults to True (pass-through)
        diag = baseline_diag()
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill="aqg-startup-preflight",
        )
        assert result == CaseStatus.BEHAVIOR_PASS

    def test_canary_takes_priority_over_drift(self):
        # If both canary AND drift fail, canary wins (upstream schema is more critical)
        diag = baseline_diag()
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill="aqg-startup-preflight",
            canary_passed=False,
            drift_passed=False,
        )
        assert result == CaseStatus.SCHEMA_VERSION_MISMATCH


class TestInfraErrorPriority:
    def test_expected_skill_not_in_init_skills(self):
        # init_skills_loaded contains aqg-* (baseline check passes) but is missing the expected one
        diag = baseline_diag(init_skills_loaded=["aqg-evidence-closeout"])
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.INFRA_ERROR

    def test_no_aqg_skills_in_init_baseline(self):
        # even in a negative case (expected=None), no AQG skills loaded is also INFRA_ERROR
        # (audit be5e71f8 convergent fix)
        diag = baseline_diag(init_skills_loaded=["other-skill", "another"])
        result = decide_status(diag, [], expected_skill=None)
        assert result == CaseStatus.INFRA_ERROR

    def test_no_aqg_skills_positive_case(self):
        diag = baseline_diag(init_skills_loaded=[])
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.INFRA_ERROR

    def test_no_result_event(self):
        diag = baseline_diag(result_event_present=False)
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.INFRA_ERROR

    def test_result_is_error_generic(self):
        # is_error=true but no api_error_status, no cost subtype → INFRA_ERROR
        diag = baseline_diag(result_is_error=True, result_subtype="success")
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.INFRA_ERROR


class TestAuthErrorPriority:
    def test_401(self):
        diag = baseline_diag(result_is_error=True, result_api_error_status=401)
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.AUTH_ERROR

    def test_403(self):
        diag = baseline_diag(result_is_error=True, result_api_error_status=403)
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.AUTH_ERROR


class TestModelUnavailable:
    def test_404_subtype_success_but_is_error(self):
        # Q-Spike-8.2 confirmed: invalid model has subtype="success" + is_error=true + api_error_status=404
        diag = baseline_diag(
            result_is_error=True,
            result_subtype="success",
            result_api_error_status=404,
            init_skills_loaded=[],  # no init.skills loaded when model 404s
        )
        result = decide_status(diag, [], expected_skill=None)  # negative case to skip infra check
        assert result == CaseStatus.MODEL_UNAVAILABLE


class TestCostExceeded:
    def test_subtype_error_max_budget(self):
        diag = baseline_diag(
            result_is_error=True,
            result_subtype="error_max_budget_usd",
            result_total_cost_usd=0.067,
        )
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.COST_EXCEEDED

    def test_actual_cost_over_guard(self):
        diag = baseline_diag(result_total_cost_usd=0.99)
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill="aqg-startup-preflight",
            per_run_budget_usd=0.50,
        )
        assert result == CaseStatus.COST_EXCEEDED


# ===== Behavior decisions =====


class TestPositiveBehaviorPass:
    def test_only_expected_called(self):
        diag = baseline_diag()
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill="aqg-startup-preflight",
        )
        assert result == CaseStatus.BEHAVIOR_PASS

    def test_non_aqg_extras_ok(self):
        # Non-AQG tool calls in stream don't count as over-trigger
        diag = baseline_diag()
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill="aqg-startup-preflight",
        )
        assert result == CaseStatus.BEHAVIOR_PASS


class TestPositiveBehaviorFail:
    def test_expected_not_called(self):
        diag = baseline_diag()
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.BEHAVIOR_FAIL

    def test_other_skill_called_instead(self):
        diag = baseline_diag()
        result = decide_status(
            diag,
            [skill_call("aqg-systematic-debugging")],
            expected_skill="aqg-startup-preflight",
        )
        assert result == CaseStatus.BEHAVIOR_FAIL


class TestOverTrigger:
    def test_positive_unexpected_aqg_skill(self):
        diag = baseline_diag()
        result = decide_status(
            diag,
            [
                skill_call("aqg-startup-preflight"),
                skill_call("aqg-evidence-closeout"),
            ],
            expected_skill="aqg-startup-preflight",
        )
        assert result == CaseStatus.OVER_TRIGGER

    def test_positive_allowed_extras_ok(self):
        diag = baseline_diag()
        result = decide_status(
            diag,
            [
                skill_call("aqg-startup-preflight"),
                skill_call("aqg-evidence-closeout"),
            ],
            expected_skill="aqg-startup-preflight",
            allowed_extra_skills=["aqg-evidence-closeout"],
        )
        assert result == CaseStatus.BEHAVIOR_PASS

    def test_negative_any_aqg_skill_over_triggers(self):
        diag = baseline_diag()
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill=None,
        )
        assert result == CaseStatus.OVER_TRIGGER

    def test_negative_no_aqg_skill_passes(self):
        diag = baseline_diag()
        result = decide_status(diag, [], expected_skill=None)
        assert result == CaseStatus.BEHAVIOR_PASS

    def test_negative_delegated_aqg_also_over_triggers(self):
        # audit be5e71f8 gpt-5.5 #3: delegated AQG calls also count as over-trigger in a negative case
        diag = baseline_diag(
            delegated_skill_calls=[skill_call("aqg-startup-preflight")],
        )
        result = decide_status(diag, [], expected_skill=None)
        assert result == CaseStatus.OVER_TRIGGER


class TestDelegatedTrigger:
    def test_only_delegated_match_returns_delegated_trigger(self):
        diag = baseline_diag(
            delegated_skill_calls=[skill_call("aqg-startup-preflight")],
        )
        # top_level_calls is empty
        result = decide_status(diag, [], expected_skill="aqg-startup-preflight")
        assert result == CaseStatus.DELEGATED_TRIGGER

    def test_delegated_match_does_not_block_top_level_pass(self):
        diag = baseline_diag(
            delegated_skill_calls=[skill_call("aqg-systematic-debugging")],
        )
        result = decide_status(
            diag,
            [skill_call("aqg-startup-preflight")],
            expected_skill="aqg-startup-preflight",
        )
        # Top-level matched expected; delegated unrelated → still PASS
        assert result == CaseStatus.BEHAVIOR_PASS


class TestDefaultBudget:
    def test_default_budget_constant(self):
        assert DEFAULT_PER_RUN_BUDGET_USD == 0.50
