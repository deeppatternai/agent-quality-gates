#!/usr/bin/env python3
"""Self-test for aqg_decision_capture.py — smokes format / commit / query / validate
end-to-end via subprocess (the real CLI surface, stdlib only).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "aqg_decision_capture.py"


def _run(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True, encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_format_emits_six_field_line() -> None:
    rc, out, err = _run(
        "format", "--actor", "owner", "--decision", "d",
        "--rationale", "r", "--basis", "无",
    )
    assert rc == 0, err
    assert out.strip().count(" | ") == 5, out  # 6 fields → 5 separators


def test_format_rejects_agent_without_permanent_basis() -> None:
    rc, _, _ = _run(
        "format", "--actor", "agent", "--decision", "d",
        "--rationale", "r", "--basis", "无",
    )
    assert rc == 2  # agent rows need a permanent basis pointer


def test_query_and_validate_on_tmp_log() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "docs" / "decisions"
        d.mkdir(parents=True)
        (d / "LOG.md").write_text(
            "# log\n\n## 日志\n\n2026-06-23 | owner | d | r | PR#1 | 无\n",
            encoding="utf-8",
        )
        rc, out, err = _run("query", "--repo", tmp, "--actor", "owner")
        assert rc == 0 and "<decision-data>" in out, err + out
        rc, out, err = _run("validate", "--repo", tmp)
        assert rc == 0, err + out


def test_commit_appends_then_validate_clean() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "docs" / "decisions"
        d.mkdir(parents=True)
        (d / "LOG.md").write_text("# log\n\n## 日志\n\n", encoding="utf-8")
        rc, out, err = _run(
            "commit", "--repo", tmp, "--actor", "owner",
            "--decision", "d", "--rationale", "r", "--basis", "无",
        )
        assert rc == 0, err + out
        # basis "无" kept (backward compat); supersedes now defaults to neutral "-"
        assert "d | r | 无 | -" in (d / "LOG.md").read_text(encoding="utf-8")
        rc, out, err = _run("validate", "--repo", tmp)
        assert rc == 0, err + out  # committed line is well-formed + leak-free


def test_commit_fail_closed_leaves_log_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "docs" / "decisions"
        d.mkdir(parents=True)
        seed = "# log\n\n## 日志\n\n"
        (d / "LOG.md").write_text(seed, encoding="utf-8")
        rc, _out, _err = _run(  # agent w/o permanent basis → rejected pre-write
            "commit", "--repo", tmp, "--actor", "agent",
            "--decision", "d", "--rationale", "r", "--basis", "无",
        )
        assert rc == 2
        assert (d / "LOG.md").read_text(encoding="utf-8") == seed  # unchanged


if __name__ == "__main__":
    test_format_emits_six_field_line()
    test_format_rejects_agent_without_permanent_basis()
    test_query_and_validate_on_tmp_log()
    test_commit_appends_then_validate_clean()
    test_commit_fail_closed_leaves_log_unchanged()
    print("aqg-decision-capture self-test: 5/5 PASS")
