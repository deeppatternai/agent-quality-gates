#!/usr/bin/env python3
"""Self-test for example_check.py covering the §1.4 exit-code contract +
§5 path portability.

Exercises all 5 exit states (0/1/2/3/70) so a new author copying this
template gets a working test that demonstrates the full §1.4 contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "example_check.py"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_default_run_exits_zero() -> None:
    rc, stdout, _ = _run("--repo", str(REPO_ROOT))
    assert rc == 0, f"expected exit 0, got {rc}"
    assert "pass" in stdout, f"expected 'pass' in stdout, got: {stdout!r}"


def test_force_fail_exits_one() -> None:
    rc, stdout, _ = _run("--repo", str(REPO_ROOT), "--fail")
    assert rc == 1, f"expected exit 1, got {rc}"
    assert "fail" in stdout


def test_unknown_flag_exits_usage() -> None:
    rc, _, stderr = _run("--no-such-flag")
    assert rc == 2, f"expected exit 2 (usage), got {rc}"
    assert "unrecognized" in stderr or "usage" in stderr.lower()


def test_bad_config_exits_schema() -> None:
    rc, stdout, _ = _run("--bad-config")
    assert rc == 3, f"expected exit 3 (schema), got {rc}"
    assert "schema_error" in stdout


def test_crash_exits_internal() -> None:
    rc, _, stderr = _run("--repo", str(REPO_ROOT), "--crash")
    assert rc == 70, f"expected exit 70 (internal), got {rc}"
    assert "internal error" in stderr


def test_json_output_parses() -> None:
    rc, stdout, _ = _run("--repo", str(REPO_ROOT), "--json")
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["status"] == "pass"
    assert payload["repo"]


def test_required_file_missing_exits_one() -> None:
    rc, stdout, _ = _run(
        "--repo",
        str(REPO_ROOT),
        "--required-file",
        "definitely-not-a-real-path.xyz",
    )
    assert rc == 1, f"expected exit 1 (required file missing), got {rc}"
    assert "required file missing" in stdout


def test_required_file_present_exits_zero() -> None:
    rc, stdout, _ = _run(
        "--repo",
        str(REPO_ROOT),
        "--required-file",
        "VERSION",
    )
    assert rc == 0, f"expected exit 0 (VERSION exists), got {rc}"
    assert "pass" in stdout


def test_invalid_repo_exits_usage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rc, _, stderr = _run("--repo", str(Path(tmp) / "nonexistent"))
        assert rc == 2, f"expected exit 2 (usage), got {rc}"
        assert "usage error" in stderr.lower() or "does not exist" in stderr


if __name__ == "__main__":
    test_default_run_exits_zero()
    test_force_fail_exits_one()
    test_unknown_flag_exits_usage()
    test_bad_config_exits_schema()
    test_crash_exits_internal()
    test_json_output_parses()
    test_required_file_missing_exits_one()
    test_required_file_present_exits_zero()
    test_invalid_repo_exits_usage()
    print("aqg-example self-test: 9/9 PASS")
