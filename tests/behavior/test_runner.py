"""Unit tests for tests/behavior/runner.py.

Mocks subprocess.run + shutil.which to verify cmd construction + env filtering +
timeout handling without invoking real claude CLI (audit be5e71f8 gpt-5.5 #6).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.behavior.runner import (
    ALLOWED_MODELS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_S,
    ClaudeRunner,
    RunResult,
    _subprocess_env,
)


@pytest.fixture
def fake_aqg_dir(tmp_path):
    """Create a fake AQG fixture dir for runner construction."""
    d = tmp_path / "aqg-clone"
    d.mkdir()
    return d


@pytest.fixture
def runner(fake_aqg_dir, tmp_path):
    """Construct ClaudeRunner with patched shutil.which to allow construction."""
    with patch("tests.behavior.runner.shutil.which", return_value="/usr/bin/claude"):
        return ClaudeRunner(
            fixture_aqg_dir=fake_aqg_dir,
            output_dir=tmp_path / "output",
        )


# ===== ClaudeRunner construction =====


class TestRunnerInit:
    def test_default_model_in_allowlist(self):
        assert DEFAULT_MODEL in ALLOWED_MODELS

    def test_default_timeout(self):
        assert DEFAULT_TIMEOUT_S == 120

    def test_invalid_model_raises(self, fake_aqg_dir, tmp_path):
        with patch("tests.behavior.runner.shutil.which", return_value="/usr/bin/claude"):
            with pytest.raises(ValueError, match="not in allowlist"):
                ClaudeRunner(
                    fixture_aqg_dir=fake_aqg_dir,
                    model="gpt-4",
                    output_dir=tmp_path / "output",
                )

    def test_missing_fixture_dir_raises(self, tmp_path):
        with patch("tests.behavior.runner.shutil.which", return_value="/usr/bin/claude"):
            with pytest.raises(FileNotFoundError, match="not found"):
                ClaudeRunner(
                    fixture_aqg_dir=tmp_path / "nonexistent",
                    output_dir=tmp_path / "output",
                )

    def test_missing_claude_bin_raises(self, fake_aqg_dir, tmp_path):
        with patch("tests.behavior.runner.shutil.which", return_value=None):
            with pytest.raises(FileNotFoundError, match="claude CLI not found"):
                ClaudeRunner(
                    fixture_aqg_dir=fake_aqg_dir,
                    output_dir=tmp_path / "output",
                )


# ===== Subprocess env filter =====


class TestSubprocessEnv:
    def test_strips_aqg_metrics(self):
        with patch.dict(os.environ, {"AQG_METRICS": "1", "OTHER": "x"}, clear=False):
            env = _subprocess_env()
            assert "AQG_METRICS" not in env
            assert env.get("OTHER") == "x"

    def test_no_aqg_metrics_set(self):
        with patch.dict(os.environ, {"OTHER": "x"}, clear=False):
            os.environ.pop("AQG_METRICS", None)
            env = _subprocess_env()
            assert env.get("OTHER") == "x"


# ===== Cmd construction (sketch v3 §3.1 baseline) =====


class TestRunCaseCmdConstruction:
    def test_cmd_contains_required_flags(self, runner):
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("tests.behavior.runner.subprocess.run", side_effect=fake_run):
            runner.run_case(prompt="test prompt", case_id="test-001")

        # Required baseline flags per sketch v3 §3.1
        assert "claude" in captured_cmd[0] or captured_cmd[0] == "claude"
        assert "-p" in captured_cmd
        assert "test prompt" in captured_cmd
        assert "--output-format" in captured_cmd
        assert "stream-json" in captured_cmd
        assert "--no-session-persistence" in captured_cmd
        assert "--max-budget-usd" in captured_cmd
        assert "--add-dir" in captured_cmd
        assert "--permission-mode" in captured_cmd
        assert "default" in captured_cmd
        assert "--model" in captured_cmd
        assert DEFAULT_MODEL in captured_cmd
        assert "--disallowed-tools" in captured_cmd  # Q-Spike-4 cost reduction
        assert "Agent" in captured_cmd
        assert "--verbose" in captured_cmd  # mandatory for stream-json

    def test_cmd_excludes_bare_and_system_prompt(self, runner):
        """v3 verified incompatible flags must not appear (Q-Spike-1/2)."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("tests.behavior.runner.subprocess.run", side_effect=fake_run):
            runner.run_case(prompt="test", case_id="test-002")

        assert "--bare" not in captured_cmd  # Q-Spike-1: AQG skills not loaded
        assert "--system-prompt" not in captured_cmd  # Q-Spike-2: costlier

    def test_budget_flag_value(self, runner):
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("tests.behavior.runner.subprocess.run", side_effect=fake_run):
            runner.run_case(prompt="test", case_id="test-003", per_run_budget_usd=0.25)

        # Budget should be formatted with 4 decimal places
        assert "0.2500" in captured_cmd

    def test_custom_permission_mode(self, runner):
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("tests.behavior.runner.subprocess.run", side_effect=fake_run):
            runner.run_case(
                prompt="test",
                case_id="test-004",
                permission_mode="acceptEdits",
            )

        # Custom permission_mode should pass through
        assert "acceptEdits" in captured_cmd


# ===== run_case result handling =====


class TestRunCaseResult:
    def test_returns_run_result(self, runner):
        with patch(
            "tests.behavior.runner.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ):
            result = runner.run_case(prompt="test", case_id="test-result")

        assert isinstance(result, RunResult)
        assert result.exit_code == 0
        assert result.timed_out is False
        assert result.duration_s >= 0

    def test_dataclass_is_frozen(self):
        # audit be5e71f8 gemini #7: RunResult must be immutable
        result = RunResult(
            exit_code=0,
            jsonl_path=Path("/tmp/x"),
            stderr_path=Path("/tmp/x.err"),
            duration_s=1.0,
            timed_out=False,
        )
        with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
            result.exit_code = 1  # type: ignore[misc]

    def test_timeout_returns_124(self, runner):
        def fake_run_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        with patch("tests.behavior.runner.subprocess.run", side_effect=fake_run_timeout):
            result = runner.run_case(
                prompt="test", case_id="test-timeout", timeout_s=1
            )

        assert result.exit_code == 124  # GNU timeout convention
        assert result.timed_out is True

    def test_jsonl_and_stderr_paths_in_output_dir(self, runner):
        with patch(
            "tests.behavior.runner.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ):
            result = runner.run_case(prompt="test", case_id="my-case-id")

        assert result.jsonl_path.name == "my-case-id.jsonl"
        assert result.stderr_path.name == "my-case-id.stderr"
        # Both inside the runner's output_dir
        assert result.jsonl_path.parent == runner.output_dir
        assert result.stderr_path.parent == runner.output_dir
