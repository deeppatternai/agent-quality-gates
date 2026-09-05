"""Task 1 — Fresh checkout install (smoke).

What: install.sh → aqg_doctor.py → assert exit 0 + 0 WARN + 0 FAIL +
PASS >= baseline (per §3 Task 1).

Pass criteria (sketch §3): doctor exit 0 + PASS count matches
version-baseline + 0 WARN + 0 FAIL.

v1 implementation note: PASS exact baseline depends on which doctor
entries the running version emits. We assert PASS >= a conservative
floor (40, the count just before B-1 shipped) plus 0 WARN/FAIL — that
detects regressions without breaking on legitimate baseline growth
(every new doctor entry would otherwise have to bump fixture before
this task could pass).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner import TaskOutcome  # noqa: E402


CONSERVATIVE_PASS_FLOOR = 40

# The one doctor WARN a fresh checkout is EXPECTED to produce: the rules block is
# appended by hand (README.md:184/186), so no installer can clear it. Matched on
# the condition, not the check-name family, so the sibling "stale rules block"
# WARN — a genuine defect — keeps failing this gate. Wording tracks
# scripts/aqg_doctor.py:588; if that text changes, this stops matching and the
# gate goes back to counting it, which is the fail-safe direction.
_ABSENT_RULES_BLOCK_MARKERS = ("no AQG rules block", "has no AQG rules block")

# Only the hosts whose block is a MANUAL step. `rules_block:cursor` is excluded
# from the exclusion: `install_cursor_support.py` DOES write `aqg.mdc`, so an
# absent cursor rule means the installer failed and the gate must stay red. The
# first version of this filter keyed on the `rules_block:` prefix alone and
# swallowed cursor too, which would have hidden a real installer regression from
# a required PR gate.
_MANUAL_RULES_BLOCK_HOSTS = ("rules_block:claude", "rules_block:codex")


def _is_absent_rules_block(result: dict) -> bool:
    """True only for an absent rules block on a host that installs it BY HAND."""
    if str(result.get("name", "")) not in _MANUAL_RULES_BLOCK_HOSTS:
        return False
    detail = str(result.get("detail", ""))
    return any(m in detail for m in _ABSENT_RULES_BLOCK_MARKERS)


def run(repo: Path) -> TaskOutcome:
    # 5 checks: install.sh exit 0 + doctor exit 0 + FAIL=0 + WARN=0 + PASS>=floor
    # (audit 297dccac gemini #3: FAIL/WARN split for clearer diagnostics)
    checks_total = 5
    checks_passed = 0
    notes_lines: list[str] = []

    install_script = repo / "scripts" / "install.sh"
    if not install_script.is_file():
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes=f"scripts/install.sh missing at {install_script}",
        )

    proc = subprocess.run(
        ["bash", str(install_script), "--force"],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    if proc.returncode == 0:
        checks_passed += 1
        notes_lines.append("install.sh --force: exit 0")
    else:
        notes_lines.append(
            f"install.sh --force: exit {proc.returncode}; tail: {proc.stdout[-300:]}"
        )

    doctor = repo / "scripts" / "aqg_doctor.py"
    if not doctor.is_file():
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=checks_passed,
            required_checks_total=checks_total,
            notes=f"scripts/aqg_doctor.py missing at {doctor}; "
            + "; ".join(notes_lines),
        )

    proc = subprocess.run(
        [sys.executable, str(doctor), "--json"],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        notes_lines.append(
            f"aqg_doctor.py --json exit {proc.returncode}; "
            f"stderr tail: {proc.stderr[-300:]}"
        )
    else:
        checks_passed += 1
        notes_lines.append(f"aqg_doctor.py --json: exit 0")

    pass_count = warn_count = fail_count = -1
    try:
        data = json.loads(proc.stdout)
        results = data.get("results", []) if isinstance(data, dict) else []
        pass_count = sum(
            1 for r in results
            if isinstance(r, dict) and r.get("status") == "PASS"
        )
        # Excluded on purpose, but narrowed to ONE CONDITION rather than to the
        # whole `rules_block:*` family. This task measures whether a FRESH
        # CHECKOUT installs cleanly; the rules block is a documented MANUAL step
        # the installers deliberately do not perform (README.md:184/186 has the
        # user append it by hand), so "installed but no rules block" WARNs by
        # construction on every CI runner. Counting it made a correct install
        # look like a failed one and turned a required PR gate red for a reason
        # no installer change could fix.
        #
        # `aqg_doctor` emits TWO different rules_block conditions
        # (scripts/aqg_doctor.py:588 and :619). Only the ABSENT one is a
        # not-yet-done manual step. A STALE block — present but carrying retired
        # framing — is a real defect, so it stays counted. Keying the exclusion
        # on the name prefix alone would have masked both.
        warn_count = sum(
            1 for r in results
            if isinstance(r, dict) and r.get("status") == "WARN"
            and not _is_absent_rules_block(r)
        )
        fail_count = sum(
            1 for r in results
            if isinstance(r, dict) and r.get("status") == "FAIL"
        )
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        notes_lines.append(
            f"could not parse doctor JSON: {exc}; head: {proc.stdout[:200]}"
        )

    # Audit 297dccac gemini #3 (clarity): split WARN==0 and FAIL==0 into
    # explicit independent checks so a doctor result with FAIL>0 is reported
    # separately from one with WARN>0. Same overall behavior as before
    # (both must hold) but easier to read in failure notes.
    if fail_count == 0 and fail_count != -1:
        checks_passed += 1
        notes_lines.append("doctor: FAIL=0")
    else:
        notes_lines.append(f"doctor: FAIL={fail_count}")

    if warn_count == 0 and warn_count != -1:
        checks_passed += 1
        notes_lines.append("doctor: WARN=0")
    else:
        notes_lines.append(f"doctor: WARN={warn_count}")

    if pass_count >= CONSERVATIVE_PASS_FLOOR:
        checks_passed += 1
        notes_lines.append(
            f"doctor PASS={pass_count} >= floor={CONSERVATIVE_PASS_FLOOR}"
        )
    else:
        notes_lines.append(
            f"doctor PASS={pass_count} < floor={CONSERVATIVE_PASS_FLOOR}"
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
