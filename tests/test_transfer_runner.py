"""Tests for tests/transfer/runner.py + 6 task modules.

Covers:
- runner CLI parses --tasks subset
- skipped task records emit fail (hard) / warn (advisory) per §5.1
- aggregate_summary computes threshold_met correctly per §5.4
- Task 4 (behavior_trigger) drift hash check on real triggers.yaml
- Task 5 advisory skip when no audit result file present
- Task 6 frontmatter equality check (using fixture in repo)
- Task 5 canonical_key extraction matches stem rule

Tasks 1, 2, 3 invoke external scripts (install.sh, validate_handoff,
aqg_chaos) which are slow / state-mutating; their integration is exercised
via the local end-to-end runner script, not in these unit tests.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
TRANSFER = REPO / "tests" / "transfer"
sys.path.insert(0, str(TRANSFER))

import runner  # noqa: E402
from tasks import (  # noqa: E402
    task4_behavior_trigger,
    task5_audit_reproducibility,
    task6_owner_cold_transfer,
)


# ===== runner CLI =====


def test_parse_task_list_accepts_valid_subset() -> None:
    assert runner._parse_task_list("1,2,3") == [1, 2, 3]
    assert runner._parse_task_list("6") == [6]


def test_parse_task_list_rejects_out_of_range() -> None:
    with pytest.raises(Exception):
        runner._parse_task_list("0,1,2")
    with pytest.raises(Exception):
        runner._parse_task_list("7")


def test_parse_task_list_rejects_non_int() -> None:
    with pytest.raises(Exception):
        runner._parse_task_list("a,b")


# ===== skipped record posture =====


def test_skipped_hard_gate_task_is_fail() -> None:
    record = runner._make_skipped_record(
        1, "smoke", repo=REPO, artifact_uri="local://"
    )
    assert record["result"] == "fail"
    assert record["task_id"] == 1
    assert record["task_name"] == "fresh_checkout"


def test_skipped_advisory_task_is_warn() -> None:
    record = runner._make_skipped_record(
        5, "advisory skip", repo=REPO, artifact_uri="local://"
    )
    assert record["result"] == "warn"
    assert record["task_id"] == 5


# ===== aggregate_summary threshold logic (§5.4) =====


def _stub_task_record(task_id: int, result: str, *, boundary_violations: int = 0) -> dict:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "task_name": runner.TASK_ID_TO_NAME[task_id],
        "started_at": "2026-05-05T01:00:00+00:00",
        "finished_at": "2026-05-05T01:00:00+00:00",
        "duration_seconds": 0,
        "exit_code": 0 if result == "pass" else 1,
        "result": result,
        "required_checks_passed": 1 if result == "pass" else 0,
        "required_checks_total": 1,
        "boundary_violations": boundary_violations,
        "notes": "stub",
        "provenance": runner._make_provenance(REPO, artifact_uri="local://"),
    }


def test_threshold_met_when_all_hard_pass_task5_warn() -> None:
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4, 6)]
    records.append(_stub_task_record(5, "warn"))
    summary = runner.aggregate_summary(
        records, repo=REPO, run_id="t1", artifact_uri="local://"
    )
    assert summary["threshold_met"] is True
    assert summary["overall_pass_rate"] == 1.0
    assert summary["task5_advisory"] == "warn"


def test_threshold_not_met_when_one_hard_fails() -> None:
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4)]
    records.append(_stub_task_record(5, "warn"))
    records.append(_stub_task_record(6, "fail"))
    summary = runner.aggregate_summary(
        records, repo=REPO, run_id="t2", artifact_uri="local://"
    )
    assert summary["threshold_met"] is False
    assert summary["overall_pass_rate"] == 4 / 5


def test_threshold_not_met_when_task5_fails_even_if_hard_pass() -> None:
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4, 6)]
    records.append(_stub_task_record(5, "fail"))
    summary = runner.aggregate_summary(
        records, repo=REPO, run_id="t3", artifact_uri="local://"
    )
    assert summary["threshold_met"] is False
    assert summary["task5_advisory"] == "fail"


def test_threshold_not_met_when_boundary_violation() -> None:
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4, 6)]
    records.append(_stub_task_record(5, "pass"))
    records[0]["boundary_violations"] = 1
    summary = runner.aggregate_summary(
        records, repo=REPO, run_id="t4", artifact_uri="local://"
    )
    assert summary["threshold_met"] is False


# ===== A2: aqg_metrics + aqg_incident_index integration =====


def _write_stub_script(repo: Path, name: str, exit_code: int = 0) -> None:
    """Drop a fake script under <repo>/scripts/ that exits with given code."""
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / name
    path.write_text(
        f"#!/usr/bin/env python3\nimport sys; sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_emit_metric_record_returns_id_on_success(tmp_path: Path) -> None:
    _write_stub_script(tmp_path, "aqg_metrics.py", exit_code=0)
    metric_id = runner.emit_metric_record(tmp_path, "run-aaa", {"threshold_met": True})
    assert metric_id == "transfer-run-aaa"


def test_emit_metric_record_returns_none_on_subprocess_failure(tmp_path: Path) -> None:
    _write_stub_script(tmp_path, "aqg_metrics.py", exit_code=1)
    metric_id = runner.emit_metric_record(tmp_path, "run-bbb", {"threshold_met": False})
    assert metric_id is None


def test_emit_metric_record_returns_none_when_script_missing(tmp_path: Path) -> None:
    metric_id = runner.emit_metric_record(tmp_path, "run-ccc", {"threshold_met": True})
    assert metric_id is None


def test_raise_incident_returns_id_on_success(tmp_path: Path) -> None:
    _write_stub_script(tmp_path, "aqg_incident_index.py", exit_code=0)
    summary_preview = {
        "threshold_met": False,
        "overall_pass_rate": 0.6,
        "task5_advisory": "warn",
    }
    incident_id = runner.raise_incident(tmp_path, "run-ddd", summary_preview)
    assert incident_id is not None
    assert "transfer-pack-fail-run-ddd" in incident_id
    assert incident_id.startswith(runner._now_utc().date().isoformat())


def test_raise_incident_returns_none_on_subprocess_failure(tmp_path: Path) -> None:
    _write_stub_script(tmp_path, "aqg_incident_index.py", exit_code=1)
    incident_id = runner.raise_incident(
        tmp_path,
        "run-eee",
        {"threshold_met": False, "overall_pass_rate": 0.6, "task5_advisory": "warn"},
    )
    assert incident_id is None


def test_raise_incident_slug_safe_chars_only(tmp_path: Path) -> None:
    _write_stub_script(tmp_path, "aqg_incident_index.py", exit_code=0)
    incident_id = runner.raise_incident(
        tmp_path,
        "Run With Spaces  & Symbols!",
        {"threshold_met": False, "overall_pass_rate": 0.6, "task5_advisory": "warn"},
    )
    assert incident_id is not None
    suffix = incident_id.split("transfer-pack-fail-", 1)[1]
    import re as _re
    assert _re.match(r"^[a-z0-9.\-]+$", suffix), f"unsafe slug chars in {suffix!r}"


def test_aggregate_summary_no_flags_emits_no_ids(monkeypatch) -> None:
    """Default behavior: helpers not called, both ids None."""
    called: list[str] = []
    monkeypatch.setattr(
        runner, "emit_metric_record", lambda *a, **k: (called.append("metric"), "should-not-be-used")[1]
    )
    monkeypatch.setattr(
        runner, "raise_incident", lambda *a, **k: (called.append("incident"), "should-not-be-used")[1]
    )
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4, 6)] + [_stub_task_record(5, "pass")]
    summary = runner.aggregate_summary(records, repo=REPO, run_id="x", artifact_uri="local://")
    assert summary["provenance"]["metric_event_id"] is None
    assert summary["provenance"]["incident_event_id"] is None
    assert called == [], "no helpers should be called when flags are off"


def test_aggregate_summary_record_metric_writes_id(monkeypatch) -> None:
    monkeypatch.setattr(runner, "emit_metric_record", lambda *a, **k: "transfer-stub-id")
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4, 6)] + [_stub_task_record(5, "warn")]
    summary = runner.aggregate_summary(
        records, repo=REPO, run_id="rid", artifact_uri="local://", record_metric=True
    )
    assert summary["provenance"]["metric_event_id"] == "transfer-stub-id"


def test_aggregate_summary_raise_incident_skipped_on_threshold_pass(monkeypatch) -> None:
    """Pass case: incident NOT raised even when flag set."""
    called: list[str] = []
    monkeypatch.setattr(
        runner, "raise_incident", lambda *a, **k: (called.append("incident"), "x-y-z")[1]
    )
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4, 6)] + [_stub_task_record(5, "warn")]
    summary = runner.aggregate_summary(
        records,
        repo=REPO,
        run_id="r-pass",
        artifact_uri="local://",
        raise_incident_on_fail=True,
    )
    assert summary["threshold_met"] is True
    assert summary["provenance"]["incident_event_id"] is None
    assert called == [], "helper not invoked when threshold_met=True"


def test_aggregate_summary_raise_incident_invokes_on_threshold_fail(monkeypatch) -> None:
    monkeypatch.setattr(runner, "raise_incident", lambda *a, **k: "2026-05-05-transfer-pack-fail-stub")
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4)] + [
        _stub_task_record(5, "warn"),
        _stub_task_record(6, "fail"),
    ]
    summary = runner.aggregate_summary(
        records,
        repo=REPO,
        run_id="r-fail",
        artifact_uri="local://",
        raise_incident_on_fail=True,
    )
    assert summary["threshold_met"] is False
    assert summary["provenance"]["incident_event_id"] == "2026-05-05-transfer-pack-fail-stub"


def test_aggregate_summary_both_flags_populate_both_ids(monkeypatch) -> None:
    monkeypatch.setattr(runner, "emit_metric_record", lambda *a, **k: "transfer-rid2")
    monkeypatch.setattr(runner, "raise_incident", lambda *a, **k: "2026-05-05-incident-stub")
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4)] + [
        _stub_task_record(5, "warn"),
        _stub_task_record(6, "fail"),
    ]
    summary = runner.aggregate_summary(
        records,
        repo=REPO,
        run_id="rid2",
        artifact_uri="local://",
        record_metric=True,
        raise_incident_on_fail=True,
    )
    assert summary["provenance"]["metric_event_id"] == "transfer-rid2"
    assert summary["provenance"]["incident_event_id"] == "2026-05-05-incident-stub"


def test_aggregate_summary_helper_failure_keeps_id_none(monkeypatch) -> None:
    """If helper returns None (subprocess fail / script missing), id stays None."""
    monkeypatch.setattr(runner, "emit_metric_record", lambda *a, **k: None)
    records = [_stub_task_record(i, "pass") for i in (1, 2, 3, 4, 6)] + [_stub_task_record(5, "warn")]
    summary = runner.aggregate_summary(
        records, repo=REPO, run_id="rid3", artifact_uri="local://", record_metric=True
    )
    assert summary["provenance"]["metric_event_id"] is None


# ===== Audit 299b566d fix verification =====


def test_emit_metric_record_writes_event_id_into_record(tmp_path: Path) -> None:
    """audit 299b566d #1: event_id must be in the record body so consumers can join."""
    # Stub script that captures stdin and writes it to a sidecar file.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    captured = tmp_path / "captured_record.json"
    (scripts / "aqg_metrics.py").write_text(
        f"#!/usr/bin/env python3\n"
        f"import sys, json\n"
        f"data = sys.stdin.read()\n"
        f"open({str(captured)!r}, 'w').write(data)\n"
        f"sys.exit(0)\n",
        encoding="utf-8",
    )
    (scripts / "aqg_metrics.py").chmod(0o755)

    metric_id = runner.emit_metric_record(
        tmp_path, "fancy-run", {"threshold_met": True}
    )
    assert metric_id == "transfer-fancy-run"
    body = json.loads(captured.read_text(encoding="utf-8"))
    assert body["event_id"] == "transfer-fancy-run", \
        "event_id must be persisted into the metric record body"


def test_emit_metric_record_handles_subprocess_timeout(monkeypatch, tmp_path, capsys) -> None:
    """audit 299b566d #2: TimeoutExpired must be caught + stderr warning."""
    _write_stub_script(tmp_path, "aqg_metrics.py", exit_code=0)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=10)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    metric_id = runner.emit_metric_record(tmp_path, "rid", {"threshold_met": True})
    assert metric_id is None
    err = capsys.readouterr().err
    assert "timeout" in err.lower()


def test_emit_metric_record_handles_oserror(monkeypatch, tmp_path, capsys) -> None:
    """audit 299b566d #2: OSError on subprocess spawn must not crash runner."""
    _write_stub_script(tmp_path, "aqg_metrics.py", exit_code=0)

    def fake_run(*args, **kwargs):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    metric_id = runner.emit_metric_record(tmp_path, "rid", {"threshold_met": False})
    assert metric_id is None
    err = capsys.readouterr().err
    assert "OSError" in err or "spawn failure" in err


def test_emit_metric_record_emits_stderr_on_nonzero_exit(tmp_path, capsys) -> None:
    """audit 299b566d #2: nonzero subprocess exit emits operator-visible stderr."""
    _write_stub_script(tmp_path, "aqg_metrics.py", exit_code=1)
    runner.emit_metric_record(tmp_path, "rid", {"threshold_met": True})
    err = capsys.readouterr().err
    assert "exit 1" in err


def test_aggregate_summary_suppresses_emit_on_partial_subset_run(
    monkeypatch, capsys
) -> None:
    """audit 299b566d #3: --tasks subset → no metric/incident emit."""
    called: list[str] = []
    monkeypatch.setattr(
        runner, "emit_metric_record",
        lambda *a, **k: (called.append("metric"), "transfer-x")[1],
    )
    monkeypatch.setattr(
        runner, "raise_incident",
        lambda *a, **k: (called.append("incident"), "x-y")[1],
    )

    # Build records where Tasks 4-6 carry the partial-subset sentinel
    records = []
    for i in (1, 2, 3):
        records.append(_stub_task_record(i, "pass"))
    for i in (4, 6):
        records.append({
            **_stub_task_record(i, "fail"),
            "notes": (
                "skipped via --tasks subset; hard-gate tasks treated as fail "
                "to prevent silent threshold bypass per §5.1"
            ),
        })
    records.append({
        **_stub_task_record(5, "warn"),
        "notes": "skipped via --tasks subset; hard-gate tasks treated as fail",
    })

    summary = runner.aggregate_summary(
        records, repo=REPO, run_id="rid", artifact_uri="local://",
        record_metric=True, raise_incident_on_fail=True,
    )
    assert summary["threshold_met"] is False
    assert summary["provenance"]["metric_event_id"] is None
    assert summary["provenance"]["incident_event_id"] is None
    assert called == [], "neither helper should fire on partial subset run"
    err = capsys.readouterr().err
    assert "suppressing" in err.lower()


def test_raise_incident_slug_includes_hash_suffix(tmp_path: Path) -> None:
    """audit 299b566d #4: distinct long run_ids sharing prefix must produce distinct slugs."""
    _write_stub_script(tmp_path, "aqg_incident_index.py", exit_code=0)
    long_prefix = "a" * 50  # exceeds 48-char prefix budget
    id_a = runner.raise_incident(
        tmp_path,
        long_prefix + "-tail-AAA",
        {"threshold_met": False, "overall_pass_rate": 0.5, "task5_advisory": "warn"},
    )
    id_b = runner.raise_incident(
        tmp_path,
        long_prefix + "-tail-BBB",
        {"threshold_met": False, "overall_pass_rate": 0.5, "task5_advisory": "warn"},
    )
    assert id_a is not None and id_b is not None
    assert id_a != id_b, "long shared-prefix run_ids must yield distinct incident ids"
    # Both must contain the 10-char hash suffix segment
    assert id_a.split("-")[-1] != id_b.split("-")[-1]


def test_emit_metric_record_real_aqg_metrics_accepts_event_id() -> None:
    """audit 299b566d #1 + integration: real aqg_metrics.py + _metrics_redaction
    must accept the event_id field. Pin against current repo to surface schema
    drift if event_id support regresses."""
    repo_root = Path(__file__).resolve().parent.parent
    metric_id = runner.emit_metric_record(
        repo_root, "audit-test", {"threshold_met": True}
    )
    assert metric_id == "transfer-audit-test"


# ===== Task 4 — drift hash check on real triggers.yaml =====


def test_task4_passes_against_current_triggers_yaml() -> None:
    outcome = task4_behavior_trigger.run(REPO)
    assert outcome.result == "pass", outcome.notes
    assert outcome.required_checks_passed == 5


# ===== Task 5 — advisory skip + canonical key =====


def test_task5_warn_when_no_result_file(tmp_path: Path) -> None:
    fake_repo = tmp_path
    transfer = fake_repo / "tests" / "transfer"
    fixtures = transfer / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "audit_repro_golden.yaml").write_text(
        "schema_version: 1\nexpected_min_findings: []\n", encoding="utf-8"
    )
    outcome = task5_audit_reproducibility.run(fake_repo)
    assert outcome.result == "warn"
    assert "advisory" in outcome.notes.lower()


def _write_task5_fixtures(transfer: Path, *, fixture_version: str = "v1.0.0") -> str:
    """Helper: create artifact + golden + return artifact sha256 for bound_to."""
    import hashlib
    fixtures = transfer / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    artifact_text = "stub artifact for tests\n"
    (fixtures / "audit_repro_artifact.md").write_text(artifact_text, encoding="utf-8")
    (fixtures / "audit_repro_golden.yaml").write_text(
        f"""
schema_version: 1
fixture_version: {fixture_version}
expected_min_findings:
  - canonical_key: missing-input-validation-on
    severity: [major, high]
""",
        encoding="utf-8",
    )
    return hashlib.sha256(artifact_text.encode()).hexdigest()


def test_task5_fail_when_result_missing_required_finding(tmp_path: Path) -> None:
    fake_repo = tmp_path
    transfer = fake_repo / "tests" / "transfer"
    sha = _write_task5_fixtures(transfer)
    (transfer / "_task5_run_result.json").write_text(
        json.dumps({
            "bound_to": {
                "artifact_sha256": sha,
                "fixture_version": "v1.0.0",
            },
            "issues": [
                {
                    "issue": "Some unrelated finding",
                    "severity": "minor",
                }
            ]
        }),
        encoding="utf-8",
    )
    outcome = task5_audit_reproducibility.run(fake_repo)
    assert outcome.result == "fail"
    assert "missing required canonical_keys" in outcome.notes


def test_task5_pass_when_result_covers_expected(tmp_path: Path) -> None:
    fake_repo = tmp_path
    transfer = fake_repo / "tests" / "transfer"
    sha = _write_task5_fixtures(transfer)
    (transfer / "_task5_run_result.json").write_text(
        json.dumps({
            "bound_to": {
                "artifact_sha256": sha,
                "fixture_version": "v1.0.0",
            },
            "issues": [
                {
                    "issue": "Missing input validation on /search endpoint",
                    "severity": "major",
                },
                {
                    "issue": "Hardcoded credentials present",
                    "severity": "high",
                },
            ]
        }),
        encoding="utf-8",
    )
    outcome = task5_audit_reproducibility.run(fake_repo)
    assert outcome.result == "pass", outcome.notes


def test_task5_fail_when_result_not_bound_to_current_artifact(tmp_path: Path) -> None:
    """Audit 297dccac gpt #2: stale result file rejection."""
    fake_repo = tmp_path
    transfer = fake_repo / "tests" / "transfer"
    _write_task5_fixtures(transfer)
    (transfer / "_task5_run_result.json").write_text(
        json.dumps({
            "bound_to": {
                "artifact_sha256": "0" * 64,
                "fixture_version": "v1.0.0",
            },
            "issues": [
                {
                    "issue": "Missing input validation on /search endpoint",
                    "severity": "major",
                },
            ]
        }),
        encoding="utf-8",
    )
    outcome = task5_audit_reproducibility.run(fake_repo)
    assert outcome.result == "fail"
    assert "not bound to current artifact" in outcome.notes


def test_task5_fail_when_fixture_version_mismatch(tmp_path: Path) -> None:
    """Audit 297dccac gpt #2: golden_version drift rejection."""
    fake_repo = tmp_path
    transfer = fake_repo / "tests" / "transfer"
    sha = _write_task5_fixtures(transfer, fixture_version="v1.0.0")
    (transfer / "_task5_run_result.json").write_text(
        json.dumps({
            "bound_to": {
                "artifact_sha256": sha,
                "fixture_version": "v0.9.0",
            },
            "issues": [
                {
                    "issue": "Missing input validation on /search endpoint",
                    "severity": "major",
                },
            ]
        }),
        encoding="utf-8",
    )
    outcome = task5_audit_reproducibility.run(fake_repo)
    assert outcome.result == "fail"
    assert "fixture_version" in outcome.notes


def test_task5_canonical_key_collision_severity_set(tmp_path: Path) -> None:
    """Audit 297dccac gpt #1 + gemini #2: collision must collect all severities."""
    fake_repo = tmp_path
    transfer = fake_repo / "tests" / "transfer"
    sha = _write_task5_fixtures(transfer)
    (transfer / "_task5_run_result.json").write_text(
        json.dumps({
            "bound_to": {
                "artifact_sha256": sha,
                "fixture_version": "v1.0.0",
            },
            "issues": [
                # Two findings collide on canonical_key "missing-input-validation-on";
                # first has minor (not allowed), second has major (allowed).
                # Pre-fix: dict overwrite would keep minor → false fail.
                # Post-fix: severity set {minor, major} ∩ {major, high} = {major} → pass.
                {
                    "issue": "Missing input validation on POST body",
                    "severity": "minor",
                },
                {
                    "issue": "Missing input validation on /search endpoint",
                    "severity": "major",
                },
            ]
        }),
        encoding="utf-8",
    )
    outcome = task5_audit_reproducibility.run(fake_repo)
    assert outcome.result == "pass", outcome.notes


def test_canonical_key_stem_rule() -> None:
    assert (
        task5_audit_reproducibility.canonical_key("Missing input validation on /search endpoint")
        == "missing-input-validation-on"
    )
    assert (
        task5_audit_reproducibility.canonical_key("No rate limit on /login")
        == "no-rate-limit-on"
    )
    assert task5_audit_reproducibility.canonical_key("") == ""
    assert task5_audit_reproducibility.canonical_key("hi there") == "hi-there"


# ===== Task 6 — fixture equality =====


def test_task6_passes_against_repo_fixture() -> None:
    outcome = task6_owner_cold_transfer.run(REPO)
    assert outcome.result == "pass", outcome.notes
    assert outcome.required_checks_passed == 2


def test_task6_fails_when_input_missing_frontmatter(tmp_path: Path) -> None:
    fake_repo = tmp_path
    transfer = fake_repo / "tests" / "transfer"
    fixtures = transfer / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "owner_transfer_input.md").write_text(
        "# no frontmatter here\n", encoding="utf-8"
    )
    (fixtures / "owner_transfer_golden.yaml").write_text(
        "next_action: foo\nblockers: []\n", encoding="utf-8"
    )
    outcome = task6_owner_cold_transfer.run(fake_repo)
    assert outcome.result == "fail"
    assert "frontmatter" in outcome.notes.lower()


def test_task6_fails_on_blocker_set_mismatch(tmp_path: Path) -> None:
    fake_repo = tmp_path
    transfer = fake_repo / "tests" / "transfer"
    fixtures = transfer / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "owner_transfer_input.md").write_text(
        "---\nnext_action: foo\nblockers:\n  - alpha\n---\n",
        encoding="utf-8",
    )
    (fixtures / "owner_transfer_golden.yaml").write_text(
        "next_action: foo\nblockers:\n  - alpha\n  - beta\n",
        encoding="utf-8",
    )
    outcome = task6_owner_cold_transfer.run(fake_repo)
    assert outcome.result == "fail"
    assert "blockers" in outcome.notes.lower()
