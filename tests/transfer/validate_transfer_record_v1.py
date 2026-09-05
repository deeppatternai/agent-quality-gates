#!/usr/bin/env python3
"""Schema validator for Transfer Test Pack v1 task records + run summaries.

Implements §4 of the v1 sketch (a2 accepted): per-task record + aggregated
run summary with required provenance block. Used by:

- runner.py to verify each task record is well-formed before emission
- pytest tests to verify _example_run_summary.yaml matches contract
- CI workflow to gate on schema compliance

stdlib only; no PyYAML required (records are dicts at this layer; the
runner / tests are responsible for loading YAML if a YAML on-disk file
is the source).

Exit codes:
  0: success — record/summary valid
  1: validation failed — schema violation listed in stderr
  2: usage error — argparse propagates exit 2 on bad args / missing path
  3: reserved — config or schema error (not currently emitted)
  70: reserved — internal error placeholder for future use
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


# B1 audit 73619b3e #1: bump emitted version to 2 to signal "skipped field
# present" so old strict v1 consumers that reject unknown fields can early-
# detect they need an upgrade. Validator accepts both 1 (pre-B1 records) and
# 2 (B1+ records with skipped field).
SCHEMA_VERSION_ACCEPTED: frozenset[int] = frozenset({1, 2})
SCHEMA_VERSION_CURRENT: int = 2

ALLOWED_TASK_IDS: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6})
ALLOWED_TASK_NAMES: frozenset[str] = frozenset({
    "fresh_checkout",
    "handoff_schema",
    "cross_env",
    "behavior_trigger",
    "audit_reproducibility",
    "owner_cold_transfer",
})
TASK_ID_TO_NAME: dict[int, str] = {
    1: "fresh_checkout",
    2: "handoff_schema",
    3: "cross_env",
    4: "behavior_trigger",
    5: "audit_reproducibility",
    6: "owner_cold_transfer",
}
HARD_GATE_TASK_IDS: frozenset[int] = frozenset({1, 2, 3, 4, 6})
ADVISORY_TASK_IDS: frozenset[int] = frozenset({5})

ALLOWED_RESULT: frozenset[str] = frozenset({"pass", "warn", "fail"})
ALLOWED_OS: frozenset[str] = frozenset({"darwin", "linux", "windows"})
ALLOWED_TASK5_PROVIDER: frozenset[str | None] = frozenset({
    "codex-cli", "gemini-cli", "openai-api", None,
})

GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$")
PY_VERSION_RE = re.compile(r"^3\.\d+(?:\.\d+)?$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REQUIRED_TASK_FIELDS: frozenset[str] = frozenset({
    "schema_version",
    "task_id",
    "task_name",
    "started_at",
    "finished_at",
    "duration_seconds",
    "exit_code",
    "result",
    "required_checks_passed",
    "required_checks_total",
    "boundary_violations",
    "notes",
    "provenance",
})

# B1: v1.1 conceptual milestone — explicit skipped: bool field decouples
# "audit ran" from result enum so a real warning (audit ran, only minor
# findings) is no longer indistinguishable from a skipped advisory state.
# Optional + default False; absent treated as not-skipped (back-compat with v1
# records emitted before B1). schema_version stays at 1 (additive change).
OPTIONAL_TASK_FIELDS: frozenset[str] = frozenset({
    "skipped",
})

ALLOWED_TASK_FIELDS: frozenset[str] = REQUIRED_TASK_FIELDS | OPTIONAL_TASK_FIELDS

REQUIRED_PROVENANCE_FIELDS: frozenset[str] = frozenset({
    "git_sha",
    "repo_url",  # audit 297dccac gpt #5 + gemini #6: cross-fork triage anchor
    "aqg_version",
    "runner_version",
    "os",
    "shell",
    "python_version",
    "fixture_version",
    "artifact_uri",
    "metric_event_id",
    "incident_event_id",
})

OPTIONAL_PROVENANCE_FIELDS: frozenset[str] = frozenset({
    "task5_model_id",
    "task5_provider",
})

ALLOWED_PROVENANCE_FIELDS: frozenset[str] = (
    REQUIRED_PROVENANCE_FIELDS | OPTIONAL_PROVENANCE_FIELDS
)

REQUIRED_SUMMARY_FIELDS: frozenset[str] = frozenset({
    "schema_version",
    "run_id",
    "date",
    "overall_pass_rate",
    "tasks",
    "threshold_strict",
    "threshold_met",
    "task5_advisory",
    "provenance",
})


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


class SchemaError(Exception):
    def __init__(self, violations: list[str]):
        self.violations = tuple(violations)
        super().__init__(self._fmt())

    def _fmt(self) -> str:
        if not self.violations:
            return "transfer record schema violation (no detail)"
        lines = [f"transfer record schema violation ({len(self.violations)} issue(s)):"]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


def _is_strict_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_strict_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _check_iso8601_utc(name: str, value: Any, violations: list[str]) -> None:
    if not isinstance(value, str):
        violations.append(f"{name}: must be str ISO 8601 UTC, got {type(value).__name__}")
        return
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        violations.append(f"{name}: invalid ISO 8601 ({exc}); got {value!r}")
        return
    if parsed.tzinfo is None:
        violations.append(f"{name}: must include UTC timezone (Z or +00:00); got {value!r}")
        return
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        violations.append(
            f"{name}: must be UTC (offset 00:00); got offset {offset}"
        )


def _check_provenance(
    provenance: Any,
    *,
    task_id: int | None,
    violations: list[str],
    task5_audit_ran: bool = False,
) -> None:
    """Validate the provenance block.

    `task5_audit_ran` is computed by the caller from the explicit
    `skipped: bool` field (B1 v1.1) when present, else fallback to the
    implicit `result in {pass, fail}` inference for pre-B1 records.
    """
    if not isinstance(provenance, Mapping):
        violations.append(
            f"provenance: must be mapping, got {type(provenance).__name__}"
        )
        return

    unknown = set(provenance.keys()) - ALLOWED_PROVENANCE_FIELDS
    for k in sorted(unknown):
        violations.append(f"provenance.{k}: unknown field")

    missing = REQUIRED_PROVENANCE_FIELDS - set(provenance.keys())
    for k in sorted(missing):
        violations.append(f"provenance.{k}: missing required field")

    git_sha = provenance.get("git_sha")
    if isinstance(git_sha, str):
        if not GIT_SHA_RE.match(git_sha):
            violations.append(f"provenance.git_sha: must be 40-hex, got {git_sha!r}")
    elif "git_sha" in provenance:
        violations.append(
            f"provenance.git_sha: must be str, got {type(git_sha).__name__}"
        )

    aqg_version = provenance.get("aqg_version")
    if isinstance(aqg_version, str):
        if not SEMVER_RE.match(aqg_version):
            violations.append(
                f"provenance.aqg_version: must match semver, got {aqg_version!r}"
            )
    elif "aqg_version" in provenance:
        violations.append(
            f"provenance.aqg_version: must be str, got {type(aqg_version).__name__}"
        )

    runner_version = provenance.get("runner_version")
    if "runner_version" in provenance and (
        not isinstance(runner_version, str) or not runner_version
    ):
        violations.append("provenance.runner_version: must be non-empty str")

    osname = provenance.get("os")
    if "os" in provenance and osname not in ALLOWED_OS:
        violations.append(
            f"provenance.os: must be one of {sorted(ALLOWED_OS)}, got {osname!r}"
        )

    shell = provenance.get("shell")
    if "shell" in provenance and (not isinstance(shell, str) or not shell):
        violations.append("provenance.shell: must be non-empty str")

    py = provenance.get("python_version")
    if isinstance(py, str):
        if not PY_VERSION_RE.match(py):
            violations.append(
                f"provenance.python_version: must match 3.x or 3.x.y, got {py!r}"
            )
    elif "python_version" in provenance:
        violations.append(
            f"provenance.python_version: must be str, got {type(py).__name__}"
        )

    fv = provenance.get("fixture_version")
    if "fixture_version" in provenance and (not isinstance(fv, str) or not fv):
        violations.append("provenance.fixture_version: must be non-empty str")

    au = provenance.get("artifact_uri")
    if "artifact_uri" in provenance and (not isinstance(au, str) or not au):
        violations.append("provenance.artifact_uri: must be non-empty str")

    repo_url = provenance.get("repo_url")
    if "repo_url" in provenance and (not isinstance(repo_url, str) or not repo_url):
        violations.append("provenance.repo_url: must be non-empty str")

    metric_id = provenance.get("metric_event_id")
    if "metric_event_id" in provenance and metric_id is not None and not (
        isinstance(metric_id, str) and metric_id
    ):
        violations.append(
            "provenance.metric_event_id: must be non-empty str or null"
        )

    incident_id = provenance.get("incident_event_id")
    if "incident_event_id" in provenance and incident_id is not None and not (
        isinstance(incident_id, str) and incident_id
    ):
        violations.append(
            "provenance.incident_event_id: must be non-empty str or null"
        )

    task5_model = provenance.get("task5_model_id", None)
    task5_provider = provenance.get("task5_provider", None)

    if task_id == 5 and task5_audit_ran:
        if task5_model is None or not isinstance(task5_model, str) or not task5_model:
            violations.append(
                "provenance.task5_model_id: required for Task 5 when audit ran "
                "(skipped=false; B1 v1.1 explicit, falls back to "
                "result in {pass, fail} on pre-B1 records); must be non-empty str"
            )
        if task5_provider is None or task5_provider not in ALLOWED_TASK5_PROVIDER:
            violations.append(
                f"provenance.task5_provider: required for Task 5 when audit ran "
                f"(skipped=false; B1 v1.1 explicit, falls back to "
                f"result in {{pass, fail}} on pre-B1 records); must be one of "
                f"{sorted(p for p in ALLOWED_TASK5_PROVIDER if p)}, got {task5_provider!r}"
            )
    else:
        if task5_model is not None and not (
            isinstance(task5_model, str) and task5_model
        ):
            violations.append(
                "provenance.task5_model_id: must be non-empty str or null when present"
            )
        if (
            "task5_provider" in provenance
            and task5_provider is not None
            and task5_provider not in ALLOWED_TASK5_PROVIDER
        ):
            violations.append(
                f"provenance.task5_provider: must be one of "
                f"{sorted(p for p in ALLOWED_TASK5_PROVIDER if p)} or null"
            )


def check_task_record(record: Mapping[str, Any]) -> ValidationResult:
    """Validate a single per-task record (§4 task schema).

    B1 v1.1: optional `skipped: bool` field. When `skipped=true`, the task
    did not actually execute (Task 5 advisory skip is the canonical case).
    Cross-field invariants: skipped=true requires task_id=5 AND result=warn,
    and removes the model_id/provider provenance requirement.
    """
    violations: list[str] = []

    if not isinstance(record, Mapping):
        return ValidationResult(False, (
            f"<root>: must be mapping, got {type(record).__name__}",
        ))

    unknown = set(record.keys()) - ALLOWED_TASK_FIELDS
    for k in sorted(unknown):
        violations.append(f"unknown top-level field: {k!r}")

    missing = REQUIRED_TASK_FIELDS - set(record.keys())
    for k in sorted(missing):
        violations.append(f"missing required field: {k}")

    sv = record.get("schema_version")
    if not _is_strict_int(sv):
        violations.append(f"schema_version: must be int, got {type(sv).__name__}")
    elif sv not in SCHEMA_VERSION_ACCEPTED:
        violations.append(
            f"schema_version: must be in {sorted(SCHEMA_VERSION_ACCEPTED)}, got {sv}"
        )

    task_id = record.get("task_id")
    if not _is_strict_int(task_id) or task_id not in ALLOWED_TASK_IDS:
        violations.append(
            f"task_id: must be int in {sorted(ALLOWED_TASK_IDS)}, got {task_id!r}"
        )
        task_id = None

    task_name = record.get("task_name")
    if task_name not in ALLOWED_TASK_NAMES:
        violations.append(
            f"task_name: must be one of {sorted(ALLOWED_TASK_NAMES)}, got {task_name!r}"
        )
    if isinstance(task_id, int) and task_id in TASK_ID_TO_NAME:
        expected_name = TASK_ID_TO_NAME[task_id]
        if task_name != expected_name:
            violations.append(
                f"task_name ({task_name!r}) does not match task_id={task_id} "
                f"expected {expected_name!r}"
            )

    started_at = record.get("started_at")
    finished_at = record.get("finished_at")
    if "started_at" in record:
        _check_iso8601_utc("started_at", started_at, violations)
    if "finished_at" in record:
        _check_iso8601_utc("finished_at", finished_at, violations)
    if (
        isinstance(started_at, str)
        and isinstance(finished_at, str)
        and "started_at" not in [v.split(":")[0] for v in violations]
        and "finished_at" not in [v.split(":")[0] for v in violations]
    ):
        try:
            s = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            f = dt.datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            if f < s:
                violations.append("finished_at must be >= started_at")
        except ValueError:
            pass

    dur = record.get("duration_seconds")
    if not _is_strict_int(dur):
        violations.append(
            f"duration_seconds: must be int, got {type(dur).__name__}"
        )
    elif dur < 0:
        violations.append(f"duration_seconds: must be >= 0, got {dur}")

    exit_code = record.get("exit_code")
    if not _is_strict_int(exit_code):
        violations.append(f"exit_code: must be int, got {type(exit_code).__name__}")

    result = record.get("result")
    if result not in ALLOWED_RESULT:
        violations.append(
            f"result: must be one of {sorted(ALLOWED_RESULT)}, got {result!r}"
        )
    if isinstance(task_id, int) and task_id in HARD_GATE_TASK_IDS and result == "warn":
        violations.append(
            f"result=warn not allowed for hard-gate task_id={task_id} "
            f"(only Task 5 may warn per §5.1)"
        )

    rcp = record.get("required_checks_passed")
    rct = record.get("required_checks_total")
    if not _is_strict_int(rcp):
        violations.append(
            f"required_checks_passed: must be int, got {type(rcp).__name__}"
        )
    elif rcp < 0:
        violations.append("required_checks_passed: must be >= 0")
    if not _is_strict_int(rct):
        violations.append(
            f"required_checks_total: must be int, got {type(rct).__name__}"
        )
    elif rct < 0:
        violations.append("required_checks_total: must be >= 0")
    if (
        _is_strict_int(rcp)
        and _is_strict_int(rct)
        and rcp > rct
    ):
        violations.append(
            f"required_checks_passed ({rcp}) > required_checks_total ({rct})"
        )

    bv = record.get("boundary_violations")
    if not _is_strict_int(bv):
        violations.append(
            f"boundary_violations: must be int, got {type(bv).__name__}"
        )
    elif bv < 0:
        violations.append("boundary_violations: must be >= 0")

    notes = record.get("notes")
    if "notes" in record and not isinstance(notes, str):
        violations.append(f"notes: must be str, got {type(notes).__name__}")

    # B1 v1.1: explicit skipped flag with cross-field invariants
    skipped = record.get("skipped", False)
    if "skipped" in record and not _is_strict_bool(skipped):
        violations.append(f"skipped: must be bool, got {type(skipped).__name__}")
        skipped = False  # treat as not-skipped for invariant checks
    if skipped is True:
        # Invariant: skipped=true is only valid for Task 5 advisory skip path
        if task_id != 5:
            violations.append(
                f"skipped=true is only valid for task_id=5 (advisory skip); "
                f"got task_id={task_id!r}"
            )
        if result not in {"warn", None}:
            violations.append(
                f"skipped=true requires result=warn (advisory skip path); "
                f"got result={result!r}"
            )

    if "provenance" in record:
        # B1 v1.1: explicit skipped flag is the discriminator for whether the
        # audit actually ran. Pre-B1 records (no skipped key) fall back to the
        # implicit "result in {pass, fail}" inference for backward compat.
        if "skipped" in record and _is_strict_bool(skipped):
            task5_audit_ran = (task_id == 5 and not skipped)
        else:
            task5_audit_ran = (task_id == 5 and result in {"pass", "fail"})
        _check_provenance(
            record["provenance"],
            task_id=task_id if isinstance(task_id, int) else None,
            violations=violations,
            task5_audit_ran=task5_audit_ran,
        )

    return ValidationResult(not violations, tuple(violations))


def check_run_summary(summary: Mapping[str, Any]) -> ValidationResult:
    """Validate the aggregated run summary (§4 summary schema)."""
    violations: list[str] = []

    if not isinstance(summary, Mapping):
        return ValidationResult(False, (
            f"<root>: must be mapping, got {type(summary).__name__}",
        ))

    unknown = set(summary.keys()) - REQUIRED_SUMMARY_FIELDS
    for k in sorted(unknown):
        violations.append(f"unknown top-level field: {k!r}")

    missing = REQUIRED_SUMMARY_FIELDS - set(summary.keys())
    for k in sorted(missing):
        violations.append(f"missing required field: {k}")

    sv = summary.get("schema_version")
    if not _is_strict_int(sv) or sv not in SCHEMA_VERSION_ACCEPTED:
        violations.append(
            f"schema_version: must be int in {sorted(SCHEMA_VERSION_ACCEPTED)}, got {sv!r}"
        )

    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        violations.append(
            f"run_id: must match {RUN_ID_RE.pattern}, got {run_id!r}"
        )

    date = summary.get("date")
    if not isinstance(date, str) or not DATE_RE.match(date):
        violations.append(f"date: must match YYYY-MM-DD, got {date!r}")

    pr = summary.get("overall_pass_rate")
    if not isinstance(pr, (int, float)) or isinstance(pr, bool):
        violations.append(
            f"overall_pass_rate: must be number 0..1, got {type(pr).__name__}"
        )
    elif not (0.0 <= float(pr) <= 1.0):
        violations.append(f"overall_pass_rate: must be 0..1, got {pr}")

    tasks = summary.get("tasks")
    if not isinstance(tasks, list):
        violations.append(f"tasks: must be list, got {type(tasks).__name__}")
    else:
        if len(tasks) != 6:
            violations.append(
                f"tasks: must have exactly 6 entries (Task 1-6), got {len(tasks)}"
            )
        seen_ids: set[int] = set()
        for i, t in enumerate(tasks):
            sub = check_task_record(t if isinstance(t, Mapping) else {})
            for v in sub.violations:
                violations.append(f"tasks[{i}].{v}")
            if isinstance(t, Mapping):
                tid = t.get("task_id")
                if isinstance(tid, int) and tid in ALLOWED_TASK_IDS:
                    if tid in seen_ids:
                        violations.append(f"tasks[{i}]: duplicate task_id={tid}")
                    seen_ids.add(tid)
        missing_ids = ALLOWED_TASK_IDS - seen_ids
        if missing_ids:
            violations.append(f"tasks: missing task_id(s) {sorted(missing_ids)}")

    ts = summary.get("threshold_strict")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        violations.append(
            f"threshold_strict: must be number, got {type(ts).__name__}"
        )
    elif float(ts) != 1.0:
        violations.append(
            f"threshold_strict: v1 fixed at 1.0 (strict-all-hard), got {ts}"
        )

    tm = summary.get("threshold_met")
    if not _is_strict_bool(tm):
        violations.append(
            f"threshold_met: must be bool, got {type(tm).__name__}"
        )

    advisory = summary.get("task5_advisory")
    if advisory not in ALLOWED_RESULT:
        violations.append(
            f"task5_advisory: must be one of {sorted(ALLOWED_RESULT)}, got {advisory!r}"
        )

    # B1 audit 73619b3e #3: summary.task5_advisory must match Task 5 record's
    # result. Drift between the two would mislead consumers reading either
    # the per-task list or the summary roll-up.
    if isinstance(tasks, list):
        task5_record = next(
            (
                t for t in tasks
                if isinstance(t, Mapping) and t.get("task_id") == 5
            ),
            None,
        )
        if task5_record is not None:
            task5_result = task5_record.get("result")
            if (
                advisory in ALLOWED_RESULT
                and task5_result in ALLOWED_RESULT
                and advisory != task5_result
            ):
                violations.append(
                    f"task5_advisory ({advisory!r}) must match the Task 5 "
                    f"record's result ({task5_result!r}); drift between "
                    f"summary and per-task list"
                )

    if "provenance" in summary:
        _check_provenance(summary["provenance"], task_id=None, violations=violations)

    return ValidationResult(not violations, tuple(violations))


def assert_valid_record(record: Mapping[str, Any]) -> None:
    result = check_task_record(record)
    if not result.is_valid:
        raise SchemaError(list(result.violations))


def assert_valid_summary(summary: Mapping[str, Any]) -> None:
    result = check_run_summary(summary)
    if not result.is_valid:
        raise SchemaError(list(result.violations))


def _load_yaml_or_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                f"PyYAML required to load {path.suffix} but is not installed: {exc}"
            ) from exc
        return yaml.safe_load(text)
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_transfer_record_v1",
        description=(
            "Validate a Transfer Test Pack v1 task record or run summary "
            "against §4 schema."
        ),
    )
    parser.add_argument("path", help="path to .yaml/.yml/.json record or summary file")
    parser.add_argument(
        "--kind",
        choices=("task", "summary"),
        default="task",
        help="record type (default: task)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output JSON result",
    )
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.is_file():
        print(f"ERROR: not a file: {path}", file=sys.stderr)
        return 2

    try:
        loaded = _load_yaml_or_json(path)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: failed to load {path}: {exc}", file=sys.stderr)
        return 2

    if not isinstance(loaded, Mapping):
        print(
            f"ERROR: top-level must be mapping, got {type(loaded).__name__}",
            file=sys.stderr,
        )
        return 1

    if args.kind == "task":
        result = check_task_record(loaded)
    else:
        result = check_run_summary(loaded)

    if args.json:
        print(json.dumps({
            "kind": args.kind,
            "is_valid": result.is_valid,
            "violations": list(result.violations),
        }, indent=2))
    else:
        if result.is_valid:
            print(f"OK: {args.kind} valid")
        else:
            print(f"FAIL: {args.kind} invalid")
            for v in result.violations:
                print(f"  - {v}")

    return 0 if result.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
