"""Tests for scripts/aqg_doctor.py session-mode integration.

每个 test 标注三审 (audit_id 2e5407d6) 对应的 accepted finding。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "aqg_doctor.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import aqg_doctor  # noqa: E402


# 0 = every check passed, 1 = at least one FAIL. Both are normal completions and
# depend on the HOST's health, not on doctor's mode handling; only 2 (usage error)
# or a crash would be a regression in what these tests actually cover. Asserting
# rc == 0 quietly turned each of them into "this machine has zero doctor FAILs",
# which broke them the moment doctor learned to report an unset AQG_ROOT
# (hook_env_guard). That behaviour is covered in
# tests/behavior/test_doctor_delivery_channels.py.
COMPLETED_OK = (0, 1)


def run_doctor(*args: str) -> tuple[int, str, str]:
    """Run doctor as subprocess; return (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(DOCTOR), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ============================================================
# Backward compat: install mode unchanged (default)
# ============================================================


class TestInstallModeBackwardCompat:
    def test_default_mode_is_install(self):
        """三审 o3 #4 rejected: 不加 --mode 参数仍是 install 行为。"""
        rc, stdout, _ = run_doctor("--no-cli", "--json")
        assert rc in COMPLETED_OK
        data = json.loads(stdout)
        assert data["mode"] == "install"
        # 没 fingerprint
        assert "session_fingerprint" not in data

    def test_install_mode_explicit(self):
        rc, stdout, _ = run_doctor("--no-cli", "--mode", "install", "--json")
        assert rc in COMPLETED_OK
        data = json.loads(stdout)
        assert data["mode"] == "install"
        assert "session_fingerprint" not in data

    def test_install_mode_text_output_unchanged_format(self):
        rc, stdout, _ = run_doctor("--no-cli")
        assert rc in COMPLETED_OK
        # existing install mode doesn't print the "## Session fingerprint" section
        assert "Session fingerprint" not in stdout
        # Summary 行仍存在
        assert "Summary: PASS=" in stdout

    def test_run_install_mode_helper_returns_same_results(self):
        """o3 #1 verification: _run_install_mode helper 输出 = main([--no-cli]) 输出."""
        # 直接调 helper
        from argparse import Namespace

        args = Namespace(
            json=False,
            no_cli=True,
            mode="install",
            claude_skills_dir=str(Path.home() / ".claude" / "skills"),
            codex_skills_dir=str(Path.home() / ".codex" / "skills"),
        )
        results, _ = aqg_doctor._run_install_mode(args)
        # 对比 subprocess 跑出来的 result count
        rc, stdout, _ = run_doctor("--no-cli", "--json")
        data = json.loads(stdout)
        assert len(results) == len(data["results"])
        # 名字对得上
        helper_names = [r.name for r in results]
        sub_names = [r["name"] for r in data["results"]]
        assert helper_names == sub_names


# ============================================================
# Mode flag: not-implemented modes exit with usage error
# ============================================================


class TestModeFlag:
    @pytest.mark.parametrize("mode", ["commit", "push", "pr"])
    def test_not_implemented_modes_exit_usage(self, mode):
        rc, _, stderr = run_doctor("--no-cli", "--mode", mode)
        assert rc == aqg_doctor.EXIT_USAGE
        assert mode in stderr
        assert "not implemented" in stderr.lower()

    def test_unknown_mode_argparse_error(self):
        # argparse choices=  → 自动报 error 并 exit 2
        rc, _, stderr = run_doctor("--no-cli", "--mode", "garbage")
        assert rc != 0
        assert "garbage" in stderr or "invalid choice" in stderr

    def test_session_mode_exit_zero_on_clean(self):
        rc, stdout, _ = run_doctor("--no-cli", "--mode", "session", "--json")
        # 即使有 WARN (gh not 装) 也 exit 0; 只 FAIL 才非 0
        # session 收 fingerprint 跑 redaction guard, 当前 dev box 应 PASS
        data = json.loads(stdout)
        if data["ok"]:
            assert rc in COMPLETED_OK
        else:
            assert rc == 1


# ============================================================
# Session mode: fingerprint shape + redact_install_details
# ============================================================


class TestSessionMode:
    def test_session_mode_json_includes_fingerprint(self):
        rc, stdout, _ = run_doctor("--no-cli", "--mode", "session", "--json")
        assert rc in (0, 1)
        data = json.loads(stdout)
        assert data["mode"] == "session"
        assert "session_fingerprint" in data
        fp = data["session_fingerprint"]
        # 关键 surface 在
        assert "claude_md" in fp
        assert "model_default_status" in fp
        assert "sandbox_status" in fp

    def test_session_mode_text_includes_fingerprint_section(self):
        rc, stdout, _ = run_doctor("--no-cli", "--mode", "session")
        assert rc in (0, 1)
        assert "## Session fingerprint" in stdout
        # 各 surface 行
        assert "claude_md:" in stdout
        assert "model_default_status:" in stdout

    def test_session_mode_install_results_have_paths_redacted(self):
        """三审 gpt-5.5 #2 accepted: session output 不含 raw $HOME / $CWD path."""
        rc, stdout, _ = run_doctor("--no-cli", "--mode", "session", "--json")
        data = json.loads(stdout)
        # 当前 home 真实 path 不应在 install results detail 里出现
        home = str(Path.home())
        cwd = str(Path.cwd())
        for r in data["results"]:
            detail = r.get("detail") or ""
            fix = r.get("fix") or ""
            assert home not in detail, f"home leak in detail: {detail}"
            assert home not in fix, f"home leak in fix: {fix}"
            # cwd 也不应出现 (替换为 <CWD>)
            assert cwd not in detail, f"cwd leak in detail: {detail}"
        # 反向：detail 应含 <HOME> 占位（至少一个 install check 含 home path）
        all_details = " ".join(r.get("detail") or "" for r in data["results"])
        assert "<HOME>" in all_details or "<CWD>" in all_details

    def test_install_mode_install_results_keep_paths(self):
        """对照组：install mode 不脱敏 (backward compat)。"""
        rc, stdout, _ = run_doctor("--no-cli", "--mode", "install", "--json")
        data = json.loads(stdout)
        all_details = " ".join(r.get("detail") or "" for r in data["results"])
        # 至少应该有 path 字串残留 (没脱敏)
        assert "<HOME>" not in all_details

    def test_session_mode_no_cli_skips_cli_probes(self):
        """gpt-5.5 #4: --no-cli ⇒ git/gh/bash version 是 None."""
        rc, stdout, _ = run_doctor("--no-cli", "--mode", "session", "--json")
        data = json.loads(stdout)
        fp = data["session_fingerprint"]
        assert fp["git_version"] is None
        assert fp["gh_version"] is None
        assert fp["bash_version"] is None
        # python3_version 仍跑（用 sys.version_info, not subprocess）
        assert fp["python3_version"] is not None
        assert fp["gh_auth"] == {"logged_in": False}


# ============================================================
# Violation 输出层 sanitization (gpt-5.5 #1)
# ============================================================


class TestViolationOutputSanitization:
    def test_extract_field_from_violation_strips_value(self):
        """gpt-5.5 #1: doctor 不直接 emit raw value snippet."""
        # 模拟 _surface_redaction violation 字串 (含 raw value)
        fake_violation = "claude_md.host: scheme not allowed (got '/etc/passwd')"
        field = aqg_doctor._extract_field_from_violation(fake_violation)
        assert field == "claude_md.host"
        # 抽出来的不该含 raw value
        assert "/etc/passwd" not in field
        assert "scheme" not in field

    def test_extract_field_no_colon(self):
        # 没冒号 fallback
        assert aqg_doctor._extract_field_from_violation("just a thing") == "just a thing"

    def test_run_session_fingerprint_failure_path_no_value_leak(
        self, monkeypatch, tmp_path
    ):
        """端到端：session_fingerprint redaction fail 时 CheckResult.detail 不含 raw value."""
        from argparse import Namespace
        from unittest import mock

        # 让 collect 返回一个会 fail redaction 的 fingerprint (含 raw value)
        bad_fp = {
            "claude_md": {"exists": True, "size_bytes": 100, "sha256_first8": "deadbeef"},
            "secret_field_xyz": "/leaked/path/to/secret",  # 不在 allowlist
        }
        with mock.patch(
            "_session_fingerprint.collect_session_fingerprint", return_value=bad_fp
        ):
            args = Namespace(
                json=False,
                no_cli=True,
                mode="session",
                claude_skills_dir=str(tmp_path / "skills"),
                codex_skills_dir=str(tmp_path / "skills2"),
            )
            results, fp = aqg_doctor._run_session_fingerprint(args, aqg_root=None)
        # fail path: fingerprint=None, 加 FAIL CheckResult
        assert fp is None
        assert any(r.status == "FAIL" for r in results)
        # FAIL detail 不含 raw value
        for r in results:
            if r.status == "FAIL":
                assert "/leaked/path/to/secret" not in r.detail
                assert "/leaked/path/to/secret" not in (r.fix or "")
