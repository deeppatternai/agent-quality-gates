#!/usr/bin/env python3
"""Transfer Test Pack v1 runner — single CLI entry point.

Dispatches Tasks 1-6 (sketch a2 §3 + Owner-default §8), aggregates
results into the §4 run summary schema, and emits to stdout / file.

Posture (Owner default):
- Tasks 1-4 + 6 = hard gate (strict-all-pass, 0 warn allowed)
- Task 5 = non-blocking advisory (warn allowed; fail still blocks per §5.4)
- threshold_strict fixed at 1.0 in v1
- threshold_met = (strict_pass_rate==1.0) AND (boundary_violations==0)
                  AND (task5_advisory != fail)

CLI:
    python3 tests/transfer/runner.py
        # runs Tasks 1-6 with defaults; emits run summary YAML to stdout
    python3 tests/transfer/runner.py --json
        # JSON output instead of YAML
    python3 tests/transfer/runner.py --tasks 1,2,3
        # subset run; result still validates as full schema (missing tasks
        # emit `result: warn` placeholder + corresponding diagnostic note)
    python3 tests/transfer/runner.py --output run_summary.yaml
        # write summary to file (in addition to stdout)
    python3 tests/transfer/runner.py --skip-task5
        # skip Task 5 entirely (provider unavailable / no internet);
        # task5_advisory becomes "warn" with note explaining skip
    python3 tests/transfer/runner.py --record-metric --actor ci-bot
        # also emit an aqg_metrics record (tool=transfer); provenance
        # metric_event_id = transfer-<run_id>. Forces --record-metrics on
        # the inner metrics call so AQG_METRICS env opt-in is bypassed.
    python3 tests/transfer/runner.py --raise-incident --actor ci-bot
        # on threshold_met=false, write an incident record to
        # docs/incidents/ via aqg_incident_index; provenance
        # incident_event_id = <date>-transfer-pack-fail-<run-id>.

Exit codes:
  0: success — threshold_met=true (all hard tasks passed + 0 boundary violations)
  1: threshold_met=false — at least one hard task failed or task5 fail or boundary violation
  2: usage error — argparse propagates exit 2 on bad args / missing path
  3: reserved — config or schema error (not currently emitted)
  70: reserved — internal error placeholder for future use
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

RUNNER_VERSION = "0.1.0"
SCHEMA_VERSION = 2  # B1: explicit `skipped: bool` field on task records
FIXTURE_VERSION = "v1.0.0"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRANSFER_DIR = REPO_ROOT / "tests" / "transfer"

sys.path.insert(0, str(TRANSFER_DIR))


# ===== Task registry =====

TASK_ID_TO_NAME: dict[int, str] = {
    1: "fresh_checkout",
    2: "handoff_schema",
    3: "cross_env",
    4: "behavior_trigger",
    5: "audit_reproducibility",
    6: "owner_cold_transfer",
}
HARD_GATE_IDS: frozenset[int] = frozenset({1, 2, 3, 4, 6})
ADVISORY_IDS: frozenset[int] = frozenset({5})
ALL_IDS: frozenset[int] = frozenset(TASK_ID_TO_NAME.keys())


@dataclass
class TaskOutcome:
    """In-memory result returned by each task module's run() function.

    B1 audit 73619b3e #2: `skipped: bool` is explicit. The task module is
    the source of truth for whether the task actually executed. The runner
    copies this field into the emitted record without re-inferring from
    outcome shape (which would re-introduce the v1 ambiguity that B1 set
    out to remove).
    """
    result: str  # "pass" | "warn" | "fail"
    exit_code: int
    required_checks_passed: int
    required_checks_total: int
    boundary_violations: int = 0
    notes: str = ""
    task5_model_id: str | None = None
    task5_provider: str | None = None
    skipped: bool = False  # B1: True only on Task 5 advisory-skip path


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(d: dt.datetime) -> str:
    return d.isoformat()


def _git_sha(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        sha = out.stdout.strip()
        if len(sha) == 40:
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    return "0" * 40


def _repo_url(repo: Path) -> str:
    """Audit 297dccac gpt #5 + gemini #6: cross-fork triage anchor.

    Resolved from `git remote get-url origin`. Returns 'unknown://' when
    no origin is configured (e.g. detached / local-only checkout).
    """
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        url = out.stdout.strip()
        if url:
            return url
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown://"


def _aqg_version(repo: Path) -> str:
    vfile = repo / "VERSION"
    if vfile.is_file():
        return vfile.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _os_name() -> str:
    p = platform.system().lower()
    if p == "darwin":
        return "darwin"
    if p == "linux":
        return "linux"
    if p == "windows":
        return "windows"
    return "linux"


def _shell_name() -> str:
    sh = os.environ.get("SHELL", "")
    if not sh:
        return "unknown"
    return Path(sh).name


def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _make_provenance(
    repo: Path,
    *,
    artifact_uri: str,
    task5_model_id: str | None = None,
    task5_provider: str | None = None,
    metric_event_id: str | None = None,
    incident_event_id: str | None = None,
) -> dict[str, Any]:
    return {
        "git_sha": _git_sha(repo),
        "repo_url": _repo_url(repo),
        "aqg_version": _aqg_version(repo),
        "runner_version": RUNNER_VERSION,
        "os": _os_name(),
        "shell": _shell_name(),
        "python_version": _python_version(),
        "task5_model_id": task5_model_id,
        "task5_provider": task5_provider,
        "fixture_version": FIXTURE_VERSION,
        "artifact_uri": artifact_uri,
        "metric_event_id": metric_event_id,
        "incident_event_id": incident_event_id,
    }


def _make_skipped_record(
    task_id: int,
    note: str,
    repo: Path,
    artifact_uri: str,
) -> dict[str, Any]:
    """Build a placeholder record for a task that was not run.

    Per §5.1: hard-gate tasks (1-4, 6) have no `warn` state — a skipped
    hard task is a `fail` so the threshold cannot be silently bypassed
    by selective --tasks invocations. Task 5 may legitimately `warn` on
    skip (advisory).

    B1 v1.1: emits explicit `skipped: true` for the Task 5 advisory skip
    path. Hard-gate tasks treated as fail keep `skipped: false` because
    they are NOT the advisory-skip semantic — they are forced-fail
    placeholders that exist precisely to surface the partial run.
    """
    now = _now_utc()
    if task_id in HARD_GATE_IDS:
        result = "fail"
        exit_code = 1
        skipped = False  # forced fail (subset run); not advisory skip
    else:
        result = "warn"
        exit_code = 0
        skipped = True  # Task 5 advisory skip
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "task_name": TASK_ID_TO_NAME[task_id],
        "started_at": _iso(now),
        "finished_at": _iso(now),
        "duration_seconds": 0,
        "exit_code": exit_code,
        "result": result,
        "required_checks_passed": 0,
        "required_checks_total": 0,
        "boundary_violations": 0,
        "notes": note,
        "skipped": skipped,
        "provenance": _make_provenance(repo, artifact_uri=artifact_uri),
    }


def _import_task_module(task_id: int):
    name = f"tasks.task{task_id}_{TASK_ID_TO_NAME[task_id]}"
    module = __import__(name, fromlist=["run"])
    return module


def _execute_task(
    task_id: int,
    repo: Path,
    artifact_uri: str,
    *,
    task5_skip: bool = False,
) -> dict[str, Any]:
    started = _now_utc()
    t0 = time.monotonic()

    if task_id == 5 and task5_skip:
        return _make_skipped_record(
            task_id,
            note="Task 5 skipped via --skip-task5 (provider unavailable / hermetic CI run)",
            repo=repo,
            artifact_uri=artifact_uri,
        )

    try:
        module = _import_task_module(task_id)
        outcome: TaskOutcome = module.run(repo)
    except Exception as exc:
        finished = _now_utc()
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "task_name": TASK_ID_TO_NAME[task_id],
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "duration_seconds": int((finished - started).total_seconds()),
            "exit_code": 70,
            "result": "fail",
            "required_checks_passed": 0,
            "required_checks_total": 0,
            "boundary_violations": 0,
            "notes": f"INTERNAL ERROR: {type(exc).__name__}: {exc}",
            "skipped": False,  # B1: internal error is a real fail, not an advisory skip
            "provenance": _make_provenance(repo, artifact_uri=artifact_uri),
        }

    finished = _now_utc()
    duration = max(0, int(time.monotonic() - t0))

    # B1 audit 73619b3e #2: copy outcome.skipped explicitly. The task
    # module is the source of truth — runner does NOT re-infer from outcome
    # shape (which would re-introduce the v1 ambiguity that B1 removes).
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "task_name": TASK_ID_TO_NAME[task_id],
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_seconds": duration,
        "exit_code": outcome.exit_code,
        "result": outcome.result,
        "required_checks_passed": outcome.required_checks_passed,
        "required_checks_total": outcome.required_checks_total,
        "boundary_violations": outcome.boundary_violations,
        "notes": outcome.notes,
        "skipped": outcome.skipped,
        "provenance": _make_provenance(
            repo,
            artifact_uri=artifact_uri,
            task5_model_id=outcome.task5_model_id,
            task5_provider=outcome.task5_provider,
        ),
    }


# ===== A2: aqg_metrics + aqg_incident_index integration =====


_INCIDENT_SLUG_SAFE_RE = re.compile(r"[^a-z0-9.\-]+")


def emit_metric_record(
    repo: Path,
    run_id: str,
    summary: dict[str, Any],
    *,
    actor: str = "ci-bot",
) -> Optional[str]:
    """Subprocess-call aqg_metrics record --json --record-metrics; return
    metric_event_id (stable derivation from run_id) or None on failure.

    Honors the metrics opt-in by passing --record-metrics so the call is
    not silently no-op'd. Caller (transfer runner) only invokes this when
    --record-metric flag is set.

    Audit 299b566d #1: writes the event_id INTO the metric record so the
    provenance.metric_event_id can be join-keyed back to the ledger row.

    Audit 299b566d #2: catches subprocess.TimeoutExpired + OSError + emits
    a stderr warning so opt-in failures are operator-visible (not silent).
    """
    metrics_script = repo / "scripts" / "aqg_metrics.py"
    if not metrics_script.is_file():
        print(
            f"[transfer.emit_metric_record] skipped: {metrics_script} not found",
            file=sys.stderr,
        )
        return None
    metric_event_id = f"transfer-{run_id}"
    record = {
        "schema_version": 1,
        "ts": _iso(_now_utc()),
        "tool": "transfer",
        "result": "pass" if summary["threshold_met"] else "fail",
        "exit_code": 0 if summary["threshold_met"] else 1,
        "actor": actor,
        "event_id": metric_event_id,  # audit #1: provenance join key
        "marker": "auto-recorded-by-aqg-metrics",
    }
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(metrics_script),
                "record",
                "--json",
                "--record-metrics",
            ],
            input=json.dumps(record),
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[transfer.emit_metric_record] timeout after 10s — metric not recorded",
            file=sys.stderr,
        )
        return None
    except OSError as exc:
        print(
            f"[transfer.emit_metric_record] OSError: {exc} — metric not recorded",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        print(
            f"[transfer.emit_metric_record] aqg_metrics exit {proc.returncode}: "
            f"{proc.stderr.strip()[:200]} — metric not recorded",
            file=sys.stderr,
        )
        return None
    return metric_event_id


def raise_incident(
    repo: Path,
    run_id: str,
    summary: dict[str, Any],
    *,
    actor: str = "ci-bot",
    severity: str = "P3",
) -> Optional[str]:
    """Subprocess-call aqg_incident_index record --json on threshold_met=false;
    return incident_event_id (`<date>-<slug>`) or None.

    Caller (transfer runner) only invokes this when --raise-incident flag is set
    AND summary['threshold_met'] is False. Hard write to docs/incidents/; no
    opt-in env var (incident_index does not have one). Use the flag as the
    opt-in surface to avoid noise during local dev.

    Audit 299b566d #4: slug appends sha256-first10 of run_id so two long
    run_ids sharing the first 60 sanitized chars produce distinct files
    (matrix CI / generated run_id with shared prefix would otherwise collide).

    Audit 299b566d #2: catches subprocess.TimeoutExpired + OSError + emits
    a stderr warning so opt-in failures are operator-visible.
    """
    incident_script = repo / "scripts" / "aqg_incident_index.py"
    if not incident_script.is_file():
        print(
            f"[transfer.raise_incident] skipped: {incident_script} not found",
            file=sys.stderr,
        )
        return None
    date = _now_utc().date().isoformat()
    slug_run_id_full = _INCIDENT_SLUG_SAFE_RE.sub("-", run_id.lower()).strip("-")
    slug_run_id = slug_run_id_full[:48]  # leave room for hash suffix + prefix
    run_id_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10]
    slug = f"transfer-pack-fail-{slug_run_id}-{run_id_hash}"[:80]
    record = {
        "schema_version": 1,
        "date": date,
        "slug": slug,
        "title": f"Transfer Test Pack threshold not met (run {run_id})",
        "severity": severity,
        "detection_source": "scheduled_check",
        "impact_scope": "internal",
        "actor": actor,
        "summary": (
            f"Transfer Test Pack run {run_id} threshold_met=false; "
            f"overall_pass_rate={summary.get('overall_pass_rate')}; "
            f"task5_advisory={summary.get('task5_advisory')}"
        ),
        "root_cause": "TODO — investigate per-task notes in run_summary.yaml",
        "resolution": (
            "TODO — fix failing task(s) and re-run; update transfer pack "
            "fixtures if intentional rotation"
        ),
        "boundaries": (
            "no production write / no branch protection bypass / no secrets "
            "touched / no Owner-only action authorized"
        ),
        "audit_id": "",
        "pr_url": "",
        "followups": [],
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(incident_script), "record", "--json"],
            input=json.dumps(record),
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[transfer.raise_incident] timeout after 10s — incident not recorded",
            file=sys.stderr,
        )
        return None
    except OSError as exc:
        print(
            f"[transfer.raise_incident] OSError: {exc} — incident not recorded",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        print(
            f"[transfer.raise_incident] aqg_incident_index exit "
            f"{proc.returncode}: {proc.stderr.strip()[:200]} — incident not recorded",
            file=sys.stderr,
        )
        return None
    return f"{date}-{slug}"


# ===== Aggregation =====


def _is_partial_run(task_records: list[dict[str, Any]]) -> bool:
    """A2 audit 299b566d #3: detect a `--tasks` subset run.

    Tasks not in the user's selection are emitted as fail/warn placeholders
    via `_make_skipped_record`, with `notes` starting with the sentinel
    "skipped via --tasks subset". Returns True if any record carries that
    sentinel — meaning the threshold is being computed against an
    intentionally-incomplete set, and metric/incident emission would mislead
    the ledger / index.
    """
    sentinel = "skipped via --tasks subset"
    return any(sentinel in r.get("notes", "") for r in task_records)


def aggregate_summary(
    task_records: list[dict[str, Any]],
    *,
    repo: Path,
    run_id: str,
    artifact_uri: str,
    record_metric: bool = False,
    raise_incident_on_fail: bool = False,
    actor: str = "ci-bot",
) -> dict[str, Any]:
    by_id = {r["task_id"]: r for r in task_records}

    hard_gate_results = [by_id[i]["result"] for i in HARD_GATE_IDS if i in by_id]
    strict_pass_count = sum(1 for r in hard_gate_results if r == "pass")
    strict_pass_rate = (
        strict_pass_count / len(HARD_GATE_IDS) if HARD_GATE_IDS else 0.0
    )

    boundary_violations = sum(
        r.get("boundary_violations", 0) for r in task_records
    )

    task5 = by_id.get(5)
    task5_advisory = task5["result"] if task5 else "fail"

    threshold_met = (
        strict_pass_rate == 1.0
        and boundary_violations == 0
        and task5_advisory != "fail"
    )

    # Build a preview summary-shape so emit helpers can read threshold_met /
    # overall_pass_rate / task5_advisory without circular dep on the final dict.
    preview = {
        "threshold_met": threshold_met,
        "overall_pass_rate": float(strict_pass_rate),
        "task5_advisory": task5_advisory,
    }

    metric_event_id: Optional[str] = None
    incident_event_id: Optional[str] = None

    # A2 audit 299b566d #3: suppress metric/incident emission for partial
    # `--tasks` runs to avoid polluting ledger/index with placeholder-induced
    # fail signals. Surface the suppression to stderr so operator knows.
    partial = _is_partial_run(task_records)

    if partial and (record_metric or raise_incident_on_fail):
        print(
            "[transfer.aggregate_summary] suppressing metric/incident emit: "
            "partial --tasks subset run; placeholder fail signals not "
            "representative of a real threshold result",
            file=sys.stderr,
        )

    if record_metric and not partial:
        metric_event_id = emit_metric_record(repo, run_id, preview, actor=actor)

    if raise_incident_on_fail and not threshold_met and not partial:
        incident_event_id = raise_incident(repo, run_id, preview, actor=actor)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "date": _now_utc().date().isoformat(),
        "overall_pass_rate": float(strict_pass_rate),
        "tasks": [by_id[i] for i in sorted(by_id.keys())],
        "threshold_strict": 1.0,
        "threshold_met": threshold_met,
        "task5_advisory": task5_advisory,
        "provenance": _make_provenance(
            repo,
            artifact_uri=artifact_uri,
            metric_event_id=metric_event_id,
            incident_event_id=incident_event_id,
        ),
    }


# ===== Output =====


def _emit_yaml(summary: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError:
        return json.dumps(summary, indent=2, default=str)
    return yaml.safe_dump(summary, sort_keys=False, default_flow_style=False)


def _emit_json(summary: dict[str, Any]) -> str:
    return json.dumps(summary, indent=2, default=str)


# ===== CLI =====


def _parse_task_list(s: str) -> list[int]:
    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"--tasks: must be comma-separated ints in 1..6, got {part!r}: {exc}"
            ) from exc
        if n not in ALL_IDS:
            raise argparse.ArgumentTypeError(
                f"--tasks: {n} not in {sorted(ALL_IDS)}"
            )
        out.append(n)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="transfer_test_pack",
        description="Transfer Test Pack v1 runner — Tasks 1-6.",
    )
    parser.add_argument(
        "--tasks",
        type=_parse_task_list,
        default=None,
        help="comma-separated task ids (1..6); default = all 6",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write run summary to this file in addition to stdout",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of YAML",
    )
    parser.add_argument(
        "--skip-task5",
        action="store_true",
        help="skip Task 5 entirely (advisory; CI uses this when provider unavailable)",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="run identifier (default: <date>-<8-char-suffix>)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO_ROOT,
        help="repo root (default: detected from runner.py location)",
    )
    parser.add_argument(
        "--artifact-uri",
        default="local://",
        help="GHA artifact URL or local path (default: local://)",
    )
    parser.add_argument(
        "--record-metric",
        action="store_true",
        help=(
            "Emit a single aqg_metrics record summarizing this run "
            "(tool=transfer, result=pass|fail). Forces --record-metrics on "
            "the metrics call so AQG_METRICS env var opt-in is bypassed for "
            "this single record. Provenance.metric_event_id is then set to "
            "transfer-<run_id>. Default: off (no metric record emitted)."
        ),
    )
    parser.add_argument(
        "--raise-incident",
        action="store_true",
        help=(
            "On threshold_met=false, write an incident record to "
            "docs/incidents/<date>-transfer-pack-fail-<run-id>.md via "
            "aqg_incident_index. Provenance.incident_event_id is then set. "
            "Default: off (avoids local-dev noise; nightly/tag CI enables it)."
        ),
    )
    parser.add_argument(
        "--actor",
        default="ci-bot",
        choices=["claude", "codex", "human", "ci-bot", "other"],
        help=(
            "Actor identity for emitted metric/incident records "
            "(default: ci-bot, matching nightly/tag CI usage)."
        ),
    )
    args = parser.parse_args(argv)

    selected = args.tasks if args.tasks else sorted(ALL_IDS)
    run_id = args.run_id or _default_run_id()

    records: list[dict[str, Any]] = []
    for tid in sorted(ALL_IDS):
        if tid not in selected:
            records.append(
                _make_skipped_record(
                    tid,
                    note=(
                        "skipped via --tasks subset; hard-gate tasks treated "
                        "as fail to prevent silent threshold bypass per §5.1"
                    ),
                    repo=args.repo,
                    artifact_uri=args.artifact_uri,
                )
            )
            continue
        records.append(
            _execute_task(
                tid,
                args.repo,
                args.artifact_uri,
                task5_skip=args.skip_task5,
            )
        )

    summary = aggregate_summary(
        records,
        repo=args.repo,
        run_id=run_id,
        artifact_uri=args.artifact_uri,
        record_metric=args.record_metric,
        raise_incident_on_fail=args.raise_incident,
        actor=args.actor,
    )

    text = _emit_json(summary) if args.json else _emit_yaml(summary)
    print(text)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")

    return 0 if summary["threshold_met"] else 1


def _default_run_id() -> str:
    import secrets
    suffix = secrets.token_hex(4)
    return f"{_now_utc().date().isoformat()}-{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
