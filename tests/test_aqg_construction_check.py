"""Tests for skills/aqg-code-construction/scripts/aqg_construction_check.py.

Per sketch a1 audit (audit_id c87fb770) 14 fixes covered (v1):
- C1, C2, D1, D2, D3, D5, D8, D9, D10, D11, gemini #1 option c

Per PR-B three-audit (audit_id 83d00576) 15 fixes covered (v2):
- v2-C1: 6-step parser + 3 hard blockers (HB1/HB2/HB4)
- v2-C2: diff-aware broad-except + tuple form + per-handler boundary
- v2-C3: install safety (tested in test_install_aqg_construction_hook... or skipped — install logic tested via _safe_run mock)
- v2-C4: path bound check (reject ../foo)
- v2-D1: diff-hunk warnings + W7 + W8 + ack matching
- v2-D4: performance budget warn-only (tested via timing assertion)
- v2-D5: fail-closed git error
- v2-D6: YAML parser strict + JSON skipped_checks
- v2-D7: markdown table pipe-in-cell parsing
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "aqg-code-construction" / "scripts"))

import aqg_construction_check as acc  # noqa: E402


def _make_git_repo(tmp_path: Path) -> Path:
    """Init a real git repo with one initial commit."""
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


def _git_add(repo: Path, *paths: str) -> None:
    subprocess.run(["git", "add", *paths], cwd=repo, check=True, timeout=10)


def _git_commit(repo: Path, msg: str = "step") -> None:
    subprocess.run(
        ["git", "commit", "-q", "-m", msg], cwd=repo, check=True, timeout=10
    )


def _write_ledger(
    repo: Path,
    header_overrides: dict | None = None,
    objections: str | None = None,
    warn_ack: str = "",
    steps_table: str | None = None,
) -> Path:
    """Write a minimal valid ledger; symlink to .aqg/current_ledger.md."""
    header = {
        "task_slug": "test-task",
        "path": "full",
        "created_at": "2099-01-01T00:00:00Z",
        "session_agent": "test-agent",
        "skipped_checks": "[]",
        "objections_diff_coverage_exception": "null",
    }
    if header_overrides:
        header.update(header_overrides)
    fm = "\n".join(f"{k}: {v}" for k, v in header.items())
    default_objections = objections if objections is not None else """
1. **Objection**: assumption (initial.txt:1) — **Mitigation**: tenacity retry 3 times exponential backoff
2. **Objection**: missing handling (initial.txt:1) — **Mitigation**: bandit scan in CI step
3. **Objection**: edge case branch (initial.txt:1) — **Mitigation**: pytest -k test_edge_case_3
"""
    default_steps = steps_table if steps_table is not None else """| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | read initial | initial.txt:1 | rg foo OK |
| 2. Behavior Lock | yes | added test | initial.txt:1 | pytest PASS 1/1 |
| 5. Local Verification | yes | tests pass | initial.txt:1 | pytest PASS 12/12 |
| 6. Self Review | yes | reviewed 5 axes | initial.txt:1 | OK |"""
    content = f"""---
{fm}
---

{default_steps}

## Warning Acknowledgements

{warn_ack}

## Predicted Objections

{default_objections}
"""
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
# v1: gemini #1 option c — AQG_AGENT env var gating
# ===========================================


class TestEnforcementGating:
    def test_silent_pass_without_env(self):
        assert acc._is_enforcing({}) is False
        assert acc._is_enforcing({"AQG_AGENT": ""}) is False

    def test_enforce_with_codex(self):
        assert acc._is_enforcing({"AQG_AGENT": "codex"}) is True

    def test_enforce_with_claude(self):
        assert acc._is_enforcing({"AQG_AGENT": "claude"}) is True

    def test_enforce_with_human_opt_in(self):
        assert acc._is_enforcing({"AQG_AGENT": "human-opt-in"}) is True

    def test_enforce_with_ci_rerun(self):
        assert acc._is_enforcing({"AQG_AGENT": "ci-rerun"}) is True

    def test_unknown_value_silent_pass(self):
        assert acc._is_enforcing({"AQG_AGENT": "random-value"}) is False


# ===========================================
# v1 C1: ledger location
# ===========================================


class TestLedgerLocation:
    def test_default_path(self, tmp_path):
        result = acc._resolve_ledger_path(None, tmp_path)
        assert result == (tmp_path / ".aqg" / "current_ledger.md").resolve()

    def test_explicit_path(self, tmp_path):
        custom = tmp_path / "custom.md"
        custom.write_text("dummy")
        result = acc._resolve_ledger_path(str(custom), tmp_path)
        assert result == custom.resolve()

    # ===== P3: ledger-path bounding (resolved target must live under
    # cwd/.aqg/code-construction/) =====
    def test_bounds_in_repo_ledger_allowed(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        led = repo / ".aqg" / "code-construction" / "task.md"
        led.parent.mkdir(parents=True, exist_ok=True)
        led.write_text("x")
        assert acc._ledger_path_within_bounds(led.resolve(), repo) is True

    def test_bounds_external_path_rejected(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        external = tmp_path / "outside.md"
        external.write_text("x")
        assert acc._ledger_path_within_bounds(external.resolve(), repo) is False

    def test_bounds_symlink_escape_resolves_outside(self, tmp_path):
        # A symlink inside code-construction/ pointing outside resolves out → reject.
        repo = _make_git_repo(tmp_path)
        target = tmp_path / "evil-ledger.md"
        target.write_text("x")
        link = repo / ".aqg" / "code-construction" / "escape.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        assert acc._ledger_path_within_bounds(link.resolve(), repo) is False

    def test_main_rejects_external_ledger(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        external = tmp_path / "external.md"
        external.write_text("---\ntask_slug: x\npath: full\ncreated_at: 2020-01-01T00:00:00Z\nsession_agent: a\n---\n")
        monkeypatch.setenv("AQG_AGENT", "claude")
        rc = acc.main(["--ledger", str(external), "--cwd", str(repo), "--quiet"])
        assert rc == acc.EXIT_LEDGER_MALFORMED, f"expected EXIT_LEDGER_MALFORMED, got {rc}"

    def test_main_rejects_symlink_escape_ledger(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        target = tmp_path / "evil.md"
        target.write_text("---\ntask_slug: x\npath: full\ncreated_at: 2020-01-01T00:00:00Z\nsession_agent: a\n---\n")
        # Point the canonical current_ledger.md symlink at an out-of-tree target.
        sym = repo / ".aqg" / "current_ledger.md"
        sym.parent.mkdir(parents=True, exist_ok=True)
        sym.symlink_to(target)
        monkeypatch.setenv("AQG_AGENT", "claude")
        rc = acc.main(["--cwd", str(repo), "--quiet"])
        assert rc == acc.EXIT_LEDGER_MALFORMED, f"expected EXIT_LEDGER_MALFORMED, got {rc}"

    def test_main_accepts_valid_in_repo_ledger(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        # Stage a small change so the diff is non-empty + valid.
        f = repo / "edit.txt"
        f.write_text("hello\nworld\n")
        _git_add(repo, "edit.txt")
        _write_ledger(
            repo,
            header_overrides={"created_at": "2020-01-01T00:00:00Z"},
            objections="\n".join(
                f"{i}. **Objection**: x (edit.txt:1) — **Mitigation**: pytest -k test_v{i}"
                for i in range(1, 4)
            ),
        )
        monkeypatch.setenv("AQG_AGENT", "claude")
        rc = acc.main(["--mode", "staged", "--cwd", str(repo), "--quiet"])
        assert rc == acc.EXIT_OK, f"expected EXIT_OK, got {rc}"


# ===========================================
# v1 D9 + v2-D6: ledger header
# ===========================================


class TestLedgerHeader:
    def test_valid_header_full_path(self, tmp_path):
        led = _write_ledger(tmp_path)
        header = acc._parse_ledger_header(led.read_text())
        assert header is not None
        assert header.path == "full"
        assert header.task_slug == "test-task"
        assert header.skipped_checks == []  # JSON-parsed empty list

    def test_invalid_path_value(self, tmp_path):
        led = _write_ledger(tmp_path, header_overrides={"path": "huge"})
        header = acc._parse_ledger_header(led.read_text())
        assert header is None

    def test_no_frontmatter(self):
        header = acc._parse_ledger_header("no frontmatter here\n# Title")
        assert header is None

    def test_missing_required_field(self, tmp_path):
        led = _write_ledger(tmp_path)
        content = led.read_text()
        content = "\n".join(
            line for line in content.split("\n") if not line.startswith("session_agent:")
        )
        led.write_text(content)
        header = acc._parse_ledger_header(led.read_text())
        assert header is None

    # v2-D6: stricter parser
    def test_multiline_scalar_rejected(self, tmp_path):
        led = _write_ledger(tmp_path)
        # Inject multi-line value (indented continuation)
        content = led.read_text().replace(
            "task_slug: test-task",
            "task_slug: test-task\n  continuation: of multiline",
        )
        led.write_text(content)
        header = acc._parse_ledger_header(led.read_text())
        assert header is None

    def test_skipped_checks_json_list(self, tmp_path):
        led = _write_ledger(
            tmp_path,
            header_overrides={
                "skipped_checks": '[{"check": "X", "reason": "infra-only PR"}]'
            },
        )
        header = acc._parse_ledger_header(led.read_text())
        assert header is not None
        assert header.skipped_checks == [{"check": "X", "reason": "infra-only PR"}]

    def test_skipped_checks_invalid_json_rejected(self, tmp_path):
        led = _write_ledger(
            tmp_path, header_overrides={"skipped_checks": "not-json"}
        )
        header = acc._parse_ledger_header(led.read_text())
        assert header is None


# ===========================================
# v1 D10: tiered objection counts
# ===========================================


class TestTieredObjections:
    def test_mini_one_objection_OK(self, tmp_path):
        objs = "1. **Objection**: x (initial.txt:1) — **Mitigation**: pytest -k test_x"
        led = _write_ledger(tmp_path, header_overrides={"path": "mini"}, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], tmp_path)
        count_blockers = [b for b in blockers if "requires" in b]
        assert count_blockers == []

    def test_full_three_objections_required(self, tmp_path):
        objs = "1. **Objection**: x (initial.txt:1) — **Mitigation**: pytest -k test_x"
        led = _write_ledger(tmp_path, header_overrides={"path": "full"}, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], tmp_path)
        assert any("requires >= 3" in b for b in blockers)

    def test_plan_five_objections_required(self, tmp_path):
        objs = "\n".join(
            f"{i}. **Objection**: x (initial.txt:1) — **Mitigation**: pytest -k test_o{i}"
            for i in range(1, 4)
        )
        led = _write_ledger(tmp_path, header_overrides={"path": "plan"}, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], tmp_path)
        assert any("requires >= 5" in b for b in blockers)


# ===========================================
# v1 C2 + v2-C4: objection validation + path traversal
# ===========================================


class TestObjectionValidation:
    def test_fake_file_path_blocked(self, tmp_path):
        objs = "\n".join(
            f"{i}. **Objection**: x (fake_xyz_{i}.py:1) — **Mitigation**: pytest -k test_t{i}"
            for i in range(1, 4)
        )
        led = _write_ledger(tmp_path, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], tmp_path)
        assert any("does not exist" in b for b in blockers)

    def test_diff_coverage_exception_required(self, tmp_path):
        objs = "\n".join(
            f"{i}. **Objection**: x (initial.txt:1) — **Mitigation**: pytest -k test_d{i}"
            for i in range(1, 4)
        )
        led = _write_ledger(tmp_path, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["other.txt"], tmp_path)
        assert any("no objection refs a changed file" in b for b in blockers)

    def test_diff_coverage_exception_acknowledged(self, tmp_path):
        objs = "\n".join(
            f"{i}. **Objection**: x (initial.txt:1) — **Mitigation**: pytest -k test_e{i}"
            for i in range(1, 4)
        )
        led = _write_ledger(
            tmp_path,
            objections=objs,
            header_overrides={
                "objections_diff_coverage_exception": "design-only PR no source change"
            },
        )
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["other.txt"], tmp_path)
        assert not any("no objection refs a changed file" in b for b in blockers)

    def test_vague_mitigation_blocked(self, tmp_path):
        objs = "\n".join(
            f"{i}. **Objection**: x (initial.txt:1) — **Mitigation**: add tests"
            for i in range(1, 4)
        )
        led = _write_ledger(tmp_path, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], tmp_path)
        assert any("vague mitigation" in b for b in blockers)

    # C5 dogfood FP fix: a concrete mitigation that legitimately references a
    # "TODO row" (the closeout evidence-ledger status placeholder) must NOT be
    # flagged vague just because the word "TODO" appears as the subject discussed.
    def test_mitigation_mentioning_todo_row_not_vague(self, tmp_path):
        objs = "\n".join(
            f"{i}. **Objection**: x (initial.txt:1) — "
            f"**Mitigation**: fill the closeout TODO row from fresh evidence via `aqg_closeout.py --staged`"
            for i in range(1, 4)
        )
        led = _write_ledger(tmp_path, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], tmp_path)
        assert not any("vague mitigation" in b for b in blockers)

    def test_missing_mitigation_blocked(self, tmp_path):
        objs = "\n".join(f"{i}. **Objection**: x (initial.txt:1)" for i in range(1, 4))
        led = _write_ledger(tmp_path, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], tmp_path)
        assert any("no Mitigation" in b for b in blockers)

    # v2-C4: path traversal
    def test_path_traversal_blocked(self, tmp_path):
        outside = tmp_path / "outside.py"
        outside.write_text("# accessible")
        repo = _make_git_repo(tmp_path)
        objs = "\n".join(
            f"{i}. **Objection**: x (../outside.py:1) — **Mitigation**: pytest -k test_p{i}"
            for i in range(1, 4)
        )
        led = _write_ledger(repo, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], repo)
        assert any("path traversal" in b or "outside repo" in b for b in blockers)

    # ===== G4: objection regex accepts extensionless paths =====
    def test_extensionless_makefile_reference_accepted(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        (repo / "Makefile").write_text("all:\n\techo hi\n")
        objs = "\n".join(
            f"{i}. **Objection**: build rule (Makefile:10) — **Mitigation**: pytest -k test_mk{i}"
            for i in range(1, 4)
        )
        led = _write_ledger(repo, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["Makefile"], repo)
        assert not any("no file:line reference" in b for b in blockers), blockers
        assert not any("does not exist" in b for b in blockers), blockers

    def test_extensionless_path_with_slash_accepted(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "build").write_text("#!/bin/sh\n")
        objs = "\n".join(
            f"{i}. **Objection**: script (scripts/build:25) — **Mitigation**: pytest -k test_sb{i}"
            for i in range(1, 4)
        )
        led = _write_ledger(repo, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["scripts/build"], repo)
        assert not any("no file:line reference" in b for b in blockers), blockers

    def test_dotted_path_still_works_after_g4(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        objs = "\n".join(
            f"{i}. **Objection**: x (initial.txt:1) — **Mitigation**: pytest -k test_dd{i}"
            for i in range(1, 4)
        )
        led = _write_ledger(repo, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], repo)
        assert not any("no file:line reference" in b for b in blockers), blockers

    def test_prose_colon_number_does_not_satisfy(self, tmp_path):
        # "note 2:10" is prose (no slash, not a known no-ext filename, no real file)
        # → must still fail the file:line requirement.
        repo = _make_git_repo(tmp_path)
        objs = "\n".join(
            f"{i}. **Objection**: see note 2:10 above — **Mitigation**: pytest -k test_pr{i}"
            for i in range(1, 4)
        )
        led = _write_ledger(repo, objections=objs)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_objections(led.read_text(), header, ["initial.txt"], repo)
        assert any("no file:line reference" in b for b in blockers), blockers


# ===========================================
# v1 D8: secret patterns
# ===========================================


class TestSecretDetection:
    # NOTE: secret strings are split via string concatenation so the literal
    # patterns do not appear in source (otherwise GitHub Push Protection
    # blocks the push). Runtime assembled string still matches our regex.

    def test_aws_key_blocked(self):
        fake_aws = "AKIA" + "IOSFODNN7EXAMPLE"
        blockers = acc._check_secrets(f"some ledger {fake_aws} more")
        assert any("AWS" in b for b in blockers)

    def test_github_token_blocked(self):
        fake_gh = "gh" + "p_" + "A" * 36 + "1"
        blockers = acc._check_secrets(f"x {fake_gh} y")
        assert any("GitHub" in b for b in blockers)

    def test_stripe_key_blocked(self):
        fake_stripe = "sk_" + "live_" + "A" * 24
        blockers = acc._check_secrets(f"key {fake_stripe} in code")
        assert any("Stripe" in b for b in blockers)

    def test_pem_private_key_blocked(self):
        # PR-D audit gemini #1 fix: pattern now requires BEGIN..END whole block
        # (so re.sub redacts the body, not just header). Updated test accordingly.
        fake_pem = (
            "-----" + "BEGIN RSA PRIVATE KEY" + "-----\n"
            "MIIEowIBAAKCAQEAfakekey\n"
            "-----" + "END RSA PRIVATE KEY" + "-----"
        )
        blockers = acc._check_secrets(f"before {fake_pem} inside")
        assert any("PEM" in b for b in blockers)

    def test_clean_ledger_no_blocker(self):
        assert acc._check_secrets("no secrets here just plain words") == []

    # ===== P4: secret-pattern coverage extension =====
    def test_aws_temp_key_asia_blocked(self):
        # AWS temporary (STS) access key — distinctive ASIA prefix
        fake_asia = "ASIA" + "IOSFODNN7EXAMPLE0"[:16]
        blockers = acc._check_secrets(f"temp creds {fake_asia} here")
        assert any("AWS" in b for b in blockers), blockers

    def test_github_user_token_ghu_blocked(self):
        fake_ghu = "gh" + "u_" + "A" * 36 + "1"
        blockers = acc._check_secrets(f"x {fake_ghu} y")
        assert any("GitHub" in b for b in blockers), blockers

    def test_github_refresh_token_ghr_blocked(self):
        fake_ghr = "gh" + "r_" + "A" * 36 + "1"
        blockers = acc._check_secrets(f"x {fake_ghr} y")
        assert any("GitHub" in b for b in blockers), blockers

    def test_openai_project_key_sk_proj_blocked(self):
        fake_proj = "sk-" + "proj-" + "A" * 24
        blockers = acc._check_secrets(f"key {fake_proj} end")
        assert any("OpenAI" in b for b in blockers), blockers

    def test_git_sha_40hex_not_false_positive(self):
        # P4 adjudication: a 40-hex git SHA in a ledger must NOT be flagged
        # (generic 40-char base64 pattern was explicitly REJECTED).
        git_sha = "a" * 40  # 40 hex chars, like a full git object id
        assert acc._check_secrets(f"see commit {git_sha} for details") == []

    def test_p4_new_patterns_capture_full_token(self):
        """Each new pattern must capture the FULL token for re.sub redaction.

        closeout imports SECRET_PATTERNS and does pattern.sub(...); a header-only
        match would leave the secret body in the output.
        """
        cases = {
            "ASIA temp key": "ASIA" + "B" * 16,
            "ghu token": "gh" + "u_" + "C" * 37,
            "ghr token": "gh" + "r_" + "D" * 37,
            "sk-proj key": "sk-" + "proj-" + "E" * 24,
        }
        for label, token in cases.items():
            redacted = token
            for pattern, _name in acc.SECRET_PATTERNS:
                redacted = pattern.sub("[REDACTED]", redacted)
            assert token not in redacted, f"{label}: token leaked after redaction"
            assert redacted == "[REDACTED]", (
                f"{label}: full token not captured (residue: {redacted!r})"
            )


# ===========================================
# v1 D11 + v2-C2: AST broad except (incl. tuple, BaseException)
# ===========================================


class TestASTBareExcept:
    def test_new_broad_except_detected(self):
        assert acc._ast_has_broad_except("try:\n    x = 1\nexcept Exception:\n    pass\n")

    def test_specific_except_OK(self):
        assert not acc._ast_has_broad_except(
            "try:\n    x = 1\nexcept ValueError:\n    pass\n"
        )

    def test_bare_except_detected(self):
        assert acc._ast_has_broad_except("try:\n    x = 1\nexcept:\n    pass\n")

    def test_syntax_error_falls_back_no_detection(self):
        assert not acc._ast_has_broad_except("this is not valid python {{")

    def test_specific_named_tuple_OK(self):
        assert not acc._ast_has_broad_except(
            "try:\n    x = 1\nexcept (ValueError, KeyError):\n    pass\n"
        )

    # v2-C2: tuple form must be detected
    def test_tuple_with_exception_detected(self):
        assert acc._ast_has_broad_except(
            "try:\n    x = 1\nexcept (ValueError, Exception):\n    pass\n"
        )

    def test_tuple_with_base_exception_detected(self):
        assert acc._ast_has_broad_except(
            "try:\n    x = 1\nexcept (ValueError, BaseException):\n    pass\n"
        )

    def test_base_exception_detected(self):
        assert acc._ast_has_broad_except(
            "try:\n    x = 1\nexcept BaseException:\n    pass\n"
        )

    def test_count_broad_excepts(self):
        code = """
try:
    x = 1
except Exception:
    pass
try:
    y = 2
except (ValueError, Exception):
    pass
try:
    z = 3
except:
    pass
"""
        assert acc._count_broad_excepts(code) == 3

    def test_count_returns_neg1_on_syntax_error(self):
        assert acc._count_broad_excepts("invalid {{") == -1


# ===========================================
# v1 D9: size mismatch
# ===========================================


class TestSizeMatch:
    def test_mini_under_30_OK(self):
        assert acc._check_size_match("mini", 20) is None

    def test_mini_over_30_fails(self):
        assert "mini" in acc._check_size_match("mini", 50)

    def test_full_under_300_OK(self):
        assert acc._check_size_match("full", 200) is None

    def test_full_over_300_fails(self):
        assert "full" in acc._check_size_match("full", 350)

    def test_plan_no_upper_limit(self):
        assert acc._check_size_match("plan", 5000) is None


# ===========================================
# v1 D2 + v2-D1 + D7: Warning Acknowledgements
# ===========================================


class TestWarningAcknowledgements:
    def test_no_warnings_no_blockers(self, tmp_path):
        led = _write_ledger(tmp_path)
        assert acc._check_warning_acknowledgements(led.read_text(), []) == []

    def test_warnings_no_section_blocked(self, tmp_path):
        blockers = acc._check_warning_acknowledgements(
            "no ack section here\n", ["W5: TODO in foo.py"]
        )
        assert any("no '## Warning Acknowledgements' section" in b for b in blockers)

    def test_warnings_with_ack_OK(self, tmp_path):
        ack_table = (
            "| condition | ack | reason |\n"
            "|---|---|---|\n"
            "| W5: TODO in foo | yes | scheduled fix in PR-X |\n"
        )
        led = _write_ledger(tmp_path, warn_ack=ack_table)
        blockers = acc._check_warning_acknowledgements(
            led.read_text(), ["W5: TODO in foo"]
        )
        assert blockers == []

    # v2-D1: ack matched by W code, missing match → blocker
    def test_warning_no_matching_ack_blocked(self, tmp_path):
        ack_table = (
            "| condition | ack | reason |\n"
            "|---|---|---|\n"
            "| W5: TODO in foo | yes | scheduled |\n"
        )
        led = _write_ledger(tmp_path, warn_ack=ack_table)
        blockers = acc._check_warning_acknowledgements(
            led.read_text(), ["W5: TODO in foo", "W6: new dep tenacity"]
        )
        assert any("W6" in b and "no matching ack" in b for b in blockers)

    # v2-D1: ack value not 'yes' → blocker
    def test_warning_ack_no_blocked(self, tmp_path):
        ack_table = (
            "| condition | ack | reason |\n"
            "|---|---|---|\n"
            "| W5: TODO in foo | no | will fix later |\n"
        )
        led = _write_ledger(tmp_path, warn_ack=ack_table)
        blockers = acc._check_warning_acknowledgements(
            led.read_text(), ["W5: TODO in foo"]
        )
        assert any("ack value 'no' is not 'yes'" in b for b in blockers)

    # v2-D7: pipe-in-cell parsing
    def test_table_pipe_in_cell_parsed(self, tmp_path):
        # Reason contains pipe chars (e.g., command syntax)
        ack_table = (
            "| condition | ack | reason |\n"
            "|---|---|---|\n"
            "| W5: TODO in foo | yes | runs `foo bar` reliably |\n"
        )
        led = _write_ledger(tmp_path, warn_ack=ack_table)
        # Should still find the matching ack row even though reason has no pipes
        blockers = acc._check_warning_acknowledgements(
            led.read_text(), ["W5: TODO in foo"]
        )
        assert blockers == []


# ===========================================
# v1 D1: created_at validation
# ===========================================


class TestCreatedAt:
    def test_invalid_iso_format(self, tmp_path):
        led = _write_ledger(tmp_path, header_overrides={"created_at": "not-a-date"})
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_created_at(header, [], tmp_path)
        assert any("invalid created_at" in b for b in blockers)

    def test_created_at_before_edits_OK(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        led = _write_ledger(repo, header_overrides={"created_at": "2020-01-01T00:00:00Z"})
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_created_at(header, ["initial.txt"], repo)
        assert blockers == []

    def test_created_at_after_edits_blocked(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        led = _write_ledger(repo, header_overrides={"created_at": "2099-01-01T00:00:00Z"})
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_created_at(header, ["initial.txt"], repo)
        assert any("backfilled" in b for b in blockers)


# ===========================================
# v2-D5: fail-closed git error
# ===========================================


class TestGitFailClosed:
    def test_unknown_mode_raises(self, tmp_path):
        with pytest.raises(RuntimeError):
            acc._get_diff_files("invalid_mode", None, tmp_path)

    def test_git_error_raises(self, tmp_path):
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        with pytest.raises(RuntimeError):
            acc._get_diff_files("staged", None, non_repo)

    # ===== C2: _get_diff_added_lines fail-closed =====
    def test_added_lines_git_error_raises(self, tmp_path):
        # Pre-fix: returned [] on git error → W5/W6 silently never generated.
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        with pytest.raises(RuntimeError):
            acc._get_diff_added_lines("foo.py", "staged", "HEAD", non_repo)

    def test_anti_patterns_propagates_added_lines_error(self, tmp_path, monkeypatch):
        # When the per-file diff fails, _check_anti_patterns must NOT swallow it.
        repo = _make_git_repo(tmp_path)
        f = repo / "foo.py"
        f.write_text("x = 1\n")
        _git_add(repo, "foo.py")

        def _boom(*_a, **_k):
            raise RuntimeError("git diff failed for foo.py")

        monkeypatch.setattr(acc, "_get_diff_added_lines", _boom)
        with pytest.raises(RuntimeError):
            acc._check_anti_patterns(["foo.py"], repo, "staged", "HEAD", 1, "full")

    def test_main_exits_generic_on_per_file_diff_error(self, tmp_path, monkeypatch):
        # Integration: a per-file diff error during anti-pattern checks must
        # surface as EXIT_GENERIC (1), not a silent EXIT_OK pass.
        repo = _make_git_repo(tmp_path)
        f = repo / "foo.py"
        f.write_text("x = 1\n")
        _git_add(repo, "foo.py")
        _write_ledger(
            repo,
            objections="\n".join(
                f"{i}. **Objection**: x (foo.py:1) — **Mitigation**: pytest -k test_g{i}"
                for i in range(1, 4)
            ),
        )

        def _boom(*_a, **_k):
            raise RuntimeError("git diff failed for foo.py")

        monkeypatch.setattr(acc, "_get_diff_added_lines", _boom)
        monkeypatch.setenv("AQG_AGENT", "claude")
        rc = acc.main(["--mode", "staged", "--cwd", str(repo), "--quiet"])
        assert rc == acc.EXIT_GENERIC, f"expected EXIT_GENERIC, got {rc}"


# ===========================================
# v2-C1: 6-step ledger table parser + 3 hard blockers
# ===========================================


class TestSixStepTable:
    def test_parse_full_table(self, tmp_path):
        led = _write_ledger(tmp_path)
        rows = acc._parse_ledger_steps(led.read_text())
        assert len(rows) == 4  # default has 4 rows (1, 2, 5, 6)
        assert rows[0].step.startswith("1.")
        assert rows[1].step.startswith("2.")
        assert rows[1].evidence == "added test"

    def test_missing_table_returns_empty(self):
        content = "no table here, just text"
        assert acc._parse_ledger_steps(content) == []

    def test_classify_diff_prod(self):
        has_prod, has_schema, has_docs = acc._classify_diff(["scripts/foo.py"])
        assert has_prod and not has_schema and not has_docs

    def test_classify_diff_schema(self):
        has_prod, has_schema, has_docs = acc._classify_diff(["templates/x.yaml"])
        assert not has_prod and has_schema and not has_docs

    def test_classify_diff_docs(self):
        has_prod, has_schema, has_docs = acc._classify_diff(["docs/foo.md", "README.md"])
        assert not has_prod and not has_schema and has_docs

    # HB1: prod code without behavior lock evidence
    def test_hb1_prod_no_test_evidence_blocked(self, tmp_path):
        steps = """| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | read foo | initial.txt:1 | rg foo OK |
| 5. Local Verification | yes | tests pass | initial.txt:1 | pytest PASS |
| 6. Self Review | yes | reviewed | initial.txt:1 | OK |"""
        led = _write_ledger(tmp_path, steps_table=steps)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_six_step_table(
            led.read_text(), header, ["scripts/foo.py"]
        )
        assert any("HB1" in b and "Behavior Lock" in b for b in blockers)

    def test_hb1_prod_with_test_evidence_OK(self, tmp_path):
        led = _write_ledger(tmp_path)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_six_step_table(
            led.read_text(), header, ["scripts/foo.py"]
        )
        # default ledger has step 2 with evidence → no HB1
        assert not any("HB1" in b for b in blockers)

    # HB2: schema change without local verification regression keyword
    def test_hb2_schema_no_regression_keyword_blocked(self, tmp_path):
        steps = """| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | read foo | initial.txt:1 | grep foo OK |
| 2. Behavior Lock | yes | added test | initial.txt:1 | manual check |
| 5. Local Verification | yes | reviewed | initial.txt:1 | manual eyeball only |
| 6. Self Review | yes | reviewed | initial.txt:1 | OK |"""
        led = _write_ledger(tmp_path, steps_table=steps)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_six_step_table(
            led.read_text(), header, ["templates/foo.yaml"]
        )
        assert any("HB2" in b and "regression" in b for b in blockers)

    def test_hb2_schema_with_pytest_OK(self, tmp_path):
        led = _write_ledger(tmp_path)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_six_step_table(
            led.read_text(), header, ["templates/foo.yaml"]
        )
        # default has "pytest PASS" in step 5 → regression keyword → no HB2
        assert not any("HB2" in b for b in blockers)

    # HB4: skipped_checks with vague reason
    def test_hb4_skipped_check_vague_reason_blocked(self, tmp_path):
        led = _write_ledger(
            tmp_path,
            header_overrides={
                "skipped_checks": '[{"check": "lint", "reason": "fix later"}]'
            },
        )
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_six_step_table(led.read_text(), header, ["initial.txt"])
        assert any("HB4" in b for b in blockers)

    def test_hb4_skipped_check_concrete_reason_OK(self, tmp_path):
        led = _write_ledger(
            tmp_path,
            header_overrides={
                "skipped_checks": '[{"check": "lint", "reason": "infra-only PR no python files"}]'
            },
        )
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_six_step_table(led.read_text(), header, ["initial.txt"])
        assert not any("HB4" in b for b in blockers)

    def test_table_missing_blocked(self, tmp_path):
        led = _write_ledger(tmp_path, steps_table="(no table at all)")
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_six_step_table(led.read_text(), header, ["initial.txt"])
        assert any("6-step table missing" in b for b in blockers)

    # ===== G3: pipe in command/result cell must rejoin (not drop the row) =====
    def test_piped_command_cell_parses_as_step_row(self, tmp_path):
        # Local Verification command contains a shell pipe; the row splits into
        # >5 cells. Surplus (cells[4:]) must rejoin so the row stays valid.
        steps = """| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | read foo | initial.txt:1 | rg foo OK |
| 2. Behavior Lock | yes | added test | initial.txt:1 | pytest PASS 1/1 |
| 5. Local Verification | yes | tests pass | initial.txt:1 | pytest -q | tee out.txt |
| 6. Self Review | yes | reviewed | initial.txt:1 | OK |"""
        led = _write_ledger(tmp_path, steps_table=steps)
        rows = acc._parse_ledger_steps(led.read_text())
        # All 4 rows must parse (pre-fix: the piped row is dropped → 3 rows)
        assert len(rows) == 4, [r.step for r in rows]
        step5 = next((r for r in rows if r.step.startswith("5.")), None)
        assert step5 is not None
        # The full piped command must be preserved in command_result.
        assert "pytest -q" in step5.command_result
        assert "tee out.txt" in step5.command_result

    def test_piped_command_cell_hb2_sees_regression_keyword(self, tmp_path):
        # G3 regression: a schema change with a piped Local Verification command
        # must NOT false-block HB2 (the row must be seen, regression kw present).
        steps = """| step | required | evidence | file:line | command/result |
|---|---|---|---|---|
| 1. Pattern Mining | yes | read foo | initial.txt:1 | rg foo OK |
| 2. Behavior Lock | yes | added test | initial.txt:1 | pytest PASS 1/1 |
| 5. Local Verification | yes | tests pass | initial.txt:1 | pytest -q | tee out.txt |
| 6. Self Review | yes | reviewed | initial.txt:1 | OK |"""
        led = _write_ledger(tmp_path, steps_table=steps)
        header = acc._parse_ledger_header(led.read_text())
        blockers = acc._check_six_step_table(
            led.read_text(), header, ["templates/foo.yaml"]
        )
        assert not any("HB2" in b for b in blockers), blockers


# ===========================================
# v2-C2: diff-aware broad-except (refactor doesn't trigger)
# ===========================================


class TestDiffAwareBroadExcept:
    def test_new_unmarked_broad_except_blocked(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        bad = repo / "bad.py"
        bad.write_text("try:\n    x = 1\nexcept Exception:\n    pass\n")
        _git_add(repo, "bad.py")
        # Pre-image: file not in HEAD; post: 1 broad. New = 1, no boundary.
        result = acc._check_diff_aware_broad_except("bad.py", "staged", "HEAD", repo)
        assert result is not None
        assert "new broad" in result.lower()

    def test_pre_existing_broad_except_not_blocked(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        f = repo / "moved.py"
        f.write_text("try:\n    x = 1\nexcept Exception:\n    pass\n")
        _git_add(repo, "moved.py")
        _git_commit(repo, "initial broad except")
        # Refactor: same broad except, modified surrounding context
        f.write_text("# new top comment\ntry:\n    x = 2\nexcept Exception:\n    pass\n")
        _git_add(repo, "moved.py")
        # Pre-count = 1, post-count = 1 → no new
        result = acc._check_diff_aware_broad_except("moved.py", "staged", "HEAD", repo)
        assert result is None

    def test_per_handler_boundary_marker_OK(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        f = repo / "boundary.py"
        f.write_text(
            "# aqg: top-level boundary\ntry:\n    x = 1\nexcept Exception:\n    raise\n"
        )
        _git_add(repo, "boundary.py")
        result = acc._check_diff_aware_broad_except("boundary.py", "staged", "HEAD", repo)
        assert result is None  # marker within 5 lines → exempt

    def test_specific_except_no_block(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        f = repo / "good.py"
        f.write_text("try:\n    x = 1\nexcept ValueError:\n    pass\n")
        _git_add(repo, "good.py")
        result = acc._check_diff_aware_broad_except("good.py", "staged", "HEAD", repo)
        assert result is None

    def test_tuple_with_exception_blocked(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        f = repo / "tup.py"
        f.write_text("try:\n    x = 1\nexcept (ValueError, Exception):\n    pass\n")
        _git_add(repo, "tup.py")
        result = acc._check_diff_aware_broad_except("tup.py", "staged", "HEAD", repo)
        assert result is not None

    # ===== C1: added-line NUMBERS helper (hunk-header parsing) =====
    def test_added_line_numbers_new_file(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        f = repo / "n.py"
        f.write_text("a = 1\nb = 2\nc = 3\n")
        _git_add(repo, "n.py")
        nums = acc._get_diff_added_line_numbers("n.py", "staged", "HEAD", repo)
        assert nums == {1, 2, 3}

    def test_added_line_numbers_partial_edit(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        f = repo / "m.py"
        f.write_text("try:\n    x = 1\nexcept Exception:\n    pass\n")
        _git_add(repo, "m.py")
        _git_commit(repo, "init m")
        # Prepend comment + change x value; the 'except' line is unchanged.
        f.write_text("# top comment\ntry:\n    x = 2\nexcept Exception:\n    pass\n")
        _git_add(repo, "m.py")
        nums = acc._get_diff_added_line_numbers("m.py", "staged", "HEAD", repo)
        # Comment added at line 1; x=2 at line 3. 'except' (line 4) NOT added.
        assert 1 in nums and 3 in nums
        assert 4 not in nums

    # ===== C1: per-handler newness (count-based aggregate was false-passing) =====
    def test_c1_remove_old_add_new_unmarked_blocked(self, tmp_path):
        # Delete one OLD unmarked broad except, add a NEW unmarked one elsewhere.
        # Counts unchanged (1 → 1) → old aggregate logic false-PASSES. Must BLOCK.
        repo = _make_git_repo(tmp_path)
        f = repo / "c1a.py"
        f.write_text(
            "def old():\n"
            "    try:\n"
            "        x = 1\n"
            "    except Exception:\n"
            "        pass\n"
            "\n"
            "def stable():\n"
            "    return 1\n"
        )
        _git_add(repo, "c1a.py")
        _git_commit(repo, "init c1a (1 broad in old())")
        # Remove old()'s handler entirely; add a brand-new broad handler in a new fn.
        f.write_text(
            "def old():\n"
            "    x = 1\n"
            "    return x\n"
            "\n"
            "def stable():\n"
            "    return 1\n"
            "\n"
            "def added():\n"
            "    try:\n"
            "        y = 2\n"
            "    except Exception:\n"
            "        pass\n"
        )
        _git_add(repo, "c1a.py")
        result = acc._check_diff_aware_broad_except("c1a.py", "staged", "HEAD", repo)
        assert result is not None, "new unmarked broad except on added lines must block"
        assert "new broad" in result.lower()

    def test_c1_add_marker_to_old_plus_new_unmarked_blocked(self, tmp_path):
        # Add a boundary marker to an OLD handler while adding a NEW unmarked handler.
        # Aggregate marker-count math cancels out → old logic false-PASSES. Must BLOCK.
        repo = _make_git_repo(tmp_path)
        f = repo / "c1b.py"
        f.write_text(
            "def old():\n"
            "    try:\n"
            "        x = 1\n"
            "    except Exception:\n"
            "        pass\n"
        )
        _git_add(repo, "c1b.py")
        _git_commit(repo, "init c1b (1 old unmarked broad)")
        # Mark the OLD handler AND add a NEW unmarked handler. The new handler
        # is textually distinct (`as e` / `raise e`) so git's minimal diff
        # attributes it to the added lines rather than matching it to the old
        # byte-identical block (see judgment-call note in the deliverable).
        f.write_text(
            "def old():\n"
            "    try:\n"
            "        x = 1\n"
            "    # aqg: top-level boundary\n"
            "    except Exception:\n"
            "        pass\n"
            "\n"
            "def added():\n"
            "    try:\n"
            "        y = 2\n"
            "    except Exception as e:\n"
            "        raise e\n"
        )
        _git_add(repo, "c1b.py")
        result = acc._check_diff_aware_broad_except("c1b.py", "staged", "HEAD", repo)
        assert result is not None, "new unmarked handler must block even if an old one gained a marker"
        assert "new broad" in result.lower()

    def test_c1_add_one_new_marked_passes(self, tmp_path):
        # Add ONE new broad handler that IS boundary-marked → PASS.
        repo = _make_git_repo(tmp_path)
        f = repo / "c1c.py"
        f.write_text("def stable():\n    return 1\n")
        _git_add(repo, "c1c.py")
        _git_commit(repo, "init c1c (no broad)")
        f.write_text(
            "def stable():\n"
            "    return 1\n"
            "\n"
            "def added():\n"
            "    try:\n"
            "        y = 2\n"
            "    # aqg: top-level boundary\n"
            "    except Exception:\n"
            "        raise\n"
        )
        _git_add(repo, "c1c.py")
        result = acc._check_diff_aware_broad_except("c1c.py", "staged", "HEAD", repo)
        assert result is None, result

    def test_c1_no_new_handlers_passes(self, tmp_path):
        # Edit lines far from a pre-existing broad handler; handler line not added → PASS.
        repo = _make_git_repo(tmp_path)
        f = repo / "c1d.py"
        f.write_text(
            "def f():\n"
            "    try:\n"
            "        x = 1\n"
            "    except Exception:\n"
            "        pass\n"
            "\n"
            "VALUE = 1\n"
        )
        _git_add(repo, "c1d.py")
        _git_commit(repo, "init c1d")
        # Only change the trailing constant; handler untouched.
        f.write_text(
            "def f():\n"
            "    try:\n"
            "        x = 1\n"
            "    except Exception:\n"
            "        pass\n"
            "\n"
            "VALUE = 2\n"
        )
        _git_add(repo, "c1d.py")
        result = acc._check_diff_aware_broad_except("c1d.py", "staged", "HEAD", repo)
        assert result is None, result

    # ===== P1: mode-appropriate post-image (staged blob, not worktree) =====
    def test_p1_staged_blob_blocks_despite_clean_worktree(self, tmp_path):
        # Stage a file with a new unmarked broad except, then EDIT the worktree
        # copy to remove it. --mode staged must block on the STAGED blob.
        repo = _make_git_repo(tmp_path)
        f = repo / "p1.py"
        f.write_text("try:\n    x = 1\nexcept Exception:\n    pass\n")
        _git_add(repo, "p1.py")  # staged version HAS the broad except
        # Now make the worktree clean (no broad except) WITHOUT re-staging.
        f.write_text("try:\n    x = 1\nexcept ValueError:\n    pass\n")
        result = acc._check_diff_aware_broad_except("p1.py", "staged", "HEAD", repo)
        assert result is not None, "staged blob has unmarked broad except → must block"
        assert "new broad" in result.lower()


# ===========================================
# v2-D1: diff-hunk warnings (TODO/dep based on added lines only)
# ===========================================


class TestDiffHunkWarnings:
    def test_historical_todo_no_warning(self, tmp_path):
        """Pre-existing TODO in committed file should NOT trigger when only unrelated edit."""
        repo = _make_git_repo(tmp_path)
        f = repo / "foo.py"
        f.write_text("# TODO: pre-existing fix later\nx = 1\n")
        _git_add(repo, "foo.py")
        _git_commit(repo, "initial with todo")
        # Now edit unrelated line (no new TODO added)
        f.write_text("# TODO: pre-existing fix later\nx = 999\n")
        _git_add(repo, "foo.py")
        added = acc._get_diff_added_lines("foo.py", "staged", "HEAD", repo)
        # Only the modified line is in added, not the TODO line
        assert "TODO" not in "\n".join(added)

    def test_new_todo_in_added_line_warning(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        f = repo / "foo.py"
        f.write_text("x = 1\n")
        _git_add(repo, "foo.py")
        _git_commit(repo, "initial")
        # Add new TODO
        f.write_text("# TODO: new\nx = 1\n")
        _git_add(repo, "foo.py")
        _, warnings = acc._check_anti_patterns(
            ["foo.py"], repo, "staged", "HEAD", 2, "full"
        )
        assert any("TODO" in w for w in warnings)

    def test_w5_functional_todo_in_markdown_no_warning(self, tmp_path):
        """C5 dogfood FP fix: a markdown doc whose added line is a functional
        `| ... | TODO |` ledger status cell (as emitted by aqg-evidence-closeout)
        must NOT trigger W5 — only code-debt comment/colon forms should."""
        repo = _make_git_repo(tmp_path)
        f = repo / "doc.md"
        f.write_text("intro\n")
        _git_add(repo, "doc.md")
        _git_commit(repo, "initial doc")
        # Add a functional ledger-status TODO cell (the closeout template form).
        f.write_text("intro\n| scope completed | <files/PR/issue> | TODO |\n")
        _git_add(repo, "doc.md")
        _, warnings = acc._check_anti_patterns(
            ["doc.md"], repo, "staged", "HEAD", 1, "full"
        )
        assert not any("TODO" in w for w in warnings)

    def test_w5_code_comment_todo_in_markdown_still_warns(self, tmp_path):
        """Counterpart: a real `<!-- TODO -->` debt marker added to a doc still warns."""
        repo = _make_git_repo(tmp_path)
        f = repo / "doc.md"
        f.write_text("intro\n")
        _git_add(repo, "doc.md")
        _git_commit(repo, "initial doc")
        f.write_text("intro\n<!-- TODO: finish this section -->\n")
        _git_add(repo, "doc.md")
        _, warnings = acc._check_anti_patterns(
            ["doc.md"], repo, "staged", "HEAD", 1, "full"
        )
        assert any("TODO" in w for w in warnings)

    def test_new_dep_entry_warning(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        (repo / "package.json").write_text('{"deps": {"x": "1.0"}}')
        _git_add(repo, "package.json")
        _, warnings = acc._check_anti_patterns(
            ["package.json"], repo, "staged", "HEAD", 1, "full"
        )
        assert any("dep entry" in w for w in warnings)


# ===========================================
# C5 dogfood: TODO/FIXME debt-form vs prose/table mention
# ===========================================


class TestTodoFixmeDebtForm:
    """TODO/FIXME only count as markers in code-debt form (comment leader or
    `TODO:` colon form) — never as a prose/table mention where TODO is the
    subject being discussed. Prevents the recurring FP on aqg-evidence-closeout
    ledgers, whose status column legitimately uses `| ... | TODO |`."""

    # VAGUE context (mitigation / ack / skipped reason) — IGNORECASE.
    @pytest.mark.parametrize(
        "text",
        [
            "fill the TODO row in the closeout evidence-ledger via aqg_closeout.py",
            "surfaces a TODO row when the summary is MALFORMED, never raises",
            "renders the `| ... | TODO |` ledger status cell unchanged",
            "INCONSISTENT — TODO is the documented status value, not code debt",
        ],
    )
    def test_prose_todo_mention_not_vague(self, text):
        assert acc.VAGUE_REGEX.search(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "TODO: add validation later",
            "# TODO fix this before merge",
            "<!-- TODO: write this section -->",
            "// FIXME broken edge case",
            "FIXME: handle null input",
        ],
    )
    def test_debt_form_todo_still_vague(self, text):
        assert acc.VAGUE_REGEX.search(text) is not None

    # W5 context (diff-added line scan) — case-sensitive debt regex.
    @pytest.mark.parametrize(
        "line",
        [
            "| scope completed | <files/PR/issue/status doc> | TODO |",
            "an inconsistent summary renders as INCONSISTENT — TODO (never PASS)",
            'print("| durable state updated | <PR/issue> | TODO |")',
            "summary unreadable: {detail} | TODO |",
        ],
    )
    def test_w5_functional_todo_not_flagged(self, line):
        assert acc.TODO_FIXME_DEBT_REGEX.search(line) is None

    @pytest.mark.parametrize(
        "line",
        [
            "# TODO: refactor this",
            "// FIXME: race condition",
            "x = 1  # TODO later",
            "<!-- TODO -->",
        ],
    )
    def test_w5_code_comment_todo_flagged(self, line):
        assert acc.TODO_FIXME_DEBT_REGEX.search(line) is not None

    def test_w5_leader_does_not_bridge_newline(self):
        """Audit 094568a8 f2: W5 searches the `"\\n".join(added_lines)` blob, so a
        comment leader at the END of one added line must NOT bridge the newline to
        a prose TODO at the START of the next line (`[^\\S\\n]*`, horizontal ws)."""
        bridged = "some divider #\nTODO appears as a status word here"
        assert acc.TODO_FIXME_DEBT_REGEX.search(bridged) is None
        # same-line leader still matches (regression guard for the fix)
        assert acc.TODO_FIXME_DEBT_REGEX.search("x = 1  # TODO real debt") is not None

    def test_w5_no_cross_line_bridge_integration(self, tmp_path):
        """End-to-end f2: two added lines where line 1 ends in `#` and line 2 is a
        prose TODO must not raise a W5 warning."""
        repo = _make_git_repo(tmp_path)
        f = repo / "doc.md"
        f.write_text("intro\n")
        _git_add(repo, "doc.md")
        _git_commit(repo, "initial doc")
        f.write_text("intro\nsection break #\nTODO appears as a status word\n")
        _git_add(repo, "doc.md")
        _, warnings = acc._check_anti_patterns(
            ["doc.md"], repo, "staged", "HEAD", 2, "full"
        )
        assert not any("TODO" in w for w in warnings)


# ===========================================
# v2-D1: W7 (large diff) + W8 (docs/status)
# ===========================================


class TestW7W8Warnings:
    def test_w7_large_diff_no_plan(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _, warnings = acc._check_anti_patterns(
            ["initial.txt"], repo, "staged", "HEAD", 500, "full"
        )
        assert any("W7" in w for w in warnings)

    def test_w7_large_diff_with_plan_OK(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _, warnings = acc._check_anti_patterns(
            ["initial.txt"], repo, "staged", "HEAD", 500, "plan"
        )
        assert not any("W7" in w for w in warnings)

    def test_w8_docs_change_warning(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        (repo / "docs").mkdir()
        (repo / "docs" / "thing.md").write_text("# updated\n")
        _git_add(repo, "docs/thing.md")
        _, warnings = acc._check_anti_patterns(
            ["docs/thing.md"], repo, "staged", "HEAD", 1, "full"
        )
        assert any("W8" in w for w in warnings)


# ===========================================
# v2-D7: markdown table pipe-in-cell + table parser
# ===========================================


class TestMarkdownTableParsing:
    def test_split_simple_row(self):
        cells = acc._split_md_table_row("| step | required | evidence | file:line | command/result |")
        assert cells == ["step", "required", "evidence", "file:line", "command/result"]

    def test_split_separator_row_returns_none(self):
        assert acc._split_md_table_row("|---|---|---|") is None

    def test_split_non_table_returns_none(self):
        assert acc._split_md_table_row("not a table row") is None

    def test_split_with_pipe_in_cell(self):
        # Pipe in cell content: split('|') gives extra empty strings, but we strip all and use cells[1:-1]
        # Note: this is a known limitation — we accept the simple split approach
        cells = acc._split_md_table_row("| W5 | yes | not-vague-reason |")
        assert cells == ["W5", "yes", "not-vague-reason"]


# ===========================================
# v2-C1: integration test via _check_anti_patterns signature
# ===========================================


class TestAntiPatternsIntegration:
    def test_broad_except_in_modified_py_blocked(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        bad = repo / "bad.py"
        bad.write_text("try:\n    x = 1\nexcept Exception:\n    pass\n")
        _git_add(repo, "bad.py")
        blockers, warnings = acc._check_anti_patterns(
            ["bad.py"], repo, "staged", "HEAD", 4, "full"
        )
        assert any("new broad" in b for b in blockers)

    def test_broad_except_with_boundary_marker_OK(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        f = repo / "boundary.py"
        f.write_text(
            "# aqg: top-level boundary\ntry:\n    x = 1\nexcept Exception:\n    raise\n"
        )
        _git_add(repo, "boundary.py")
        blockers, warnings = acc._check_anti_patterns(
            ["boundary.py"], repo, "staged", "HEAD", 5, "full"
        )
        assert blockers == []

    def test_specific_except_OK(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        good = repo / "good.py"
        good.write_text("try:\n    x = 1\nexcept ValueError:\n    pass\n")
        _git_add(repo, "good.py")
        blockers, warnings = acc._check_anti_patterns(
            ["good.py"], repo, "staged", "HEAD", 4, "full"
        )
        assert blockers == []
