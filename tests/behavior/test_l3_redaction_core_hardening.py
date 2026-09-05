"""L3 pre-launch hardening regression suite for _redaction_common.scan_leaks.

Pins find+verify findings RB-01/RB-02 (lone surrogate) and RC-02/RB-03/RB-04
(Unicode line separators NEL/LS/PS), as adjudicated against audit 65c96e08:

- Lone surrogates (U+D800-DFFF) are rejected in BOTH modes (they pass every regex
  + the <32 check but crash the first downstream UTF-8 emit).
- NEL/LS/PS are line breaks: rejected in INLINE (single-line) mode; ALLOWED in
  MULTILINE mode (like \n) — heading injection via any separator is still caught
  by the existing heading scan (str.splitlines() honours NEL/LS/PS). Rejecting
  them in multiline too was over-rejection with no security gain (gemini f1).

Each bug-repro FAILS against pre-fix scan_leaks (stash-proven):
`git stash push -- scripts/_redaction_common.py` -> RED.

scan_leaks never raises — it appends to a `violations` list; the domain guards
return a result with `.is_safe`. Separators are written with chr() (no invisible
characters in source).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _redaction_common as rc  # noqa: E402
import _orchestration_redaction as orch  # noqa: E402
import _simulation_redaction as sim  # noqa: E402

SURROGATE_HI = chr(0xD800)
SURROGATE_LO = chr(0xDFFF)
SEPARATORS = (chr(0x85), chr(0x2028), chr(0x2029))  # NEL, LINE SEP, PARAGRAPH SEP


def _leaks(value: str, *, multiline: bool = False) -> list[str]:
    out: list[str] = []
    rc.scan_leaks("f", value, out, multiline=multiline)
    return out


# ----- unit: lone surrogates rejected in BOTH modes (RB-01/RB-02) -----

@pytest.mark.parametrize("ml", [False, True])
def test_lone_surrogate_rejected_both_modes(ml: bool) -> None:
    assert any("surrogate" in v for v in _leaks(f"intent {SURROGATE_HI} x", multiline=ml))
    assert any("surrogate" in v for v in _leaks(f"body {SURROGATE_LO}", multiline=ml))


def test_surrogate_value_is_actually_unencodable() -> None:
    # ground the WHY: the surrogate that passed the old guard crashes the first emit.
    with pytest.raises(UnicodeEncodeError):
        (f"x{SURROGATE_HI}").encode("utf-8")


# ----- unit: NEL/LS/PS line separators — inline rejected, multiline = line break -----

@pytest.mark.parametrize("sep", SEPARATORS)
def test_separator_rejected_inline(sep: str) -> None:
    # inline guard previously only saw \n/\r/<32; these are >= 0x20 and slipped past.
    assert any("single-line" in v for v in _leaks(f"ok{sep}tail"))


@pytest.mark.parametrize("sep", SEPARATORS)
def test_separator_heading_still_caught_multiline(sep: str) -> None:
    # heading injection via ANY separator is caught by the heading scan
    # (str.splitlines() honours NEL/LS/PS), so multiline heading injection is blocked.
    assert any("heading" in v for v in _leaks(f"text{sep}# Fake Heading", multiline=True))


@pytest.mark.parametrize("sep", SEPARATORS)
def test_separator_non_heading_allowed_multiline(sep: str) -> None:
    # f1 (audit 65c96e08): in multiline mode a separator is a legitimate line break
    # like \n; a NON-heading second line must NOT be over-rejected.
    assert _leaks(f"all clear{sep}second normal line", multiline=True) == []


def test_benign_newline_multiline_still_passes() -> None:
    assert _leaks("line one\nline two ok", multiline=True) == []


# ----- integration: orchestration manifest -----

def _valid_orch() -> dict:
    return {
        "schema_version": 1,
        "name": "review-implement-audit",
        "description": "claude reviews PR\ncodex implements",
        "steps": [
            {"id": "review", "actor": "claude", "intent": "review PR design", "inputs": [],
             "timeout_seconds": 600, "retry_max": 0, "on_fail": "abort"},
        ],
        "boundaries": "AQG validates schema only",
        "marker": "orchestration-helper-manifest",
    }


def test_orch_valid_still_safe() -> None:
    assert orch.check_orchestration_manifest(_valid_orch()).is_safe


def test_orch_surrogate_in_intent_rejected() -> None:
    rec = _valid_orch()
    rec["steps"][0]["intent"] = f"review{SURROGATE_HI}design"  # intent is inline
    assert not orch.check_orchestration_manifest(rec).is_safe


def test_orch_surrogate_in_description_rejected() -> None:
    rec = _valid_orch()
    rec["description"] = f"line one\nline two{SURROGATE_LO}"  # multiline; surrogate both modes
    assert not orch.check_orchestration_manifest(rec).is_safe


def test_orch_separator_in_intent_rejected() -> None:
    rec = _valid_orch()
    rec["steps"][0]["intent"] = f"review{chr(0x2028)}injected"  # inline -> rejected
    assert not orch.check_orchestration_manifest(rec).is_safe


def test_orch_separator_heading_in_description_rejected() -> None:
    rec = _valid_orch()
    rec["description"] = f"all clear{chr(0x2028)}# injected heading"  # heading via sep -> caught
    assert not orch.check_orchestration_manifest(rec).is_safe


# ----- integration: simulation manifest -----

def _valid_sim() -> dict:
    return {
        "schema_version": 1,
        "name": "api-failure-30pct",
        "description": "simulate 30 percent failure rate\nmulti-line OK",
        "kind": "api",
        "duration_seconds": 300,
        "seed": 42,
        "parameters": {"failure_rate_percent": 30, "targets": ["auth_api"]},
        "expected_artifacts": ["docs/sim-runs/api-failure.log"],
        "boundaries": "no production touched\nsynthetic only",
        "actor": "claude",
        "marker": "simulation-mock-manifest",
    }


def test_sim_valid_still_safe() -> None:
    assert sim.check_simulation_manifest(_valid_sim()).is_safe


def test_sim_surrogate_in_description_rejected() -> None:
    rec = _valid_sim()
    rec["description"] = f"simulate failure{SURROGATE_HI}"
    assert not sim.check_simulation_manifest(rec).is_safe


def test_sim_surrogate_in_boundaries_rejected() -> None:
    rec = _valid_sim()
    rec["boundaries"] = f"no prod{SURROGATE_LO}"
    assert not sim.check_simulation_manifest(rec).is_safe


def test_sim_separator_in_parameter_value_rejected() -> None:
    # parameter string values go through scan_leaks(multiline=False) (inline) -> RB-04.
    rec = _valid_sim()
    rec["parameters"] = {"note": f"benign{chr(0x2029)}tail", "targets": ["auth_api"]}
    assert not sim.check_simulation_manifest(rec).is_safe
