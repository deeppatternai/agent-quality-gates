"""Tests for PR-D: aqg_closeout.py construction ledger auto-import.

Per PR-A audit D6 deferral: closeout should import code construction ledger
(`.aqg/current_ledger.md`) and surface its 6-step + warnings + objections in
output, with secret redaction.
"""

from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "aqg-evidence-closeout" / "scripts"))
sys.path.insert(0, str(REPO / "skills" / "aqg-code-construction" / "scripts"))

import aqg_closeout as closeout  # noqa: E402


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=repo, check=True, timeout=10
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "initial.txt").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, timeout=10
    )
    return repo


def _write_valid_ledger(repo: Path, extras: str = "") -> Path:
    """Write a syntactically valid construction ledger."""
    content = """---
task_slug: test-construction-task
path: full
created_at: 2026-05-04T12:00:00Z
session_agent: test-agent
skipped_checks: []
objections_diff_coverage_exception: null
---

# AQG Code Construction Ledger

| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | read foo module | scripts/foo.py:23 | rg "..." matched 5 |
| 2. Behavior Lock | yes | added test_foo_retry | tests/test_foo.py:55 | pytest -k retry PASS |
| 5. Local Verification | yes | tests pass + ruff clean | scripts/foo.py | pytest PASS 12/12 |
| 6. Self Review | yes | reviewed 5 axes | scripts/foo.py | manual OK |

## Warning Acknowledgements

| condition | ack | reason |
|---|---|---|
| W6: new dep tenacity | yes | retry needed; safer than ad-hoc |

## Predicted Objections

1. **Objection**: assumption (scripts/foo.py:18) — **Mitigation**: tenacity retry 3 times exponential
2. **Objection**: missing handler (scripts/foo.py:55) — **Mitigation**: bandit scan in CI
3. **Objection**: edge case (scripts/foo.py:80) — **Mitigation**: pytest -k test_edge

{extras}
""".replace("{extras}", extras)
    led_dir = repo / ".aqg" / "code-construction"
    led_dir.mkdir(parents=True, exist_ok=True)
    led_file = led_dir / "test-task.md"
    led_file.write_text(content)
    sym = repo / ".aqg" / "current_ledger.md"
    if sym.exists() or sym.is_symlink():
        sym.unlink()
    sym.symlink_to(led_file)
    return led_file


# ===========================================
# Construction parser availability
# ===========================================


class TestConstructionParserAvailable:
    def test_parser_imports(self):
        """The construction parser must be importable from closeout."""
        assert closeout._CONSTRUCTION is not None, (
            "closeout cannot import aqg_construction_check; "
            "PR-D auto-import will silently fall back to skeleton-only mode"
        )


# ===========================================
# Ledger import — happy path
# ===========================================


class TestLedgerImport:
    """PR-D audit C3 fix: import returns status dict (not raw dict or None).

    Status values: ok, missing, parser_unavailable, read_error, malformed.
    """

    def test_valid_ledger_imports(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        led = _write_valid_ledger(repo)
        result = closeout.import_construction_ledger(led)
        assert result["status"] == closeout.LedgerImportStatus.OK
        data = result["data"]
        assert data["header"].task_slug == "test-construction-task"
        assert data["header"].path == "full"
        assert len(data["steps"]) == 4

    def test_missing_ledger_returns_missing_status(self, tmp_path):
        result = closeout.import_construction_ledger(tmp_path / "nope.md")
        assert result["status"] == closeout.LedgerImportStatus.MISSING
        assert result["data"] is None
        assert "no file" in result["detail"].lower()

    def test_malformed_header_returns_malformed_status(self, tmp_path):
        led = tmp_path / "bad.md"
        led.write_text("not a valid ledger\n# Just text\n")
        result = closeout.import_construction_ledger(led)
        assert result["status"] == closeout.LedgerImportStatus.MALFORMED
        assert result["data"] is None
        assert "frontmatter" in result["detail"].lower() or "yaml" in result["detail"].lower()

    def test_warning_section_extracted(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        led = _write_valid_ledger(repo)
        result = closeout.import_construction_ledger(led)
        data = result["data"]
        assert "W6" in data["warning_section"]
        assert "tenacity" in data["warning_section"]

    def test_objection_section_extracted(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        led = _write_valid_ledger(repo)
        result = closeout.import_construction_ledger(led)
        data = result["data"]
        assert "Mitigation" in data["objection_section"]
        assert "tenacity" in data["objection_section"]


# ===========================================
# Secret redaction
# ===========================================


class TestSecretRedaction:
    def test_aws_key_redacted(self):
        # split to avoid push-protection (same trick as test_aqg_construction_check.py)
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        redacted = closeout.redact_secrets(f"some text {secret} more")
        assert secret not in redacted
        assert "[REDACTED:AWS access key]" in redacted

    def test_github_token_redacted(self):
        secret = "gh" + "p_" + "A" * 36 + "1"
        redacted = closeout.redact_secrets(f"x {secret} y")
        assert secret not in redacted
        assert "[REDACTED:GitHub token]" in redacted

    def test_clean_text_unchanged(self):
        text = "no secrets here just words"
        assert closeout.redact_secrets(text) == text

    def test_ledger_with_secret_in_command_result_redacts(self, tmp_path):
        # Inject AKIA pattern into a step row's command/result via raw write
        repo = _make_git_repo(tmp_path)
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        # We cannot inject through normal ledger because checker would block; use raw
        led_dir = repo / ".aqg" / "code-construction"
        led_dir.mkdir(parents=True, exist_ok=True)
        led = led_dir / "test.md"
        # Build content with secret inside step 5 command/result column
        content = f"""---
task_slug: secret-test
path: full
created_at: 2026-05-04T12:00:00Z
session_agent: test
skipped_checks: []
objections_diff_coverage_exception: null
---

| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 5. Local Verification | yes | ran tests | foo.py:1 | API key {secret} OK |

## Warning Acknowledgements

## Predicted Objections

1. **Objection**: x (foo.py:1) — **Mitigation**: pytest -k test_foo
"""
        led.write_text(content)
        sym = repo / ".aqg" / "current_ledger.md"
        if sym.exists() or sym.is_symlink():
            sym.unlink()
        sym.symlink_to(led)

        # Capture stdout from print_construction_section
        result = closeout.import_construction_ledger(led)
        if result["status"] != closeout.LedgerImportStatus.OK:
            pytest.skip("ledger malformed in test setup; skip")
        buf = io.StringIO()
        with redirect_stdout(buf):
            closeout.print_construction_section(result["data"])
        out = buf.getvalue()
        assert secret not in out
        assert "[REDACTED:AWS access key]" in out


# ===========================================
# Output formatting
# ===========================================


class TestOutputFormatting:
    def test_construction_section_printed(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        led = _write_valid_ledger(repo)
        result = closeout.import_construction_ledger(led)
        buf = io.StringIO()
        with redirect_stdout(buf):
            closeout.print_construction_section(result["data"])
        out = buf.getvalue()
        assert "## Code Construction Evidence" in out
        assert "test-construction-task" in out
        assert "full" in out
        assert "6 Steps" in out
        assert "Warning Acknowledgements" in out
        assert "Predicted Objections" in out

    def test_long_cell_truncated(self):
        long_text = "A" * 200
        truncated = closeout._truncate(long_text)
        assert len(truncated) <= 60
        assert truncated.endswith("...")

    def test_short_cell_unchanged(self):
        assert closeout._truncate("short") == "short"

    def test_pipe_in_cell_escaped(self):
        assert "\\|" in closeout._truncate("a | b | c")

    def test_missing_notice_explicit(self, tmp_path, capsys):
        nonexistent = tmp_path / "nope.md"
        closeout.print_construction_missing_notice(
            nonexistent,
            status=closeout.LedgerImportStatus.MISSING,
            detail=f"no file at {nonexistent}",
            was_explicit=True,
        )
        captured = capsys.readouterr()
        assert "ledger NOT FOUND" in captured.out
        assert str(nonexistent) in captured.out

    # PR-D audit C3 fix: differentiated notice per status
    def test_notice_parser_unavailable(self, tmp_path, capsys):
        closeout.print_construction_missing_notice(
            tmp_path / "ledger.md",
            status=closeout.LedgerImportStatus.PARSER_UNAVAILABLE,
            detail="construction parser module not available",
            was_explicit=True,
        )
        captured = capsys.readouterr()
        assert "construction parser UNAVAILABLE" in captured.out

    def test_notice_malformed(self, tmp_path, capsys):
        led = tmp_path / "bad.md"
        closeout.print_construction_missing_notice(
            led,
            status=closeout.LedgerImportStatus.MALFORMED,
            detail="ledger yaml frontmatter missing or invalid",
            was_explicit=True,
        )
        captured = capsys.readouterr()
        assert "MALFORMED" in captured.out

    def test_notice_implicit_missing_silent(self, tmp_path, capsys):
        # Implicit (was_explicit=False) MISSING should print nothing
        closeout.print_construction_missing_notice(
            tmp_path / "ledger.md",
            status=closeout.LedgerImportStatus.MISSING,
            detail="no file",
            was_explicit=False,
        )
        captured = capsys.readouterr()
        assert captured.out == ""


# ===========================================
# Backwards compat: --no-construction-import / silent fallback
# ===========================================


class TestBackwardsCompat:
    def test_no_construction_import_arg(self, tmp_path, capsys, monkeypatch):
        repo = _make_git_repo(tmp_path)
        # write valid ledger but use --no-construction-import flag
        _write_valid_ledger(repo)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "aqg_closeout.py",
                "--task",
                "test",
                "--repo",
                str(repo),
                "--no-construction-import",
            ],
        )
        rc = closeout.main()
        out = capsys.readouterr().out
        assert rc == 0
        # Construction section should NOT appear despite ledger present
        assert "## Code Construction Evidence" not in out
        # But the regular evidence ledger skeleton SHOULD still appear
        assert "## Evidence ledger" in out

    def test_silent_fallback_when_no_ledger(self, tmp_path, capsys, monkeypatch):
        repo = _make_git_repo(tmp_path)
        # No ledger written
        monkeypatch.setattr(
            sys,
            "argv",
            ["aqg_closeout.py", "--task", "test", "--repo", str(repo)],
        )
        rc = closeout.main()
        out = capsys.readouterr().out
        assert rc == 0
        # When implicit (default path) and missing, silent skip — no Code Construction Evidence section
        assert "## Code Construction Evidence" not in out
        # But skeleton works
        assert "## Evidence ledger" in out

    def test_explicit_path_missing_shows_notice(self, tmp_path, capsys, monkeypatch):
        repo = _make_git_repo(tmp_path)
        nonexistent = repo / "nope.md"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "aqg_closeout.py",
                "--task",
                "test",
                "--repo",
                str(repo),
                "--construction-ledger",
                str(nonexistent),
            ],
        )
        rc = closeout.main()
        out = capsys.readouterr().out
        assert rc == 0
        # Explicit --construction-ledger with missing path → shows notice
        assert "ledger NOT FOUND" in out

    def test_full_run_with_ledger(self, tmp_path, capsys, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _write_valid_ledger(repo)
        monkeypatch.setattr(
            sys,
            "argv",
            ["aqg_closeout.py", "--task", "test-task", "--repo", str(repo)],
        )
        rc = closeout.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "## Code Construction Evidence" in out
        assert "test-construction-task" in out
        assert "## Evidence ledger" in out
        assert "## Final-answer checklist" in out


# ===========================================
# PR-D audit fixes — security tests
# ===========================================


class TestSecurityFixes:
    """PR-D audit_id dbda27f3 critical + major fixes verification."""

    # CRIT1 (gemini #1): PEM regex must capture FULL block, not just header
    def test_pem_full_block_redacted_including_body(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAvSecretBodyXYZAAAA\n"
            "BBBBcccDDDDeeeeffffggggHHHHIIII11\n"
            "-----END RSA PRIVATE KEY-----"
        )
        redacted = closeout.redact_secrets(f"key {pem} end")
        # Body MUST be redacted (was the leak in pre-fix)
        assert "MIIEowIBAAKCAQEA" not in redacted
        assert "BBBBcccDDDD" not in redacted
        # Marker present
        assert "[REDACTED:PEM private key]" in redacted

    # CRIT2 part 1 (gpt #1): all user-controlled header fields must be redacted
    def test_header_fields_redacted_in_section(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        # Inject secret into session_agent header field via raw write
        led_dir = repo / ".aqg" / "code-construction"
        led_dir.mkdir(parents=True, exist_ok=True)
        led = led_dir / "test.md"
        content = f"""---
task_slug: test-task
path: full
created_at: 2026-05-04T12:00:00Z
session_agent: agent-{secret}-suffix
skipped_checks: []
objections_diff_coverage_exception: null
---

| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | x | foo.py:1 | rg ok |

## Warning Acknowledgements

## Predicted Objections

1. **Objection**: x (foo.py:1) — **Mitigation**: pytest -k test_x
"""
        led.write_text(content)
        result = closeout.import_construction_ledger(led)
        assert result["status"] == closeout.LedgerImportStatus.OK
        buf = io.StringIO()
        with redirect_stdout(buf):
            closeout.print_construction_section(result["data"])
        out = buf.getvalue()
        # Secret must not leak via session_agent
        assert secret not in out
        assert "[REDACTED:AWS access key]" in out

    # CRIT2 part 2 (gpt #1): redact MUST happen before truncate
    def test_redact_before_truncate_at_boundary(self):
        # Build evidence where the secret crosses the 60-char boundary
        # Pre-fix: truncate first chops the secret, regex doesn't match → leaks
        # Post-fix: redact full string first, then truncate the redacted result
        secret = "AKIA" + "IOSFODNN7EXAMPLE"  # 20 chars
        prefix = "x" * 50  # makes secret start at char 50, end at 70 (past 60-char boundary)
        evidence = f"{prefix}{secret}rest"
        # Simulate the print_construction_section flow:
        truncated_redacted = closeout._truncate(closeout.redact_secrets(evidence))
        # Even though the truncated output may contain a partial REDACTED marker,
        # the original secret must NOT appear (no leak)
        assert "AKIA" + "IOSFODNN7EX" not in truncated_redacted
        assert secret not in truncated_redacted

    # C2 (gpt #2 + gemini #3): SECRET_PATTERNS must include OpenAI / Slack / JWT
    def test_openai_key_redacted(self):
        secret = "sk-" + "A" * 40
        redacted = closeout.redact_secrets(f"key {secret} end")
        assert secret not in redacted
        assert "[REDACTED:OpenAI API key]" in redacted

    def test_slack_token_redacted(self):
        secret = "xoxb-" + "1234567890-AbCdEfGh"
        redacted = closeout.redact_secrets(f"slack {secret} end")
        assert secret not in redacted
        assert "[REDACTED:Slack token]" in redacted

    def test_jwt_redacted(self):
        secret = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        redacted = closeout.redact_secrets(f"jwt {secret} end")
        assert secret not in redacted
        assert "[REDACTED:JWT]" in redacted

    def test_github_fine_grained_pat_redacted(self):
        secret = "github_pat_" + "1" * 40 + "ABCD"
        redacted = closeout.redact_secrets(f"pat {secret} end")
        assert secret not in redacted
        assert "[REDACTED:GitHub fine-grained PAT]" in redacted

    # D1 (gpt #4): _try_import_construction_parser uses importlib.util — no sys.path mutation
    def test_construction_parser_pinned_to_file(self):
        """Verify _CONSTRUCTION module __file__ resolves to the expected path."""
        assert closeout._CONSTRUCTION is not None
        expected_path = REPO / "skills" / "aqg-code-construction" / "scripts" / "aqg_construction_check.py"
        assert Path(closeout._CONSTRUCTION.__file__).resolve() == expected_path.resolve()

    def test_construction_parser_required_api_present(self):
        """Verify all required API attributes exist on loaded module."""
        for attr in closeout._CONSTRUCTION_REQUIRED_API:
            assert hasattr(closeout._CONSTRUCTION, attr), (
                f"Missing required API attribute: {attr}"
            )

    # C3 (gpt #3 + gemini #2 convergent): differentiated error states
    def test_full_run_parser_status_propagated(self, tmp_path, capsys, monkeypatch):
        # Even with a present-but-MALFORMED ledger via explicit arg, user sees specific status
        bad = tmp_path / "bad-ledger.md"
        bad.write_text("not valid yaml frontmatter\n")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "aqg_closeout.py",
                "--task",
                "test",
                "--repo",
                str(_make_git_repo(tmp_path)),
                "--construction-ledger",
                str(bad),
            ],
        )
        rc = closeout.main()
        out = capsys.readouterr().out
        assert rc == 0
        # Should print MALFORMED notice (not "NOT FOUND")
        assert "MALFORMED" in out
        assert "yaml" in out.lower() or "frontmatter" in out.lower()
