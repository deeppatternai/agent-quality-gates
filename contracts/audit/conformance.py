"""AuditEvent conformance validator — pure stdlib (no jsonschema dependency).

Mirrors audit-event.schema.json. Validates the de_audit INTERFACE contract only
(request + response event shape); it knows nothing about the engine's vendor
routing / convergence / ground_truth (those are external and deliberately not
modeled here). Accumulates ALL errors and NEVER raises on a malformed event —
every problem is a string in the returned list (mirrors contracts/ledger).

Why hand-rolled, not jsonschema: AQG keeps contract code zero-third-party so
adopters can embed it anywhere. The JSON Schema stays the machine-readable
contract document; this is the runnable check.
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1.0"
_SUPPORTED_VERSIONS = {"1.0"}
_EVENT_TYPES = {"audit_request", "audit_response"}
_MODES = {"fast", "standard", "deep"}
_STAKES = {"low", "medium", "high"}
_STATUSES = {"running", "completed", "failed", "cancelled"}
_VERDICTS = {"clean", "has-issues", "has-serious-issues", "blocking"}
_SEVERITIES = {"nit", "minor", "major", "critical"}

_TOP_KEYS = {"schema_version", "event_type", "payload"}
_REQUEST_KEYS = {"artifact", "context", "focus", "mode", "stakes", "caller_family"}
_RESPONSE_KEYS = {"audit_id", "status", "mode_effective", "panel", "verdicts", "note"}
_FINDING_KEYS = {"severity", "dimension", "claim", "location", "suggested_fix"}
_VERDICT_KEYS = {"voice_label", "overall_verdict", "findings"}


def validate_audit_event(event: Any) -> "tuple[bool, list[str]]":
    """Return (ok, errors). Never raises — a malformed event yields ok=False + a
    list of human-readable error strings (all problems, not just the first)."""
    errors: list[str] = []

    if not isinstance(event, dict):
        return False, [f"event must be an object, got {type(event).__name__}"]

    extra = set(event) - _TOP_KEYS
    if extra:
        errors.append(f"unknown top-level field(s): {sorted(extra)}")

    version = event.get("schema_version")
    if version not in _SUPPORTED_VERSIONS:
        errors.append(
            f"schema_version must be one of {sorted(_SUPPORTED_VERSIONS)}, got {version!r}"
        )

    event_type = event.get("event_type")
    if event_type not in _EVENT_TYPES:
        errors.append(
            f"event_type must be one of {sorted(_EVENT_TYPES)}, got {event_type!r}"
        )

    payload = event.get("payload")
    if not isinstance(payload, dict):
        errors.append("payload must be an object")
        return len(errors) == 0, errors

    if event_type == "audit_request":
        _validate_request(payload, errors)
    elif event_type == "audit_response":
        _validate_response(payload, errors)
    # unknown event_type already reported; skip payload branch

    return len(errors) == 0, errors


def _validate_request(payload: dict, errors: list[str]) -> None:
    extra = set(payload) - _REQUEST_KEYS
    if extra:
        errors.append(f"audit_request payload has unknown field(s): {sorted(extra)}")

    artifact = payload.get("artifact")
    if not isinstance(artifact, str) or not artifact:
        errors.append("audit_request.artifact is required and must be a non-empty string")

    _opt_enum(payload, "mode", _MODES, errors, "audit_request")
    _opt_enum(payload, "stakes", _STAKES, errors, "audit_request")
    _opt_str(payload, "context", errors, "audit_request")
    _opt_str(payload, "focus", errors, "audit_request")
    _opt_str(payload, "caller_family", errors, "audit_request")


def _validate_response(payload: dict, errors: list[str]) -> None:
    extra = set(payload) - _RESPONSE_KEYS
    if extra:
        errors.append(f"audit_response payload has unknown field(s): {sorted(extra)}")

    audit_id = payload.get("audit_id")
    if not isinstance(audit_id, str) or not audit_id:
        errors.append("audit_response.audit_id is required and must be a non-empty string")

    status = payload.get("status")
    if status not in _STATUSES:
        errors.append(
            f"audit_response.status must be one of {sorted(_STATUSES)}, got {status!r}"
        )

    _opt_enum(payload, "mode_effective", _MODES, errors, "audit_response")
    _opt_str(payload, "note", errors, "audit_response")

    panel = payload.get("panel")
    if panel is not None:
        if not isinstance(panel, list) or any(not isinstance(v, str) for v in panel):
            errors.append("audit_response.panel must be a list of strings")

    verdicts = payload.get("verdicts")
    if verdicts is not None:
        if not isinstance(verdicts, list):
            errors.append("audit_response.verdicts must be a list")
        else:
            for i, v in enumerate(verdicts):
                _validate_verdict(i, v, errors)


def _validate_verdict(i: int, verdict: Any, errors: list[str]) -> None:
    if not isinstance(verdict, dict):
        errors.append(f"verdicts[{i}] must be an object")
        return
    extra = set(verdict) - _VERDICT_KEYS
    if extra:
        errors.append(f"verdicts[{i}] has unknown field(s): {sorted(extra)}")
    ov = verdict.get("overall_verdict")
    if ov not in _VERDICTS:
        errors.append(
            f"verdicts[{i}].overall_verdict must be one of {sorted(_VERDICTS)}, got {ov!r}"
        )
    _opt_str(verdict, "voice_label", errors, f"verdicts[{i}]")
    findings = verdict.get("findings")
    if findings is not None:
        if not isinstance(findings, list):
            errors.append(f"verdicts[{i}].findings must be a list")
        else:
            for j, f in enumerate(findings):
                _validate_finding(i, j, f, errors)


def _validate_finding(i: int, j: int, finding: Any, errors: list[str]) -> None:
    where = f"verdicts[{i}].findings[{j}]"
    if not isinstance(finding, dict):
        errors.append(f"{where} must be an object")
        return
    extra = set(finding) - _FINDING_KEYS
    if extra:
        errors.append(f"{where} has unknown field(s): {sorted(extra)}")
    sev = finding.get("severity")
    if sev not in _SEVERITIES:
        errors.append(f"{where}.severity must be one of {sorted(_SEVERITIES)}, got {sev!r}")
    claim = finding.get("claim")
    if not isinstance(claim, str) or not claim:
        errors.append(f"{where}.claim is required and must be a non-empty string")
    for k in ("dimension", "location", "suggested_fix"):
        _opt_str(finding, k, errors, where)


def _opt_enum(obj: dict, key: str, allowed: set, errors: list[str], where: str) -> None:
    if key in obj and obj[key] not in allowed:
        errors.append(f"{where}.{key} must be one of {sorted(allowed)}, got {obj[key]!r}")


def _opt_str(obj: dict, key: str, errors: list[str], where: str) -> None:
    if key in obj and not isinstance(obj[key], str):
        errors.append(f"{where}.{key} must be a string")
