#!/usr/bin/env python3
"""Pytest/unittest regression for handoff manifest v0 validator (Wave 1-0)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from validate_handoff_manifest import (  # noqa: E402
    ManifestValidationResult,
    cross_check_ci_run_id,
    self_test as module_self_test,
    validate_manifest,
)


def _make_clean_manifest() -> dict:
    return {
        "aqg_context": {
            "actor": "claude",
            "parent_session_id": "session-abc-123",
            "task": "implement Wave 1-0 surface redaction",
        },
        "aqg_confirm": {
            "command_line": "python3 scripts/_surface_redaction.py",
            "tool_version": "0.2.0",
            "env_fingerprint": {
                "claude_settings_json": {
                    "exists": True,
                    "size_bytes": 1024,
                    "sha256_first8": "a1b2c3d4",
                    "hooks_count": 1,
                },
                "path_dirs_count": 12,
            },
            "ci_run_id": 12345,
            "artifact_uri": "https://github.com/o/r/actions/runs/12345",
            "log_digest": "sha256:abcdef1234567890",
            "seeds": [],
            "scenario_ids": [],
        },
        "aqg_trace": {
            "exit_code": 0,
            "result": "pass",
            "blockers": [],
        },
    }


def _fake_gh_api(responses: dict[str, tuple[int, str]]):
    def _fake(path: str, *, timeout: int = 15) -> tuple[int, str]:
        return responses.get(path, (1, f"unexpected path: {path}"))
    return _fake


class HandoffManifestRegressionTest(unittest.TestCase):
    """High-frequency cases asserted in dedicated unittests (not relying on the module self_test)."""

    def test_module_self_test_passes(self) -> None:
        """Wave 1-0 gatekeeping: the module's built-in self_test must PASS."""
        self.assertEqual(module_self_test(), 0)

    def test_clean_manifest_schema_only(self) -> None:
        result = validate_manifest(manifest=_make_clean_manifest())
        self.assertTrue(result.is_valid, msg=str(result))

    def test_missing_required_node_aqg_context(self) -> None:
        bad = _make_clean_manifest()
        del bad["aqg_context"]
        result = validate_manifest(manifest=bad)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("aqg_context" in e for e in result.schema_errors))

    def test_missing_required_node_aqg_trace(self) -> None:
        bad = _make_clean_manifest()
        del bad["aqg_trace"]
        result = validate_manifest(manifest=bad)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("aqg_trace" in e for e in result.schema_errors))

    def test_actor_must_be_claude_or_codex(self) -> None:
        for bad_actor in ("gemini", "human", "", "Claude"):
            bad = _make_clean_manifest()
            bad["aqg_context"]["actor"] = bad_actor
            result = validate_manifest(manifest=bad)
            self.assertFalse(
                result.is_valid,
                f"actor={bad_actor!r} should be rejected",
            )

    def test_result_must_be_pass_warn_fail(self) -> None:
        for bad_result in ("succeeded", "ok", "passed"):
            bad = _make_clean_manifest()
            bad["aqg_trace"]["result"] = bad_result
            result = validate_manifest(manifest=bad)
            self.assertFalse(
                result.is_valid,
                f"result={bad_result!r} should be rejected",
            )

    def test_exit_code_bool_rejected(self) -> None:
        """Python bool is a subclass of int, but exit_code must not be a bool."""
        bad = _make_clean_manifest()
        bad["aqg_trace"]["exit_code"] = True
        result = validate_manifest(manifest=bad)
        self.assertFalse(result.is_valid)

    def test_high_stakes_requires_ci_run_id(self) -> None:
        bad = _make_clean_manifest()
        del bad["aqg_confirm"]["ci_run_id"]
        result = validate_manifest(
            manifest=bad,
            high_stakes=True,
            skip_remote_cross_check=True,
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(any("ci_run_id" in e for e in result.schema_errors))

    def test_env_fingerprint_redaction_integration(self) -> None:
        """Phase A redactor integration: env_fingerprint containing a forbidden field fails."""
        bad = _make_clean_manifest()
        bad["aqg_confirm"]["env_fingerprint"]["openai_api_key"] = "sk-leak"
        result = validate_manifest(manifest=bad)
        self.assertFalse(result.is_valid)
        self.assertTrue(result.redaction_errors, str(result))

    def test_high_stakes_cross_check_pass(self) -> None:
        """The case where all three cross-checks pass."""
        fake = _fake_gh_api({
            "/repos/deeppatternai/agent-quality-gates/actions/runs/12345": (
                0,
                json.dumps({
                    "conclusion": "success",
                    "head_sha": "deadbeef" * 5,
                    "workflow_id": 999,
                }),
            ),
            "/repos/deeppatternai/agent-quality-gates/actions/workflows/999": (
                0,
                json.dumps({"name": "Quality Gates"}),
            ),
        })
        result = validate_manifest(
            manifest=_make_clean_manifest(),
            high_stakes=True,
            repo="deeppatternai/agent-quality-gates",
            expected_commit_sha="deadbeef" * 5,
            expected_workflow_name="Quality Gates",
            gh_api_func=fake,
        )
        self.assertTrue(result.is_valid, msg=str(result))

    def test_high_stakes_head_sha_mismatch_blocks(self) -> None:
        """LLM-hallucinated reuse of a historical run_id: head_sha mismatch fails."""
        fake = _fake_gh_api({
            "/repos/deeppatternai/agent-quality-gates/actions/runs/12345": (
                0,
                json.dumps({
                    "conclusion": "success",
                    "head_sha": "wrong_sha_xx",
                    "workflow_id": 999,
                }),
            ),
            "/repos/deeppatternai/agent-quality-gates/actions/workflows/999": (
                0,
                json.dumps({"name": "Quality Gates"}),
            ),
        })
        result = validate_manifest(
            manifest=_make_clean_manifest(),
            high_stakes=True,
            repo="deeppatternai/agent-quality-gates",
            expected_commit_sha="deadbeef" * 5,
            expected_workflow_name="Quality Gates",
            gh_api_func=fake,
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(any("head_sha" in e for e in result.cross_check_errors))

    def test_high_stakes_workflow_name_mismatch_blocks(self) -> None:
        fake = _fake_gh_api({
            "/repos/deeppatternai/agent-quality-gates/actions/runs/12345": (
                0,
                json.dumps({
                    "conclusion": "success",
                    "head_sha": "deadbeef" * 5,
                    "workflow_id": 999,
                }),
            ),
            "/repos/deeppatternai/agent-quality-gates/actions/workflows/999": (
                0,
                json.dumps({"name": "Other Workflow"}),
            ),
        })
        result = validate_manifest(
            manifest=_make_clean_manifest(),
            high_stakes=True,
            repo="deeppatternai/agent-quality-gates",
            expected_commit_sha="deadbeef" * 5,
            expected_workflow_name="Quality Gates",
            gh_api_func=fake,
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(any("workflow name" in e for e in result.cross_check_errors))

    def test_high_stakes_failed_run_blocks(self) -> None:
        fake = _fake_gh_api({
            "/repos/deeppatternai/agent-quality-gates/actions/runs/12345": (
                0,
                json.dumps({
                    "conclusion": "failure",
                    "head_sha": "deadbeef" * 5,
                    "workflow_id": 999,
                }),
            ),
            "/repos/deeppatternai/agent-quality-gates/actions/workflows/999": (
                0,
                json.dumps({"name": "Quality Gates"}),
            ),
        })
        result = validate_manifest(
            manifest=_make_clean_manifest(),
            high_stakes=True,
            repo="deeppatternai/agent-quality-gates",
            expected_commit_sha="deadbeef" * 5,
            expected_workflow_name="Quality Gates",
            gh_api_func=fake,
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(any("conclusion" in e for e in result.cross_check_errors))

    def test_high_stakes_skip_remote_check(self) -> None:
        """skip_remote_cross_check still enforces the schema but skips the gh api call."""
        result = validate_manifest(
            manifest=_make_clean_manifest(),
            high_stakes=True,
            skip_remote_cross_check=True,
        )
        self.assertTrue(result.is_valid)

    def test_high_stakes_missing_repo_returns_clear_error(self) -> None:
        result = validate_manifest(
            manifest=_make_clean_manifest(),
            high_stakes=True,
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(any("requires" in e for e in result.cross_check_errors))

    def test_load_manifest_from_json_file(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(_make_clean_manifest(), tmp)
            tmp_path = tmp.name
        try:
            result = validate_manifest(manifest_path=tmp_path)
            self.assertTrue(result.is_valid)
        finally:
            Path(tmp_path).unlink()

    def test_non_mapping_input_rejected(self) -> None:
        result = validate_manifest(manifest=["not", "a", "dict"])  # type: ignore[arg-type]
        self.assertFalse(result.is_valid)

    def test_must_provide_manifest_or_path(self) -> None:
        with self.assertRaises(ValueError):
            validate_manifest()


# ===== v2 schema tests (ADR 2026-05-04 handoff-manifest-v2 + a1 audit fixes) =====


def _make_clean_v2_manifest() -> dict:
    """Clean v2 manifest with aqg_review node — passes all invariants."""
    base = _make_clean_manifest()
    base["schema_version"] = 2
    base["aqg_review"] = {
        "implementation": {
            "status": "done",
            "implementer_model": "claude-opus-4-7",
            "commit_shas": ["deadbeefdeadbeef"],
        },
        "spec_review": {
            "status": "passed",
            "reviewer_model": "gpt-5.5",
            "findings_count": 0,
            "loop_iter": 0,
        },
        "quality_review": {
            "status": "passed",
            "reviewer_model": "gemini-3.1-pro-preview",
            "findings_count": 0,
            "loop_iter": 0,
        },
        "review_loop_count": 0,
        "accepted_findings": [],
        "fixed_before_next_task": True,
    }
    return base


class V2SchemaTests(unittest.TestCase):
    """v2 schema upgrade — 6 invariants + version dispatch + typo guard."""

    def test_clean_v2_manifest_valid(self) -> None:
        result = validate_manifest(manifest=_make_clean_v2_manifest())
        self.assertTrue(result.is_valid, result.schema_errors)

    def test_v1_forward_compat_no_schema_version_no_review(self) -> None:
        """v1 manifest without schema_version + without aqg_review still passes."""
        result = validate_manifest(manifest=_make_clean_manifest())
        self.assertTrue(result.is_valid)

    def test_v1_explicit_schema_version_one(self) -> None:
        m = _make_clean_manifest()
        m["schema_version"] = 1
        result = validate_manifest(manifest=m)
        self.assertTrue(result.is_valid)

    def test_schema_version_bool_rejected(self) -> None:
        m = _make_clean_manifest()
        m["schema_version"] = True
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)

    def test_schema_version_unknown_int_rejected(self) -> None:
        m = _make_clean_manifest()
        m["schema_version"] = 3
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("not in" in e for e in result.schema_errors))

    def test_aqg_review_without_schema_version_2_fails(self) -> None:
        """ADR §2.1: aqg_review present requires schema_version: 2."""
        m = _make_clean_v2_manifest()
        m["schema_version"] = 1
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("must be 2" in e for e in result.schema_errors))

    def test_aqg_typo_guard_fails(self) -> None:
        """ADR §2.5: aqg_-prefixed unknown top-level key fails."""
        m = _make_clean_v2_manifest()
        m["aqg_reviwe"] = {"oops": "typo"}
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("aqg_reviwe" in e for e in result.schema_errors))

    def test_non_aqg_unknown_key_warn_only(self) -> None:
        """Non-aqg_ unknown key is forward-compat (no error)."""
        m = _make_clean_v2_manifest()
        m["project_meta"] = {"some": "extension"}
        result = validate_manifest(manifest=m)
        self.assertTrue(result.is_valid, result.schema_errors)

    def test_invariant_1_quality_before_spec_passed_fails(self) -> None:
        """Invariant 1: quality_review.status != pending requires spec_review.status == passed."""
        m = _make_clean_v2_manifest()
        m["aqg_review"]["spec_review"]["status"] = "pending"
        m["aqg_review"]["spec_review"]["reviewer_model"] = ""
        m["aqg_review"]["quality_review"]["status"] = "passed"
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("invariant 1" in e for e in result.schema_errors))

    def test_invariant_2_done_requires_commit_shas(self) -> None:
        """Invariant 2: implementation.status == done requires non-empty commit_shas."""
        m = _make_clean_v2_manifest()
        m["aqg_review"]["implementation"]["commit_shas"] = []
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("invariant 2" in e for e in result.schema_errors))

    def test_invariant_3_loop_count_monotonic(self) -> None:
        """Invariant 3: review_loop_count >= max(spec.loop_iter, quality.loop_iter)."""
        m = _make_clean_v2_manifest()
        m["aqg_review"]["spec_review"]["loop_iter"] = 5
        m["aqg_review"]["review_loop_count"] = 2  # < 5
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("invariant 3" in e for e in result.schema_errors))

    def test_invariant_5_spec_passed_requires_impl_done(self) -> None:
        """Invariant 5: spec_review.status != pending requires implementation.status == done."""
        m = _make_clean_v2_manifest()
        m["aqg_review"]["implementation"]["status"] = "in_progress"
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("invariant 5" in e for e in result.schema_errors))

    def test_invariant_6_final_handoff_with_unfixed_findings_fails(self) -> None:
        """Invariant 6: aqg_trace.result==pass + accepted_findings non-empty
        requires fixed_before_next_task=true."""
        m = _make_clean_v2_manifest()
        m["aqg_review"]["accepted_findings"] = ["finding-1", "finding-2"]
        m["aqg_review"]["fixed_before_next_task"] = False
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("invariant 6" in e for e in result.schema_errors))

    def test_invariant_6_warn_trace_with_unfixed_findings_ok(self) -> None:
        """Invariant 6 only kicks in on aqg_trace.result==pass; warn allows unfixed."""
        m = _make_clean_v2_manifest()
        m["aqg_review"]["accepted_findings"] = ["finding-1"]
        m["aqg_review"]["fixed_before_next_task"] = False
        m["aqg_trace"]["result"] = "warn"
        result = validate_manifest(manifest=m)
        self.assertTrue(result.is_valid, result.schema_errors)

    def test_commit_sha_invalid_format_fails(self) -> None:
        m = _make_clean_v2_manifest()
        m["aqg_review"]["implementation"]["commit_shas"] = ["not-a-sha!"]
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)

    def test_commit_sha_short_7chars_ok(self) -> None:
        m = _make_clean_v2_manifest()
        m["aqg_review"]["implementation"]["commit_shas"] = ["abc1234"]
        result = validate_manifest(manifest=m)
        self.assertTrue(result.is_valid, result.schema_errors)

    def test_review_status_kebab_old_re_review_rejected(self) -> None:
        """Per a1 audit #7: re-review (kebab) must NOT be accepted; only re_review (snake)."""
        m = _make_clean_v2_manifest()
        m["aqg_review"]["spec_review"]["status"] = "re-review"
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)

    def test_review_status_re_review_snake_ok(self) -> None:
        m = _make_clean_v2_manifest()
        m["aqg_review"]["spec_review"]["status"] = "re_review"
        m["aqg_review"]["spec_review"]["loop_iter"] = 1
        m["aqg_review"]["review_loop_count"] = 1
        # quality_review can't be passed if spec is re_review — invariant 1
        m["aqg_review"]["quality_review"]["status"] = "pending"
        m["aqg_review"]["quality_review"]["reviewer_model"] = ""
        result = validate_manifest(manifest=m)
        self.assertTrue(result.is_valid, result.schema_errors)

    def test_required_subnode_missing_fails(self) -> None:
        m = _make_clean_v2_manifest()
        del m["aqg_review"]["spec_review"]
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("spec_review" in e for e in result.schema_errors))

    def test_fixed_before_next_task_must_be_bool(self) -> None:
        m = _make_clean_v2_manifest()
        m["aqg_review"]["fixed_before_next_task"] = "true"  # str not bool
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)

    def test_v2_sample_file_validates(self) -> None:
        """The committed v2 sample under docs/handoff-manifests/ must validate."""
        sample_path = Path(__file__).resolve().parent.parent / "docs" / "handoff-manifests" / "_example_clean_v2.json"
        result = validate_manifest(manifest_path=sample_path)
        self.assertTrue(result.is_valid, result.schema_errors)

    # ===== a3 audit fix (audit 4afe34c9) =====

    def test_invariant_4_fixed_before_next_task_required_even_when_findings_empty(self) -> None:
        """Invariant 4 (ADR §2.3): fixed_before_next_task must be explicit.
        Our impl is stricter than ADR text — required regardless of accepted_findings.
        Both empty-findings and non-empty-findings cases must require the field."""
        m = _make_clean_v2_manifest()
        # accepted_findings empty AND fixed_before_next_task missing → still fails
        m["aqg_review"]["accepted_findings"] = []
        del m["aqg_review"]["fixed_before_next_task"]
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("fixed_before_next_task" in e and "required" in e
                            for e in result.schema_errors))

    def test_invariant_4_fixed_before_next_task_required_when_findings_nonempty(self) -> None:
        m = _make_clean_v2_manifest()
        m["aqg_review"]["accepted_findings"] = ["finding-1"]
        del m["aqg_review"]["fixed_before_next_task"]
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("fixed_before_next_task" in e and "required" in e
                            for e in result.schema_errors))

    def test_invariant_6_skipped_when_accepted_findings_non_list(self) -> None:
        """a3 audit fix: invariant 6 must NOT fire on malformed (non-list)
        accepted_findings. The malformed-type error from _validate_review_node
        is the single source of truth for that."""
        m = _make_clean_v2_manifest()
        m["aqg_review"]["accepted_findings"] = "should be list"  # invalid type
        m["aqg_review"]["fixed_before_next_task"] = False
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        # Invariant 6 should NOT appear; only the type error
        self.assertFalse(any("invariant 6" in e for e in result.schema_errors))
        self.assertTrue(any("accepted_findings" in e and "must be list" in e
                            for e in result.schema_errors))


class TestL2Hardening(unittest.TestCase):
    """audit bbf4ca5d (B3/B4/B5) — kept here (separate from the in-module self_test)
    so a `git stash` of scripts/validate_handoff_manifest.py proves they fail pre-fix."""

    def test_b3_high_stakes_requires_env_fingerprint(self):
        # CRIT: omitting env_fingerprint must not bypass the redaction gate.
        m = _make_clean_manifest()
        del m["aqg_confirm"]["env_fingerprint"]
        result = validate_manifest(manifest=m, high_stakes=True, skip_remote_cross_check=True)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("env_fingerprint" in e for e in result.schema_errors), result.schema_errors)

    def test_b4_high_stakes_field_types_checked(self):
        m = _make_clean_manifest()
        m["aqg_confirm"]["command_line"] = {"not": "str"}
        m["aqg_confirm"]["seeds"] = "not-a-list"
        result = validate_manifest(manifest=m, high_stakes=True, skip_remote_cross_check=True)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("command_line" in e for e in result.schema_errors), result.schema_errors)
        self.assertTrue(any("seeds" in e for e in result.schema_errors), result.schema_errors)

    def test_b5_invalid_enum_value_not_echoed(self):
        m = _make_clean_manifest()
        m["aqg_context"]["actor"] = "leaked-sk-secret-value"
        result = validate_manifest(manifest=m)
        self.assertFalse(result.is_valid)
        self.assertFalse(
            any("leaked-sk-secret-value" in e for e in result.schema_errors),
            result.schema_errors,
        )


if __name__ == "__main__":
    unittest.main()
