"""Tests for aqg_closeout.py Behavior Contract render (AQG 0.14.0).

Design: docs/discussion/2026-07-09-behavior-contract-openspec-absorb-a3.md §3.7.
Round-1 f4/f5 durability + secret-redaction; round-2 f3 malformed-safety.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "aqg-evidence-closeout" / "scripts"))
sys.path.insert(0, str(REPO / "skills" / "aqg-code-construction" / "scripts"))

import aqg_closeout as closeout  # noqa: E402


CONTRACT = """## Behavior Contract

### R1: OTP challenge
The system MUST present an OTP challenge for 2FA users.

- Scenario S1.1: valid creds
  - GIVEN a 2FA user
  - WHEN valid credentials are submitted
  - THEN an OTP challenge is presented
"""


def _ledger_file(tmp_path: Path, contract: str = CONTRACT, header_extra: str = "") -> Path:
    extra = f"\n{header_extra}" if header_extra else ""
    content = f"""---
task_slug: bc
path: full
created_at: 2020-01-01T00:00:00Z
session_agent: agent
skipped_checks: []
objections_diff_coverage_exception: null{extra}
---

| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | read | x.py:1 | ok |
| 2. Behavior Lock | yes | covers: R1 | x.py:1 | PASS |

{contract}

## Warning Acknowledgements

## Predicted Objections

1. **Objection**: x (x.py:1) — **Mitigation**: pytest -k t
"""
    p = tmp_path / "ledger.md"
    p.write_text(content)
    return p


def _render(ledger_path: Path) -> str:
    result = closeout.import_construction_ledger(ledger_path)
    assert result["status"] == closeout.LedgerImportStatus.OK, result
    buf = io.StringIO()
    with redirect_stdout(buf):
        closeout.print_construction_section(result["data"])
    return buf.getvalue()


@pytest.mark.skipif(
    closeout._CONSTRUCTION is None, reason="construction parser unavailable"
)
class TestRenderEndToEnd:
    def test_full_contract_rendered(self, tmp_path):
        out = _render(_ledger_file(tmp_path))
        assert "### Behavior Contract" in out
        assert "R1" in out
        # full normative statement text is present (durable referenceability)
        assert "MUST present an OTP challenge" in out
        # scenario id + markers rendered
        assert "S1.1" in out
        assert "GIVEN" in out and "THEN" in out

    def test_absent_contract_silent_skip(self, tmp_path):
        out = _render(_ledger_file(tmp_path, contract="(no contract here)"))
        assert "### Behavior Contract" not in out

    def test_waived_contract_note(self, tmp_path):
        out = _render(
            _ledger_file(
                tmp_path,
                contract="(none)",
                header_extra="behavior_contract_exception: emergency hotfix per PR 42",
            )
        )
        assert "### Behavior Contract" in out
        assert "waived" in out

    def test_null_exception_renders_contract_not_waiver(self, tmp_path):
        # Regression (dogfood bug): a raw `behavior_contract_exception: null` must
        # NOT be treated as an active waiver — the contract must render normally.
        out = _render(
            _ledger_file(tmp_path, header_extra="behavior_contract_exception: null")
        )
        assert "waived" not in out
        assert "MUST present an OTP challenge" in out

    def test_secret_in_contract_is_redacted(self, tmp_path):
        # Build a token matching the AWS-key pattern at runtime so no literal
        # secret appears in this source file (the repo's own secret-scan gate).
        fake_key = "AKIA" + "B" * 16
        leaky = f"""## Behavior Contract

### R1: leaky requirement
The system MUST store the key {fake_key} safely.

- Scenario S1.1: s
  - GIVEN a
  - WHEN b
  - THEN c
"""
        out = _render(_ledger_file(tmp_path, contract=leaky))
        assert fake_key not in out
        assert "[REDACTED" in out


class TestRenderHelperUnit:
    def _capture(self, bc, exc):
        buf = io.StringIO()
        with redirect_stdout(buf):
            closeout._print_behavior_contract(bc, exc)
        return buf.getvalue()

    def test_malformed_parse_error_is_note_not_crash(self):
        bc = SimpleNamespace(present=True, requirements=(), parse_error="ValueError: boom")
        out = self._capture(bc, None)
        assert "malformed" in out
        assert "### Behavior Contract" in out

    def test_none_bc_prints_nothing(self):
        assert self._capture(None, None) == ""

    def test_absent_present_false_prints_nothing(self):
        bc = SimpleNamespace(present=False, requirements=(), parse_error=None)
        assert self._capture(bc, None) == ""

    def test_waived_takes_precedence_over_bc(self):
        # even with a valid-looking bc, an active exception renders the waived note
        bc = SimpleNamespace(present=True, requirements=(), parse_error=None)
        out = self._capture(bc, "waived: contract upstream")
        assert "waived" in out
