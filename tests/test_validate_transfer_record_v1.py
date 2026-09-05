"""Tests for tests/transfer/validate_transfer_record_v1.py.

Covers §4 task record + run summary schema (sketch a2):
- happy path on _example_run_summary.yaml
- task_id / task_name mismatch
- result=warn rejected for hard-gate tasks
- provenance required block + Task 5 model_id requirement
- summary tasks[] count + duplicate task_id
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
TRANSFER = REPO / "tests" / "transfer"
sys.path.insert(0, str(TRANSFER))

import validate_transfer_record_v1 as vtr  # noqa: E402


def _load_example_summary() -> dict:
    import yaml
    return yaml.safe_load(
        (TRANSFER / "_example_run_summary.yaml").read_text(encoding="utf-8")
    )


# ===== Happy path =====


def test_example_summary_validates_clean() -> None:
    summary = _load_example_summary()
    result = vtr.check_run_summary(summary)
    assert result.is_valid, result.violations


def test_example_summary_each_task_record_validates() -> None:
    summary = _load_example_summary()
    for i, task in enumerate(summary["tasks"]):
        result = vtr.check_task_record(task)
        assert result.is_valid, f"task[{i}]: {result.violations}"


# ===== Task record schema =====


def test_task_id_must_be_in_1_to_6() -> None:
    summary = _load_example_summary()
    record = deepcopy(summary["tasks"][0])
    record["task_id"] = 7
    result = vtr.check_task_record(record)
    assert not result.is_valid
    assert any("task_id" in v for v in result.violations)


def test_task_name_must_match_task_id() -> None:
    summary = _load_example_summary()
    record = deepcopy(summary["tasks"][0])
    record["task_id"] = 1
    record["task_name"] = "handoff_schema"  # Task 2 name
    result = vtr.check_task_record(record)
    assert not result.is_valid
    assert any("task_name" in v and "match" in v.lower() for v in result.violations)


def test_hard_gate_task_cannot_be_warn() -> None:
    summary = _load_example_summary()
    record = deepcopy(summary["tasks"][0])  # task_id 1
    record["result"] = "warn"
    result = vtr.check_task_record(record)
    assert not result.is_valid
    assert any("warn" in v.lower() and "hard-gate" in v.lower() for v in result.violations)


def test_task5_warn_is_allowed() -> None:
    summary = _load_example_summary()
    record = deepcopy(summary["tasks"][4])  # task_id 5
    record["result"] = "warn"
    result = vtr.check_task_record(record)
    assert result.is_valid, result.violations


def test_required_checks_passed_cannot_exceed_total() -> None:
    summary = _load_example_summary()
    record = deepcopy(summary["tasks"][0])
    record["required_checks_passed"] = 99
    record["required_checks_total"] = 4
    result = vtr.check_task_record(record)
    assert not result.is_valid
    assert any("required_checks_passed" in v for v in result.violations)


def test_finished_at_must_be_after_started_at() -> None:
    summary = _load_example_summary()
    record = deepcopy(summary["tasks"][0])
    record["started_at"] = "2026-05-05T01:00:30+00:00"
    record["finished_at"] = "2026-05-05T01:00:00+00:00"
    result = vtr.check_task_record(record)
    assert not result.is_valid
    assert any("finished_at" in v and ">=" in v for v in result.violations)


# ===== Provenance =====


def test_provenance_required_fields_enforced() -> None:
    summary = _load_example_summary()
    record = deepcopy(summary["tasks"][0])
    del record["provenance"]["git_sha"]
    result = vtr.check_task_record(record)
    assert not result.is_valid
    assert any("git_sha" in v for v in result.violations)


def test_git_sha_must_be_40_hex() -> None:
    summary = _load_example_summary()
    record = deepcopy(summary["tasks"][0])
    record["provenance"]["git_sha"] = "abc123"
    result = vtr.check_task_record(record)
    assert not result.is_valid
    assert any("git_sha" in v and "40-hex" in v for v in result.violations)


def test_task5_record_requires_task5_model_id() -> None:
    summary = _load_example_summary()
    task5 = deepcopy(summary["tasks"][4])
    task5["result"] = "pass"
    task5["skipped"] = False  # B1: audit really ran (consistent with result=pass)
    task5["provenance"]["task5_model_id"] = None  # not allowed for Task 5 pass
    task5["provenance"]["task5_provider"] = None
    result = vtr.check_task_record(task5)
    assert not result.is_valid
    assert any(
        "task5_model_id" in v and "required" in v.lower() for v in result.violations
    )


def test_aqg_version_must_be_semver() -> None:
    summary = _load_example_summary()
    record = deepcopy(summary["tasks"][0])
    record["provenance"]["aqg_version"] = "garbage"
    result = vtr.check_task_record(record)
    assert not result.is_valid
    assert any("aqg_version" in v for v in result.violations)


# ===== Run summary =====


def test_summary_must_have_exactly_6_tasks() -> None:
    summary = _load_example_summary()
    summary["tasks"] = summary["tasks"][:5]
    result = vtr.check_run_summary(summary)
    assert not result.is_valid
    assert any("tasks" in v and "exactly 6" in v for v in result.violations)


def test_summary_duplicate_task_id_rejected() -> None:
    summary = _load_example_summary()
    summary["tasks"][1] = deepcopy(summary["tasks"][0])  # both task_id=1
    result = vtr.check_run_summary(summary)
    assert not result.is_valid
    assert any("duplicate task_id" in v for v in result.violations)


def test_threshold_strict_must_be_1_0_in_v1() -> None:
    summary = _load_example_summary()
    summary["threshold_strict"] = 0.8
    result = vtr.check_run_summary(summary)
    assert not result.is_valid
    assert any("threshold_strict" in v for v in result.violations)


def test_overall_pass_rate_out_of_range_rejected() -> None:
    summary = _load_example_summary()
    summary["overall_pass_rate"] = 1.5
    result = vtr.check_run_summary(summary)
    assert not result.is_valid
    assert any("overall_pass_rate" in v for v in result.violations)


def test_run_id_pattern_enforced() -> None:
    summary = _load_example_summary()
    summary["run_id"] = "Has Spaces"
    result = vtr.check_run_summary(summary)
    assert not result.is_valid
    assert any("run_id" in v for v in result.violations)


def test_date_pattern_enforced() -> None:
    summary = _load_example_summary()
    summary["date"] = "2026/05/05"
    result = vtr.check_run_summary(summary)
    assert not result.is_valid
    assert any("date" in v for v in result.violations)


def test_task5_advisory_must_be_pass_warn_or_fail() -> None:
    summary = _load_example_summary()
    summary["task5_advisory"] = "indeterminate"
    result = vtr.check_run_summary(summary)
    assert not result.is_valid
    assert any("task5_advisory" in v for v in result.violations)


# ===== B1 v1.1: explicit `skipped: bool` field =====


def test_task5_skipped_true_record_validates_clean() -> None:
    """Task 5 advisory skip: skipped=true + result=warn + null model/provider OK."""
    summary = _load_example_summary()
    task5 = deepcopy(summary["tasks"][4])
    assert task5["skipped"] is True
    assert task5["result"] == "warn"
    assert task5["provenance"]["task5_model_id"] is None
    assert task5["provenance"]["task5_provider"] is None
    result = vtr.check_task_record(task5)
    assert result.is_valid, result.violations


def test_task5_skipped_false_with_audit_run_validates_clean() -> None:
    """Task 5 audit ran: skipped=false + result=pass + non-null model/provider OK."""
    summary = _load_example_summary()
    task5 = deepcopy(summary["tasks"][4])
    task5["result"] = "pass"
    task5["skipped"] = False
    task5["provenance"]["task5_model_id"] = "gpt-5.5"
    task5["provenance"]["task5_provider"] = "codex-cli"
    result = vtr.check_task_record(task5)
    assert result.is_valid, result.violations


def test_skipped_must_be_bool() -> None:
    summary = _load_example_summary()
    task5 = deepcopy(summary["tasks"][4])
    task5["skipped"] = "true"  # str not bool
    result = vtr.check_task_record(task5)
    assert not result.is_valid
    assert any("skipped: must be bool" in v for v in result.violations)


def test_skipped_true_invalid_for_non_task5() -> None:
    """skipped=true on Task 1-4/6 violates the advisory-skip invariant."""
    summary = _load_example_summary()
    task1 = deepcopy(summary["tasks"][0])  # task_id 1
    task1["skipped"] = True
    result = vtr.check_task_record(task1)
    assert not result.is_valid
    assert any(
        "skipped=true is only valid for task_id=5" in v for v in result.violations
    )


def test_skipped_true_invalid_with_pass_or_fail_result() -> None:
    """skipped=true requires result=warn (advisory skip path)."""
    summary = _load_example_summary()
    task5 = deepcopy(summary["tasks"][4])
    task5["result"] = "pass"
    task5["skipped"] = True  # contradicts result=pass
    task5["provenance"]["task5_model_id"] = "gpt-5.5"
    task5["provenance"]["task5_provider"] = "codex-cli"
    result = vtr.check_task_record(task5)
    assert not result.is_valid
    assert any(
        "skipped=true requires result=warn" in v for v in result.violations
    )


def test_skipped_true_with_warn_result_does_not_require_model_id() -> None:
    """B1 fix for gemini #4: real-warning vs skipped-advisory distinguished by skipped flag.
    skipped=true + result=warn + null model_id is the ADVISORY-SKIP path,
    so model_id/provider are NOT required."""
    summary = _load_example_summary()
    task5 = deepcopy(summary["tasks"][4])
    task5["skipped"] = True
    task5["result"] = "warn"
    task5["provenance"]["task5_model_id"] = None
    task5["provenance"]["task5_provider"] = None
    result = vtr.check_task_record(task5)
    assert result.is_valid, result.violations


def test_skipped_false_with_warn_result_requires_model_id() -> None:
    """B1 fix for gemini #4: REAL warning (audit ran but findings warned)
    requires model_id/provider just like pass/fail. skipped=false enforces this."""
    summary = _load_example_summary()
    task5 = deepcopy(summary["tasks"][4])
    task5["skipped"] = False  # audit really ran
    task5["result"] = "warn"  # but only minor findings
    task5["provenance"]["task5_model_id"] = None  # missing → violation
    task5["provenance"]["task5_provider"] = None
    result = vtr.check_task_record(task5)
    assert not result.is_valid
    assert any(
        "task5_model_id" in v and "required" in v.lower() for v in result.violations
    )


def test_pre_b1_record_without_skipped_falls_back_to_implicit_inference() -> None:
    """Backward compat: a pre-B1 record (no `skipped` key) still validates via
    the implicit `result in {pass, fail}` inference. Task 5 result=warn with
    null model/provider is treated as advisory skip per pre-B1 semantics."""
    summary = _load_example_summary()
    task5 = deepcopy(summary["tasks"][4])
    del task5["skipped"]  # simulate pre-B1 record
    assert task5["result"] == "warn"
    assert task5["provenance"]["task5_model_id"] is None
    result = vtr.check_task_record(task5)
    assert result.is_valid, result.violations


def test_unknown_top_level_field_still_rejected() -> None:
    """Adding `skipped` to OPTIONAL_TASK_FIELDS must not let other unknown
    fields slip through."""
    summary = _load_example_summary()
    task5 = deepcopy(summary["tasks"][4])
    task5["foo_bar_baz"] = True  # not allowed
    result = vtr.check_task_record(task5)
    assert not result.is_valid
    assert any("foo_bar_baz" in v and "unknown" in v for v in result.violations)


def test_summary_task5_advisory_must_match_task5_record_result() -> None:
    """B1 audit 73619b3e #3: summary.task5_advisory must equal the Task 5
    record's `result`. Drift between them misleads downstream consumers."""
    summary = _load_example_summary()
    summary["task5_advisory"] = "pass"  # Task 5 record's result is "warn"
    result = vtr.check_run_summary(summary)
    assert not result.is_valid
    assert any(
        "task5_advisory" in v and "must match" in v for v in result.violations
    )


def test_summary_task5_advisory_match_passes() -> None:
    """Happy path: example fixture has task5_advisory: warn matching Task 5 result: warn."""
    summary = _load_example_summary()
    assert summary["task5_advisory"] == summary["tasks"][4]["result"]
    result = vtr.check_run_summary(summary)
    assert result.is_valid, result.violations


def test_schema_version_2_accepted() -> None:
    """B1 audit 73619b3e #1: validator accepts both schema_version 1 and 2."""
    summary = _load_example_summary()
    assert summary["schema_version"] == 2
    result = vtr.check_run_summary(summary)
    assert result.is_valid, result.violations


def test_schema_version_1_still_accepted_backward_compat() -> None:
    """Pre-B1 records (schema_version=1, no skipped key) still validate."""
    summary = _load_example_summary()
    summary["schema_version"] = 1
    for task in summary["tasks"]:
        task["schema_version"] = 1
        if "skipped" in task:
            del task["skipped"]
    # task5_advisory still must match record result
    result = vtr.check_run_summary(summary)
    assert result.is_valid, result.violations


def test_schema_version_3_rejected() -> None:
    summary = _load_example_summary()
    summary["schema_version"] = 3
    result = vtr.check_run_summary(summary)
    assert not result.is_valid
    assert any("schema_version" in v for v in result.violations)
