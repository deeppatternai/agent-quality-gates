"""Task 3 — Cross-env synthetic smoke.

What: invoke scripts/aqg_chaos.py run-all → assert exit 0 + every
scenario's expected outcome per §3 Task 3.

Sketch §3 originally described "HOME/XDG mock + PATH mock + GIT_CEILING"
as conceptual env-mock examples. The actual aqg_chaos.py scenarios
(doctor regression / WIP corrupt / metrics gating / install boundaries)
are the real curated subset shipped in the repo. v1 runs the full
chaos scenario set; if Owner wants a strict 3-scenario subset later,
the run-list can be parameterized via aqg_chaos.py run <name>.

Pass criteria: chaos run-all exit 0.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner import TaskOutcome  # noqa: E402


def run(repo: Path) -> TaskOutcome:
    chaos = repo / "scripts" / "aqg_chaos.py"
    if not chaos.is_file():
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=1,
            notes=f"scripts/aqg_chaos.py missing at {chaos}",
        )

    proc = subprocess.run(
        [sys.executable, str(chaos), "run-all"],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    notes = proc.stdout[-500:] if proc.stdout else "no chaos output"

    if proc.returncode == 0:
        return TaskOutcome(
            result="pass",
            exit_code=0,
            required_checks_passed=1,
            required_checks_total=1,
            notes=f"aqg_chaos run-all: exit 0; tail: {notes}",
        )
    return TaskOutcome(
        result="fail",
        exit_code=1,
        required_checks_passed=0,
        required_checks_total=1,
        notes=f"aqg_chaos run-all: exit {proc.returncode}; tail: {notes}",
    )
