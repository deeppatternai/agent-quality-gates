#!/usr/bin/env python3
"""Regression tests for Claude Code warn-only hook behavior."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def run_cmd(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd or REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class ClaudeWarnOnlyHookTest(unittest.TestCase):
    def test_settings_command_prints_missing_aqg_root_hint(self) -> None:
        payload = json.loads(
            (REPO / "agent-packs/claude-code/hooks/settings.warn-only.example.json").read_text(encoding="utf-8")
        )
        command = payload["hooks"]["Stop"][0]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as tmp_s:
            env = os.environ.copy()
            env.pop("AQG_ROOT", None)
            env["CLAUDE_PROJECT_DIR"] = tmp_s
            proc = run_cmd(["bash", "-c", command], env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("skip: AQG_ROOT is not set", proc.stderr)
            self.assertNotIn("/dev/null", proc.stderr)

    def test_warn_only_hook_accepts_linked_worktree_git_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            base = tmp / "base"
            linked = tmp / "linked"
            base.mkdir()
            self.assertEqual(run_cmd(["git", "init"], cwd=base).returncode, 0)
            self.assertEqual(
                run_cmd(
                    [
                        "git",
                        "-c",
                        "user.name=AQG Test",
                        "-c",
                        "user.email=aqg-test@example.invalid",
                        "commit",
                        "--allow-empty",
                        "-m",
                        "init",
                    ],
                    cwd=base,
                ).returncode,
                0,
            )
            self.assertEqual(run_cmd(["git", "worktree", "add", str(linked)], cwd=base).returncode, 0)
            self.assertTrue((linked / ".git").is_file())

            (linked / "quality-gates.json").write_text(
                (REPO / "quality-gates.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (linked / ".aqg").mkdir()
            (linked / ".aqg/pr-body.md").write_text(
                (REPO / "tests/fixtures/pr_body/valid.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["AQG_ROOT"] = str(REPO)
            env["CLAUDE_PROJECT_DIR"] = str(linked)
            proc = run_cmd(
                ["bash", "agent-packs/claude-code/hooks/run_warn_only.sh", str(linked)],
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("running gates ->", proc.stderr)
            self.assertNotIn("is not a git worktree", proc.stderr)


if __name__ == "__main__":
    unittest.main()

