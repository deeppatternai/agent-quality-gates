"""Unit tests for tests/behavior/nightly_runner.py.

Mocks ClaudeRunner + extractor so no real claude CLI call. Verifies:
- fixture loading + sampling determinism (rng_seed)
- per-case orchestration: canary + extract + decide chain
- aggregation: by_status / pass_rate / cost
- exit codes (PASS / FAIL / BUDGET_EXCEEDED)
- argparse + main() ANTHROPIC_API_KEY guard
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.behavior.decision import CaseStatus
from tests.behavior.nightly_runner import (
    EXIT_BUDGET_EXCEEDED,
    EXIT_CANARY_FAIL,
    EXIT_FAIL,
    EXIT_OK,
    EXIT_USAGE,
    main,
    run_nightly,
)
from tests.behavior.runner import RunResult


# ===== Fixtures =====


@pytest.fixture
def fake_repo(tmp_path):
    """Mock a repo root with fixture YAML, canary, and SKILL.md placeholder."""
    repo = tmp_path / "repo"
    (repo / "tests/behavior/fixtures").mkdir(parents=True)
    (repo / "tests/behavior/fixtures/canary").mkdir()
    (repo / "agent-packs/claude-code/skills/aqg-startup-preflight").mkdir(parents=True)

    # Minimal fixture
    fixture = {
        "schema_version": 1,
        "cases": [
            {
                "id": "preflight-001",
                "style": "description_based",
                "prompt": "我刚 clone 这个项目",
                "expected_skill": "aqg-startup-preflight",
                "skill_file_ref": "agent-packs/claude-code/skills/aqg-startup-preflight/SKILL.md",
                # M2 hash fields kept as null (audit 454077e6 gemini-f2): the
                # drift-hash mechanism was retired (PR-3), but the null fields
                # are the `managed ⟺ M2-null` marker, kept for schema parity
                # with the real triggers.yaml.
                "description_sha256_first8": None,
                "trigger_section_sha256_first8": None,
            },
            {
                "id": "preflight-002",
                "style": "explicit_invocation",
                "prompt": "use /aqg-startup-preflight",
                "expected_skill": "aqg-startup-preflight",
                "skill_file_ref": "agent-packs/claude-code/skills/aqg-startup-preflight/SKILL.md",
                "description_sha256_first8": None,
                "trigger_section_sha256_first8": None,
            },
        ],
    }
    import yaml
    (repo / "tests/behavior/fixtures/triggers.yaml").write_text(yaml.dump(fixture))

    # Canary
    canary = {
        "type": "system",
        "subtype": "init",
        "skills": ["aqg-startup-preflight"],
    }
    skill_call = {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {
            "content": [
                {"type": "tool_use", "id": "tu", "name": "Skill",
                 "input": {"skill": "aqg-startup-preflight"},
                 "caller": {"type": "direct"}},
            ],
        },
    }
    result = {"type": "result", "subtype": "success", "is_error": False,
              "total_cost_usd": 0.05, "num_turns": 1}
    (repo / "tests/behavior/fixtures/canary/canary.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [canary, skill_call, result])
    )
    (repo / "tests/behavior/fixtures/canary/canary.expected.json").write_text(
        json.dumps({"expected_skill_names": ["aqg-startup-preflight"]})
    )

    # SKILL.md placeholder (minimal valid frontmatter for parse_skill_md consumers)
    (repo / "agent-packs/claude-code/skills/aqg-startup-preflight/SKILL.md").write_text(
        "---\nname: aqg-startup-preflight\ndescription: test\n---\n\n# Body\n"
    )

    return repo


def _mock_run_result(tmp_path: Path, case_id: str, skill_name: str,
                     cost_usd: float = 0.10, turns: int = 4) -> RunResult:
    """Write a stream-json that the extractor will parse to one Skill call."""
    jsonl = tmp_path / f"{case_id}.jsonl"
    init = {"type": "system", "subtype": "init", "skills": [skill_name]}
    assistant = {
        "type": "assistant", "parent_tool_use_id": None,
        "message": {"content": [
            {"type": "tool_use", "id": "tu", "name": "Skill",
             "input": {"skill": skill_name}, "caller": {"type": "direct"}},
        ]},
    }
    result = {"type": "result", "subtype": "success", "is_error": False,
              "total_cost_usd": cost_usd, "num_turns": turns,
              "duration_ms": 30000, "api_error_status": None}
    jsonl.write_text("\n".join(json.dumps(e) for e in [init, assistant, result]))
    return RunResult(exit_code=0, jsonl_path=jsonl,
                     stderr_path=tmp_path / f"{case_id}.stderr",
                     duration_s=30.0, timed_out=False)


# ===== run_nightly happy path =====


class TestRunNightlyHappy:
    def test_all_pass(self, fake_repo, tmp_path):
        """All sampled cases trigger correctly → exit OK."""
        with patch("tests.behavior.nightly_runner.ClaudeRunner") as MockRunner:
            instance = MagicMock()
            instance.run_case.side_effect = lambda prompt, case_id, **kw: _mock_run_result(
                tmp_path, case_id, "aqg-startup-preflight"
            )
            MockRunner.return_value = instance

            report, exit_code = run_nightly(
                fixture_path=fake_repo / "tests/behavior/fixtures/triggers.yaml",
                fixture_aqg_dir=fake_repo,
                canary_jsonl_path=fake_repo / "tests/behavior/fixtures/canary/canary.jsonl",
                canary_expected_path=fake_repo / "tests/behavior/fixtures/canary/canary.expected.json",
                sample_size=2,
                per_run_budget_usd=0.50,
                per_nightly_budget_usd=5.0,
            )

        assert exit_code == EXIT_OK
        assert report["actual_sample_count"] == 2
        assert report["gating_metrics"]["pass"] == 2
        assert report["gating_metrics"]["fail"] == 0
        assert report["canary_passed"] is True
        # Cost accumulation
        assert report["accumulated_cost_usd"] > 0


class TestRunNightlyFail:
    def test_behavior_fail_returns_exit_1(self, fake_repo, tmp_path):
        """Mock returns no Skill call → BEHAVIOR_FAIL → exit 1."""
        with patch("tests.behavior.nightly_runner.ClaudeRunner") as MockRunner:
            instance = MagicMock()

            def no_skill_run(prompt, case_id, **kw):
                # Stream with init + result but no Skill call
                jsonl = tmp_path / f"{case_id}.jsonl"
                events = [
                    {"type": "system", "subtype": "init", "skills": ["aqg-startup-preflight"]},
                    {"type": "result", "subtype": "success", "is_error": False,
                     "total_cost_usd": 0.10, "num_turns": 1, "duration_ms": 5000,
                     "api_error_status": None},
                ]
                jsonl.write_text("\n".join(json.dumps(e) for e in events))
                return RunResult(exit_code=0, jsonl_path=jsonl,
                                 stderr_path=tmp_path / f"{case_id}.stderr",
                                 duration_s=5.0, timed_out=False)

            instance.run_case.side_effect = no_skill_run
            MockRunner.return_value = instance

            report, exit_code = run_nightly(
                fixture_path=fake_repo / "tests/behavior/fixtures/triggers.yaml",
                fixture_aqg_dir=fake_repo,
                canary_jsonl_path=fake_repo / "tests/behavior/fixtures/canary/canary.jsonl",
                canary_expected_path=fake_repo / "tests/behavior/fixtures/canary/canary.expected.json",
                sample_size=1,
                per_run_budget_usd=0.50,
                per_nightly_budget_usd=5.0,
            )

        assert exit_code == EXIT_FAIL
        assert report["gating_metrics"]["fail"] >= 1


class TestBudgetGuard:
    def test_budget_exceeded_returns_exit_3(self, fake_repo, tmp_path):
        with patch("tests.behavior.nightly_runner.ClaudeRunner") as MockRunner:
            instance = MagicMock()
            # Each case costs $3 → 2 cases would be $6 > $5 budget
            instance.run_case.side_effect = lambda prompt, case_id, **kw: _mock_run_result(
                tmp_path, case_id, "aqg-startup-preflight", cost_usd=3.0
            )
            MockRunner.return_value = instance

            report, exit_code = run_nightly(
                fixture_path=fake_repo / "tests/behavior/fixtures/triggers.yaml",
                fixture_aqg_dir=fake_repo,
                canary_jsonl_path=fake_repo / "tests/behavior/fixtures/canary/canary.jsonl",
                canary_expected_path=fake_repo / "tests/behavior/fixtures/canary/canary.expected.json",
                sample_size=2,
                per_run_budget_usd=5.0,
                per_nightly_budget_usd=5.0,
            )

        assert exit_code == EXIT_BUDGET_EXCEEDED
        assert report["budget_exceeded"] is True
        # Audit fix: pre-check budget BEFORE running case (don't overshoot).
        # Case 1 cost $3 → remaining $2; case 2 pre-check ($2 < per_run $5) → break.
        # Result: only 1 case actually ran (no $6 overshoot).
        assert report["actual_sample_count"] == 1
        assert report["accumulated_cost_usd"] == 3.0  # case 1 only


class TestSamplingDeterminism:
    def test_same_seed_same_sample(self, fake_repo, tmp_path):
        """rng_seed produces deterministic case selection."""
        with patch("tests.behavior.nightly_runner.ClaudeRunner") as MockRunner:
            instance = MagicMock()
            instance.run_case.side_effect = lambda prompt, case_id, **kw: _mock_run_result(
                tmp_path, case_id, "aqg-startup-preflight"
            )
            MockRunner.return_value = instance

            report1, _ = run_nightly(
                fixture_path=fake_repo / "tests/behavior/fixtures/triggers.yaml",
                fixture_aqg_dir=fake_repo,
                canary_jsonl_path=fake_repo / "tests/behavior/fixtures/canary/canary.jsonl",
                canary_expected_path=fake_repo / "tests/behavior/fixtures/canary/canary.expected.json",
                sample_size=1,
                per_run_budget_usd=0.50,
                per_nightly_budget_usd=5.0,
                rng_seed=123,
            )
            report2, _ = run_nightly(
                fixture_path=fake_repo / "tests/behavior/fixtures/triggers.yaml",
                fixture_aqg_dir=fake_repo,
                canary_jsonl_path=fake_repo / "tests/behavior/fixtures/canary/canary.jsonl",
                canary_expected_path=fake_repo / "tests/behavior/fixtures/canary/canary.expected.json",
                sample_size=1,
                per_run_budget_usd=0.50,
                per_nightly_budget_usd=5.0,
                rng_seed=123,
            )

        # Same seed → same case sampled
        assert report1["case_results"][0]["case_id"] == report2["case_results"][0]["case_id"]


# ===== main() ANTHROPIC_API_KEY guard =====


class TestCanaryFailFast:
    def test_canary_fail_skips_claude_invocation(self, fake_repo, tmp_path):
        """audit 0b5cc864 convergent: canary fail must fail-fast (no Claude calls).

        Tamper canary expected.json to declare wrong skill, run nightly, verify
        ClaudeRunner.run_case is NEVER called and exit_code == EXIT_CANARY_FAIL.
        """
        # Tamper canary expected
        canary_expected = fake_repo / "tests/behavior/fixtures/canary/canary.expected.json"
        canary_expected.write_text(
            json.dumps({"expected_skill_names": ["aqg-evidence-closeout"]})  # wrong
        )

        with patch("tests.behavior.nightly_runner.ClaudeRunner") as MockRunner:
            instance = MagicMock()
            instance.run_case.side_effect = AssertionError(
                "Claude must not be called when canary fails"
            )
            MockRunner.return_value = instance

            report, exit_code = run_nightly(
                fixture_path=fake_repo / "tests/behavior/fixtures/triggers.yaml",
                fixture_aqg_dir=fake_repo,
                canary_jsonl_path=fake_repo / "tests/behavior/fixtures/canary/canary.jsonl",
                canary_expected_path=canary_expected,
                sample_size=2,
                per_run_budget_usd=0.50,
                per_nightly_budget_usd=5.0,
            )

        assert exit_code == EXIT_CANARY_FAIL
        assert report["canary_passed"] is False
        assert report["actual_sample_count"] == 0
        assert "abort_reason" in report
        instance.run_case.assert_not_called()


class TestPerCaseExceptionHandler:
    def test_runner_exception_does_not_lose_report(self, fake_repo, tmp_path):
        """audit 0b5cc864 gemini #4: per-case exception → INFRA_ERROR, continue."""
        with patch("tests.behavior.nightly_runner.ClaudeRunner") as MockRunner:
            instance = MagicMock()
            calls = [0]

            def crash_first(prompt, case_id, **kw):
                calls[0] += 1
                if calls[0] == 1:
                    raise RuntimeError("simulated network error")
                return _mock_run_result(tmp_path, case_id, "aqg-startup-preflight")

            instance.run_case.side_effect = crash_first
            MockRunner.return_value = instance

            report, exit_code = run_nightly(
                fixture_path=fake_repo / "tests/behavior/fixtures/triggers.yaml",
                fixture_aqg_dir=fake_repo,
                canary_jsonl_path=fake_repo / "tests/behavior/fixtures/canary/canary.jsonl",
                canary_expected_path=fake_repo / "tests/behavior/fixtures/canary/canary.expected.json",
                sample_size=2,
                per_run_budget_usd=0.50,
                per_nightly_budget_usd=5.0,
            )

        assert report["actual_sample_count"] == 2
        # First case: INFRA_ERROR with exception field
        infra_errors = [c for c in report["case_results"]
                        if c["status"] == "infra_error"]
        assert len(infra_errors) >= 1
        assert "exception" in infra_errors[0]
        assert "RuntimeError" in infra_errors[0]["exception"]


class TestSampleSizeBounds:
    def test_zero_sample_size_rejected(self, fake_repo, tmp_path):
        """audit 0b5cc864 gpt-5.5 #6: sample_size=0 must be rejected, not silent pass."""
        with pytest.raises(ValueError, match="sample_size must be"):
            run_nightly(
                fixture_path=fake_repo / "tests/behavior/fixtures/triggers.yaml",
                fixture_aqg_dir=fake_repo,
                canary_jsonl_path=fake_repo / "tests/behavior/fixtures/canary/canary.jsonl",
                canary_expected_path=fake_repo / "tests/behavior/fixtures/canary/canary.expected.json",
                sample_size=0,
                per_run_budget_usd=0.50,
                per_nightly_budget_usd=5.0,
            )

    def test_excessive_sample_size_rejected(self, fake_repo, tmp_path):
        with pytest.raises(ValueError, match="sample_size must be"):
            run_nightly(
                fixture_path=fake_repo / "tests/behavior/fixtures/triggers.yaml",
                fixture_aqg_dir=fake_repo,
                canary_jsonl_path=fake_repo / "tests/behavior/fixtures/canary/canary.jsonl",
                canary_expected_path=fake_repo / "tests/behavior/fixtures/canary/canary.expected.json",
                sample_size=999,
                per_run_budget_usd=0.50,
                per_nightly_budget_usd=5.0,
            )


class TestMainGuard:
    def test_main_aborts_without_api_key(self, fake_repo, tmp_path, capsys):
        # Strip api key from env
        with patch.dict(os.environ, {}, clear=True):
            exit_code = main([
                "--fixture", str(fake_repo / "tests/behavior/fixtures/triggers.yaml"),
                "--canary-jsonl", str(fake_repo / "tests/behavior/fixtures/canary/canary.jsonl"),
                "--canary-expected", str(fake_repo / "tests/behavior/fixtures/canary/canary.expected.json"),
                "--sample-size", "1",
                "--output", str(tmp_path / "report.json"),
            ])
        assert exit_code == EXIT_USAGE
        captured = capsys.readouterr()
        assert "ANTHROPIC_API_KEY" in captured.err

    def test_main_aborts_on_missing_fixture(self, tmp_path, capsys):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            exit_code = main([
                "--fixture", str(tmp_path / "nonexistent.yaml"),
                "--canary-jsonl", str(tmp_path / "canary.jsonl"),
                "--canary-expected", str(tmp_path / "expected.json"),
                "--sample-size", "1",
                "--output", str(tmp_path / "report.json"),
            ])
        assert exit_code == EXIT_USAGE
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()
