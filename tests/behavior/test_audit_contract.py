"""Behavior lock for the de_audit interface contract (contracts/audit/).

WS-4 item 3: AQG ships the de_audit INTERFACE (request/response event shape) +
a non-implementing stub, NOT the engine. The engine's vendor routing, panel
convergence weighting, and ground_truth stay external (cloud decision-engine).
These tests lock (a) the conformance validator against the schema, (b) the stub
fails cleanly with a pointer to the external engine, and (c) a moat guard that no
concrete auditor vendor/model identifiers leak into the shipped contract.

Runs under the existing `pytest tests/behavior/` CI step; injects contracts/ on
sys.path (mirrors the ledger suite's conftest).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_CONTRACTS = _REPO / "contracts"
if str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

from audit.conformance import validate_audit_event  # noqa: E402
from audit.stub import de_audit, describe_contract, EngineNotAvailable  # noqa: E402

_AUDIT_DIR = _CONTRACTS / "audit"


def _req(**payload):
    return {"schema_version": "1.0", "event_type": "audit_request", "payload": payload}


def _resp(**payload):
    return {"schema_version": "1.0", "event_type": "audit_response", "payload": payload}


# ---- conformance: happy path ----------------------------------------------


def test_valid_request_passes():
    ok, errors = validate_audit_event(_req(artifact="def f(): return 1", mode="standard"))
    assert ok is True, errors


def test_valid_response_passes():
    ev = _resp(
        audit_id="14c08bb3",
        status="completed",
        mode_effective="standard",
        panel=["Voice 1", "Voice 2"],
        verdicts=[{"overall_verdict": "clean", "findings": []}],
    )
    ok, errors = validate_audit_event(ev)
    assert ok is True, errors


# ---- conformance: structural rejections -----------------------------------


def test_request_missing_artifact_rejected():
    ok, errors = validate_audit_event(_req(mode="standard"))
    assert ok is False
    assert any("artifact" in e for e in errors), errors


def test_unknown_event_type_rejected():
    ok, errors = validate_audit_event(
        {"schema_version": "1.0", "event_type": "audit_bogus", "payload": {}}
    )
    assert ok is False
    assert any("event_type" in e for e in errors), errors


def test_request_unknown_field_rejected():
    """additionalProperties:false — a stray field must not pass silently."""
    ok, errors = validate_audit_event(_req(artifact="x", vendor="gpt-5.5"))
    assert ok is False
    assert any("vendor" in e for e in errors), errors


def test_request_bad_mode_rejected():
    ok, errors = validate_audit_event(_req(artifact="x", mode="ultra"))
    assert ok is False
    assert any("mode" in e for e in errors), errors


def test_response_missing_audit_id_rejected():
    ok, errors = validate_audit_event(_resp(status="completed"))
    assert ok is False
    assert any("audit_id" in e for e in errors), errors


def test_response_bad_verdict_enum_rejected():
    ev = _resp(audit_id="x", status="completed",
               verdicts=[{"overall_verdict": "looks-fine"}])
    ok, errors = validate_audit_event(ev)
    assert ok is False
    assert any("overall_verdict" in e for e in errors), errors


def test_bad_schema_version_rejected():
    ok, errors = validate_audit_event(
        {"schema_version": "9.9", "event_type": "audit_request", "payload": {"artifact": "x"}}
    )
    assert ok is False
    assert any("schema_version" in e for e in errors), errors


# ---- stub: interface seam fails cleanly -----------------------------------


def test_stub_raises_engine_not_available():
    with pytest.raises(EngineNotAvailable) as exc:
        de_audit(artifact="def f(): return 1", mode="standard")
    msg = str(exc.value).lower()
    # points the caller at the external engine + the in-repo degraded fallback
    assert "engine" in msg
    assert "fallback-session-llm" in msg


def test_stub_validates_request_before_raising():
    """A malformed request is a ValueError (contract violation), distinct from the
    EngineNotAvailable that a well-formed request hits."""
    with pytest.raises(ValueError):
        de_audit(artifact="", mode="standard")


def test_describe_contract_is_moat_safe():
    meta = describe_contract()
    assert meta["schema_version"] == "1.0"
    assert set(meta["event_types"]) == {"audit_request", "audit_response"}
    assert set(meta["modes"]) == {"fast", "standard", "deep"}
    # the metadata advertises the INTERFACE, never the engine internals
    blob = json.dumps(meta).lower()
    for moat in ("ground_truth", "convergence", "routing", "weight"):
        assert moat not in blob, f"describe_contract leaked engine internal: {moat}"


# ---- moat guard: no vendor/model identifiers in the shipped contract ------


def test_schema_and_stub_do_not_leak_vendor_routing():
    """The shipped contract must expose interface SHAPE only. Concrete auditor
    vendor/model identifiers would mean routing detail leaked into MIT-public code
    (guarded downstream by WS-0 G0-④; this is the local regression net)."""
    # Bare vendor/family tokens too — not only model-specific ids. audit 9b4cf9cd
    # (3/4) caught a `caller_family="claude"` default + schema examples that the
    # model-id-only denylist missed; scan EVERY shipped file (code + schema +
    # README + fixtures), since all of contracts/audit/ is carve-public.
    denylist = [
        "gpt-5", "gpt-4", "gemini", "grok", "deepseek", "qwen", "o3-",
        "claude", "openai", "anthropic", "minimax", "xai",
    ]
    shipped = [p for p in _AUDIT_DIR.rglob("*")
               if p.is_file() and not p.name.endswith(".pyc")]
    assert shipped, "no shipped contract files found"
    for path in shipped:
        text = path.read_text(encoding="utf-8").lower()
        rel = path.relative_to(_AUDIT_DIR)
        for term in denylist:
            assert term not in text, f"{rel} leaks vendor/model identifier: {term!r}"


# ---- fixtures: every golden sample matches its verdict --------------------


def test_valid_fixtures_all_pass():
    vdir = _AUDIT_DIR / "fixtures" / "valid"
    files = sorted(vdir.glob("*.json"))
    assert files, "no valid fixtures found"
    for f in files:
        ev = json.loads(f.read_text(encoding="utf-8"))
        ok, errors = validate_audit_event(ev)
        assert ok is True, f"{f.name} should be valid: {errors}"


def test_invalid_fixtures_all_fail():
    idir = _AUDIT_DIR / "fixtures" / "invalid"
    files = sorted(idir.glob("*.json"))
    assert files, "no invalid fixtures found"
    for f in files:
        ev = json.loads(f.read_text(encoding="utf-8"))
        ok, _ = validate_audit_event(ev)
        assert ok is False, f"{f.name} should be rejected"


# ---- WS-4 audit 9b4cf9cd: hardening (4/4 + moat leak my own guard missed) ----


def test_stub_no_vendor_family_default():
    """Moat: the shipped stub must not bake a concrete vendor family into the
    signature default (audit 9b4cf9cd 3/4 — my guard's denylist missed bare
    'claude'). Default must be a neutral sentinel, not a vendor name."""
    import inspect
    from audit import stub as stub_mod
    sig = inspect.signature(stub_mod.de_audit)
    default = sig.parameters["caller_family"].default
    assert default in (None, "", "unknown"), f"vendor family baked into default: {default!r}"


def test_stub_rejects_unknown_kwarg():
    """Fail closed: an unknown/misspelled kwarg must not be silently swallowed
    (audit 9b4cf9cd 4/4 — `**_ignored` dropped typos and downgraded silently)."""
    with pytest.raises(TypeError):
        de_audit(artifact="x", mde="deep")  # typo for mode=


def test_request_nonstring_optionals_rejected():
    """Lock the existing _opt_str enforcement (audit flagged it as a gap because
    the condensed audit artifact hid the checks; the code already enforces it —
    this pins it against regression)."""
    for bad in ({"artifact": "x", "context": 123},
                {"artifact": "x", "focus": []},
                {"artifact": "x", "caller_family": {}}):
        ok, _ = validate_audit_event(_req(**bad))
        assert ok is False, f"{bad} should be rejected"


def test_response_nonstring_note_rejected():
    ok, errors = validate_audit_event(_resp(audit_id="x", status="completed", note=5))
    assert ok is False
    assert any("note" in e for e in errors), errors


def test_describe_contract_is_symmetric():
    """describe_contract advertises both request and response surfaces."""
    meta = describe_contract()
    assert set(meta["response_fields"]) == {
        "audit_id", "status", "mode_effective", "panel", "verdicts", "note"
    }
    assert set(meta["statuses"]) == {"running", "completed", "failed", "cancelled"}


def test_schema_constants_parity():
    """Drift guard: the hand-maintained conformance enums/keys must match the
    shipped JSON schema (audit 9b4cf9cd — dual source of truth risk)."""
    from audit import conformance as cf
    schema = json.loads((_AUDIT_DIR / "audit-event.schema.json").read_text(encoding="utf-8"))
    # schema_version enum
    sv = schema["properties"]["schema_version"]["enum"]
    assert set(sv) == cf._SUPPORTED_VERSIONS
    # event_type enum
    et = schema["properties"]["event_type"]["enum"]
    assert set(et) == cf._EVENT_TYPES
    # request + response payload keys come from the allOf branches
    for branch in schema["allOf"]:
        then_props = branch["then"]["properties"]["payload"]["properties"]
        et_const = branch["if"]["properties"]["event_type"]["const"]
        if et_const == "audit_request":
            assert set(then_props) == cf._REQUEST_KEYS
        elif et_const == "audit_response":
            assert set(then_props) == cf._RESPONSE_KEYS
