"""Tests for the Behavior Contract feature (AQG 0.14.0).

Absorbs OpenSpec requirement/scenario into the aqg-code-construction Behavior
Lock. Design: docs/discussion/2026-07-09-behavior-contract-openspec-absorb-a3.md
(LOCKED, two Deep audit rounds 7f24f960 → 0041b460).

Covers, per the round-1/round-2 adjudication:
- parse_behavior_contract: section bounds, per-requirement statement vs scenario
  region, normative keyword (case-sensitive) only in statement, GIVEN/WHEN/THEN
  (case-insensitive) in scenario, total/never-raises.
- _check_behavior_contract advisories: BC0 (parse crash), BC1 (per-requirement
  keyword + missing section), BC2 (per-scenario markers), BC3 (covers coverage +
  dangling + missing covers token), BC4 (duplicate id).
- scope gating (has_prod × tier), exception hatch (case-insensitive null/none),
  legacy-ledger safety, and the warn-only exit-0 invariant via main().
"""

from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stderr
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "aqg-code-construction" / "scripts"))

import aqg_construction_check as acc  # noqa: E402


WELL_FORMED_CONTRACT = """## Behavior Contract

### R1: OTP challenge on valid credentials
The system MUST present an OTP challenge when a 2FA user submits valid credentials.

- Scenario S1.1: valid creds with 2FA
  - GIVEN a user with 2FA enabled
  - WHEN the user submits valid credentials
  - THEN an OTP challenge is presented
"""


def _ledger(
    contract: str = WELL_FORMED_CONTRACT,
    row2_evidence: str = "R1 locked by test_otp; covers: R1",
    header_extra: str = "",
    path: str = "full",
) -> str:
    """Build a full ledger content string with a behavior contract section."""
    extra = f"\n{header_extra}" if header_extra else ""
    return f"""---
task_slug: bc-test
path: {path}
created_at: 2020-01-01T00:00:00Z
session_agent: test-agent
skipped_checks: []
objections_diff_coverage_exception: null{extra}
---

| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | read | initial.txt:1 | rg OK |
| 2. Behavior Lock | yes | {row2_evidence} | initial.txt:1 | pytest PASS |
| 5. Local Verification | yes | tests | initial.txt:1 | pytest PASS |
| 6. Self Review | yes | reviewed | initial.txt:1 | OK |

{contract}

## Warning Acknowledgements

## Predicted Objections

1. **Objection**: risk one (scripts/foo.py:1) — **Mitigation**: pytest -k test_one
2. **Objection**: risk two (scripts/foo.py:1) — **Mitigation**: ruff check scripts/
3. **Objection**: risk three (scripts/foo.py:1) — **Mitigation**: mypy scripts/
"""


def _check(content: str, changed_files=None) -> list[str]:
    changed_files = changed_files if changed_files is not None else ["scripts/foo.py"]
    header = acc._parse_ledger_header(content)
    assert header is not None, "test ledger header must parse"
    return acc._check_behavior_contract(content, header, changed_files)


# ===== parse_behavior_contract =====


class TestParse:
    def test_wellformed_two_scenarios(self):
        c = acc.parse_behavior_contract(WELL_FORMED_CONTRACT)
        assert c.present is True
        assert c.parse_error is None
        assert [r.id for r in c.requirements] == ["R1"]
        r1 = c.requirements[0]
        assert r1.has_normative is True
        assert len(r1.scenarios) == 1
        assert r1.scenarios[0].markers == frozenset({"GIVEN", "WHEN", "THEN"})

    def test_absent_section(self):
        c = acc.parse_behavior_contract("no contract here\n")
        assert c.present is False
        assert c.parse_error is None
        assert list(c.requirements) == []

    def test_keyword_only_in_scenario_not_statement(self):
        # "MUST" appears in a THEN line but NOT in the requirement statement.
        contract = """## Behavior Contract

### R1: loose requirement
This requirement describes a behavior without a normative verb.

- Scenario S1.1: s
  - GIVEN x
  - WHEN y
  - THEN the value MUST be present
"""
        c = acc.parse_behavior_contract(contract)
        assert c.requirements[0].has_normative is False

    def test_title_case_markers_accepted(self):
        contract = """## Behavior Contract

### R1: t
The system SHALL do a thing.

- Scenario S1.1: s
  - Given x
  - When y
  - Then z
"""
        c = acc.parse_behavior_contract(contract)
        assert c.requirements[0].scenarios[0].markers == frozenset(
            {"GIVEN", "WHEN", "THEN"}
        )

    def test_section_ends_at_next_h2(self):
        contract = WELL_FORMED_CONTRACT + "\n## Warning Acknowledgements\n\n### R9: leaked\nThe system MUST NOT leak.\n"
        c = acc.parse_behavior_contract(contract)
        assert [r.id for r in c.requirements] == ["R1"]  # R9 is outside the section

    def test_total_never_raises_on_garbage(self):
        for junk in ("", "## Behavior Contract", "### R1\nno colon heading", "\x00\x01"):
            c = acc.parse_behavior_contract(junk)
            assert c is not None  # never raises


# ===== _check_behavior_contract advisories =====


class TestChecks:
    def test_wellformed_no_advisories(self):
        assert _check(_ledger()) == []

    def test_bc1_missing_section(self):
        advs = _check(_ledger(contract="(no contract section)"))
        assert any(a.startswith("BC1") and "missing" in a for a in advs)

    def test_bc1_requirement_without_keyword(self):
        contract = """## Behavior Contract

### R1: no normative verb here
This just narrates behavior, no MUST in the statement... wait, avoid it.

- Scenario S1.1: s
  - GIVEN a
  - WHEN b
  - THEN c
"""
        # Rewrite so the statement genuinely lacks a keyword.
        contract = contract.replace(
            "This just narrates behavior, no MUST in the statement... wait, avoid it.",
            "This narrates behavior only.",
        )
        advs = _check(_ledger(contract=contract, row2_evidence="covers: R1"))
        assert any(a.startswith("BC1") and "R1" in a for a in advs)

    def test_bc2_scenario_missing_marker(self):
        contract = """## Behavior Contract

### R1: t
The system MUST do a thing.

- Scenario S1.1: s
  - GIVEN a
  - WHEN b
"""
        advs = _check(_ledger(contract=contract, row2_evidence="covers: R1"))
        assert any(a.startswith("BC2") and "S1.1" in a and "THEN" in a for a in advs)

    def test_bc2_requirement_without_scenario(self):
        contract = """## Behavior Contract

### R1: t
The system MUST do a thing.
"""
        advs = _check(_ledger(contract=contract, row2_evidence="covers: R1"))
        assert any(a.startswith("BC2") and "R1" in a for a in advs)

    def test_bc3_uncovered_requirement(self):
        two = WELL_FORMED_CONTRACT + """
### R2: second
The system MUST also do a second thing.

- Scenario S2.1: s
  - GIVEN a
  - WHEN b
  - THEN c
"""
        advs = _check(_ledger(contract=two, row2_evidence="covers: R1"))
        assert any(a.startswith("BC3") and "R2" in a for a in advs)

    def test_bc3_dangling_citation(self):
        advs = _check(_ledger(row2_evidence="covers: R1, R99"))
        assert any(a.startswith("BC3") and "R99" in a for a in advs)

    def test_bc3_missing_covers_token(self):
        advs = _check(_ledger(row2_evidence="locked by test_otp (no covers token)"))
        assert any(a.startswith("BC3") and "covers" in a for a in advs)

    def test_bc3_incidental_r_token_ignored(self):
        # 'R99' appears as incidental prose, not in a covers: list → not dangling.
        advs = _check(
            _ledger(row2_evidence="see ticket R99 for context; covers: R1")
        )
        assert not any("R99" in a for a in advs)

    def test_bc3_discovers_substring_not_matched_as_covers(self):
        # 'discovers: R1' must NOT satisfy the covers citation (\b anchor).
        advs = _check(_ledger(row2_evidence="this discovers: R1 in passing"))
        assert any(a.startswith("BC3") and "covers" in a for a in advs)

    def test_bc3_covers_word_boundary_real_token_matches(self):
        # a real 'covers: R1' next to an incidental 'recovers:' still resolves.
        advs = _check(_ledger(row2_evidence="recovers: X; covers: R1"))
        assert not any(a.startswith("BC3") for a in advs)

    def test_bc2_marker_word_in_scenario_title_not_counted(self):
        contract = """## Behavior Contract

### R1: t
The system MUST do a thing.

- Scenario S1.1: what to do when the user is banned
  - GIVEN a
  - THEN c
"""
        advs = _check(_ledger(contract=contract, row2_evidence="covers: R1"))
        # WHEN appears only in the title line → must still be reported missing
        assert any(a.startswith("BC2") and "S1.1" in a and "WHEN" in a for a in advs)

    def test_bc4_duplicate_id(self):
        dup = WELL_FORMED_CONTRACT + """
### R1: duplicate id
The system MUST not reuse an id.

- Scenario S1.2: s
  - GIVEN a
  - WHEN b
  - THEN c
"""
        advs = _check(_ledger(contract=dup, row2_evidence="covers: R1"))
        assert any(a.startswith("BC4") and "R1" in a for a in advs)

    def test_bc0_parse_crash_is_advisory_not_exception(self, monkeypatch):
        def boom(_content):
            raise ValueError("synthetic parse crash")

        monkeypatch.setattr(acc, "parse_behavior_contract", boom)
        advs = _check(_ledger())  # must not raise
        assert any(a.startswith("BC0") for a in advs)


# ===== scope + exception gating =====


class TestScope:
    def test_non_prod_change_no_advisories(self):
        # Missing section would warn on prod; on docs-only it must not.
        assert _check(_ledger(contract="(none)"), changed_files=["README.md"]) == []

    def test_mini_tier_no_advisories(self):
        assert _check(_ledger(contract="(none)", path="mini")) == []

    def test_exception_active_suppresses(self):
        advs = _check(
            _ledger(
                contract="(none)",
                header_extra="behavior_contract_exception: emergency hotfix, contract in PR #123",
            )
        )
        assert advs == []

    def test_exception_null_is_inactive(self):
        # 'null' (any case) must NOT activate the exception.
        for val in ("null", "NULL", "None", "~", ""):
            advs = _check(
                _ledger(
                    contract="(none)",
                    header_extra=f"behavior_contract_exception: {val}",
                )
            )
            assert any(a.startswith("BC1") for a in advs), f"val={val!r} wrongly suppressed"

    def test_exception_vague_is_inactive(self):
        advs = _check(
            _ledger(contract="(none)", header_extra="behavior_contract_exception: TBD")
        )
        assert any(a.startswith("BC1") for a in advs)

    def test_legacy_ledger_without_field_is_safe(self):
        # A ledger whose header lacks behavior_contract_exception (the default
        # builder omits it) must parse and check without raising (backward compat).
        content = _ledger()
        assert "behavior_contract_exception" not in content  # legacy: field absent
        header = acc._parse_ledger_header(content)
        assert header is not None
        assert header.behavior_contract_exception is None
        # Must not raise:
        acc._check_behavior_contract(content, header, ["scripts/foo.py"])


# ===== warn-only exit-0 invariant (integration via main) =====


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "initial.txt").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, timeout=10)
    return repo


class TestWarnOnlyExitZero:
    def test_broken_contract_does_not_block(self, tmp_path, monkeypatch):
        """A prod change with a MISSING/broken behavior contract must still exit 0
        in 0.14.0 (warn-only), emitting a BC warning to stderr — never a block."""
        repo = _make_git_repo(tmp_path)
        # stage a prod-path file so has_prod is true
        (repo / "scripts").mkdir()
        (repo / "scripts" / "foo.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "scripts/foo.py"], cwd=repo, check=True)

        # ledger with a valid header/steps/objections but NO behavior contract
        led_dir = repo / ".aqg" / "code-construction"
        led_dir.mkdir(parents=True)
        content = _ledger(contract="(no behavior contract at all)")
        led = led_dir / "bc-test.md"
        led.write_text(content)
        (repo / ".aqg" / "current_ledger.md").symlink_to(led)

        monkeypatch.setenv("AQG_AGENT", "claude")
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = acc.main(["--mode", "staged", "--cwd", str(repo)])
        assert rc == acc.EXIT_OK, f"BC must be warn-only, got exit {rc}"
        assert "BC1" in buf.getvalue(), "expected a BC1 warning on stderr"

    def test_bc_warning_does_not_force_ack_block(self, tmp_path, monkeypatch):
        """BC advisories must NOT enter the warnings list gated by
        _check_warning_acknowledgements (which would hard-block)."""
        repo = _make_git_repo(tmp_path)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "foo.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "scripts/foo.py"], cwd=repo, check=True)
        led_dir = repo / ".aqg" / "code-construction"
        led_dir.mkdir(parents=True)
        # broken contract (missing covers) but NO Warning Acknowledgements rows
        content = _ledger(row2_evidence="no covers token here")
        led = led_dir / "bc-test.md"
        led.write_text(content)
        (repo / ".aqg" / "current_ledger.md").symlink_to(led)
        monkeypatch.setenv("AQG_AGENT", "claude")
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = acc.main(["--mode", "staged", "--cwd", str(repo)])
        assert rc == acc.EXIT_OK
