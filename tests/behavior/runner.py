"""claude -p subprocess runner for behavior tests.

Implements baseline config from sketch v3 §3.1:
- claude -p --output-format stream-json --no-session-persistence --max-budget-usd ...
- --add-dir <fixture> --permission-mode default --model claude-sonnet-4-6
- --disallowed-tools Agent (Q-Spike-4: cost reduction without trigger loss)
- --verbose (mandatory for stream-json output)

NOT included (v3 verified incompatible / costlier):
- --bare (Q-Spike-1: AQG skills not loaded)
- --system-prompt-file (Q-Spike-2: triggers but more turns/cost)

⚠️ Hermeticity caveat (audit be5e71f8 convergent finding):
This runner inherits the caller's HOME, so the subprocess reads ~/.claude/skills
(developer-installed skills + plugins). For full isolation, downstream PR-2 CI
should run in Docker with a clean HOME containing only fixture-curated skills.
For local development this means behavior results may vary by developer machine
unless extractor diagnostics.init_skills_loaded is validated against an allowlist
(see decide_status step 3a baseline check).

Usage:
    runner = ClaudeRunner(fixture_aqg_dir=Path("/path/to/aqg-clone"))
    result = runner.run_case(prompt="...", per_run_budget_usd=0.50, timeout_s=120)
    # result is RunResult dataclass; pass to extractor.extract_skill_calls(result.jsonl_path)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Allowlist (sketch v3 §9 provider boundary).
ALLOWED_MODELS = ("claude-sonnet-4-6", "claude-haiku-4-5-20251001")
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT_S = 120  # sketch v3 §3.1; audit `717918e8` finding 60s too tight


@dataclass(frozen=True)
class RunResult:
    """Outcome of one claude -p subprocess invocation. Immutable per Python rules."""

    exit_code: int
    jsonl_path: Path
    stderr_path: Path
    duration_s: float
    timed_out: bool


class ClaudeRunner:
    """Runs claude -p subprocess with sketch v3 baseline config."""

    def __init__(
        self,
        fixture_aqg_dir: Path,
        model: str = DEFAULT_MODEL,
        claude_bin: str = "claude",
        output_dir: Optional[Path] = None,
    ) -> None:
        if model not in ALLOWED_MODELS:
            raise ValueError(
                f"model={model!r} not in allowlist {ALLOWED_MODELS}; "
                "extend allowlist via ADR per sketch v3 §9 provider boundary"
            )
        if not fixture_aqg_dir.exists():
            raise FileNotFoundError(
                f"fixture_aqg_dir={fixture_aqg_dir!r} not found; "
                "must be sanitized AQG clone (sketch v3 §9 isolated workspace)"
            )
        if shutil.which(claude_bin) is None:
            raise FileNotFoundError(
                f"claude CLI not found in PATH (looked for {claude_bin!r}); "
                "install Claude Code CLI per sketch v3 §3.1 baseline"
            )
        self.fixture_aqg_dir = fixture_aqg_dir
        self.model = model
        self.claude_bin = claude_bin
        self.output_dir = output_dir or Path(tempfile.mkdtemp(prefix="aqg-behavior-"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run_case(
        self,
        prompt: str,
        case_id: str = "unnamed",
        per_run_budget_usd: float = 0.50,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        permission_mode: str = "default",
    ) -> RunResult:
        """Run one trigger prompt; capture stream-json + stderr; report exit + duration.

        Subprocess gets mocked HOME-style env to keep auth clean (we still use the
        host claude binary which reads ~/.claude). For full isolation use a clean
        cwd and rely on --no-session-persistence to prevent session leak.
        """
        jsonl_path = self.output_dir / f"{case_id}.jsonl"
        stderr_path = self.output_dir / f"{case_id}.stderr"

        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--no-session-persistence",
            "--max-budget-usd",
            f"{per_run_budget_usd:.4f}",
            "--add-dir",
            str(self.fixture_aqg_dir),
            "--permission-mode",
            permission_mode,
            "--model",
            self.model,
            "--disallowed-tools",
            "Agent",
            "--verbose",
        ]

        # Run from a clean tmp cwd to avoid agent picking up the host repo context.
        tmp_cwd = Path(tempfile.mkdtemp(prefix="aqg-behavior-cwd-"))
        try:
            timed_out = False
            start = _monotonic()
            try:
                with jsonl_path.open("wb") as out_fh, stderr_path.open("wb") as err_fh:
                    completed = subprocess.run(
                        cmd,
                        cwd=str(tmp_cwd),
                        stdout=out_fh,
                        stderr=err_fh,
                        timeout=timeout_s,
                        check=False,
                        env=_subprocess_env(),
                    )
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = 124  # GNU timeout convention; distinguishable from 0/1/2
            duration_s = _monotonic() - start
        finally:
            shutil.rmtree(tmp_cwd, ignore_errors=True)

        return RunResult(
            exit_code=exit_code,
            jsonl_path=jsonl_path,
            stderr_path=stderr_path,
            duration_s=duration_s,
            timed_out=timed_out,
        )


def _subprocess_env() -> dict[str, str]:
    """Forward minimal env to subprocess; strip AQG_METRICS to avoid double-recording."""
    env = dict(os.environ)
    env.pop("AQG_METRICS", None)
    return env


def _monotonic() -> float:
    """time.monotonic() shim for testability."""
    import time

    return time.monotonic()
