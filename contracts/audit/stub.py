"""de_audit interface STUB — the integration seam, not the engine.

AQG's skills route external cross-vendor audits through `de_audit`. This module
ships the callable's SIGNATURE + the request/response contract, but deliberately
does NOT implement the engine: vendor selection / per-mode routing, panel
convergence weighting, and ground_truth are engine-internal (the external cloud
decision-engine) and are not part of this MIT-public package.

An adopter wires a real engine behind this seam (see contracts/audit/README.md),
or uses `aqg-multi-review new --fallback-session-llm` for a single-model degraded
eval when no engine is available. Calling the stub validates the request against
the contract and then raises EngineNotAvailable — it never fabricates a verdict.
"""
from __future__ import annotations

from typing import Any, NoReturn

from audit.conformance import (
    SCHEMA_VERSION,
    _EVENT_TYPES,
    _MODES,
    _REQUEST_KEYS,
    _RESPONSE_KEYS,
    _STAKES,
    _STATUSES,
    validate_audit_event,
)


class EngineNotAvailable(RuntimeError):
    """Raised when de_audit is called but no external engine is wired behind the
    seam. Distinct from ValueError (which signals a malformed request)."""


_ENGINE_MISSING_MSG = (
    "de_audit engine not included in this package. The cross-vendor routing + "
    "convergence engine ships separately (external decision-engine). Wire your "
    "own engine behind this seam (see contracts/audit/README.md), or run "
    "`aqg-multi-review new --fallback-session-llm` for a single-model degraded "
    "eval (labeled non-independent)."
)


def de_audit(
    artifact: str,
    *,
    context: str = "",
    focus: "str | None" = None,
    mode: str = "standard",
    stakes: str = "medium",
    caller_family: "str | None" = None,
) -> NoReturn:
    """Contract-shaped entry point. Validates the request, then raises
    EngineNotAvailable (this package ships no engine).

    Never returns a successful audit_response — this package has no engine.
    Unknown keyword arguments raise TypeError (no `**kwargs` catch-all), so a
    misspelled parameter fails loudly instead of being silently dropped.
    `caller_family` defaults to None (a neutral interface presupposes no vendor).

    Raises:
        ValueError: the request violates the audit_request contract.
        EngineNotAvailable: the request is well-formed but no engine is wired.
    """
    payload: dict[str, Any] = {"artifact": artifact, "mode": mode, "stakes": stakes}
    if context:
        payload["context"] = context
    if focus:
        payload["focus"] = focus
    if caller_family:
        payload["caller_family"] = caller_family

    ok, errors = validate_audit_event(
        {"schema_version": SCHEMA_VERSION, "event_type": "audit_request", "payload": payload}
    )
    if not ok:
        raise ValueError("invalid de_audit request: " + "; ".join(errors))

    raise EngineNotAvailable(_ENGINE_MISSING_MSG)


def describe_contract() -> "dict[str, Any]":
    """Return the moat-safe interface metadata: what a caller may send/expect.

    Advertises the INTERFACE only — never engine internals (no vendor list, no
    routing map, no convergence weighting, no ground_truth)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "event_types": sorted(_EVENT_TYPES),
        "modes": sorted(_MODES),
        "stakes": sorted(_STAKES),
        "statuses": sorted(_STATUSES),
        "request_fields": sorted(_REQUEST_KEYS),
        "response_fields": sorted(_RESPONSE_KEYS),
        "engine_included": False,
        "schema_file": "contracts/audit/audit-event.schema.json",
    }
