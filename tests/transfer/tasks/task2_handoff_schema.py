"""Task 2 — Handoff schema fixture validation.

What: load tests/transfer/fixtures/handoff_manifest_v1.json (and v2 if
present) → run scripts/validate_handoff_manifest.py with high-stakes
+ --no-remote-check (hermetic CI) → assert validator exit 0 + 0
schema_errors + 0 redaction_errors per §3 Task 2.

Pass criteria: validator exit 0 on each fixture (v1 + v2 if present).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner import TaskOutcome  # noqa: E402


FIXTURE_FILES = ("handoff_manifest_v1.json", "handoff_manifest_v2.json")


def run(repo: Path) -> TaskOutcome:
    fixtures_dir = repo / "tests" / "transfer" / "fixtures"
    validator = repo / "scripts" / "validate_handoff_manifest.py"

    if not validator.is_file():
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=1,
            notes=f"validate_handoff_manifest.py missing at {validator}",
        )

    fixture_paths = [
        fixtures_dir / name for name in FIXTURE_FILES
        if (fixtures_dir / name).is_file()
    ]
    if not fixture_paths:
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=1,
            notes=(
                f"no handoff manifest fixtures found in {fixtures_dir}; "
                f"expected at least one of {list(FIXTURE_FILES)}"
            ),
        )

    checks_total = len(fixture_paths)
    checks_passed = 0
    notes_lines: list[str] = []

    for fp in fixture_paths:
        proc = subprocess.run(
            [
                sys.executable,
                str(validator),
                str(fp),
                "--high-stakes",
                "--no-remote-check",
            ],
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0:
            checks_passed += 1
            notes_lines.append(f"{fp.name}: validator exit 0")
        else:
            notes_lines.append(
                f"{fp.name}: validator exit {proc.returncode}; "
                f"output tail: {proc.stdout[-300:]}"
            )

    if checks_passed == checks_total:
        return TaskOutcome(
            result="pass",
            exit_code=0,
            required_checks_passed=checks_passed,
            required_checks_total=checks_total,
            notes="; ".join(notes_lines),
        )
    return TaskOutcome(
        result="fail",
        exit_code=1,
        required_checks_passed=checks_passed,
        required_checks_total=checks_total,
        notes="; ".join(notes_lines),
    )
