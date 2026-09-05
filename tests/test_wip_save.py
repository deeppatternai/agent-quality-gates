"""Tests for scripts/wip_save.py — PreCompact hook handler.

Each test is annotated with the accepted finding from the three-round audit
(audit_id 656913cb) it corresponds to.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import wip_save as ws  # noqa: E402
import _wip_redaction as wr  # noqa: E402


# ============================================================
# _resolve_session_id: gpt-5.5 #1 + gemini #5
# ============================================================


class TestResolveSessionId:
    def test_valid_event_session_id_used(self):
        sid = ws._resolve_session_id({"session_id": "abc_DEF-123"}, cwd=Path("/tmp"))
        assert sid == "abc_DEF-123"

    def test_path_traversal_session_id_falls_back(self, tmp_path):
        """gpt-5.5 #1: ../ is not allowed."""
        sid = ws._resolve_session_id({"session_id": "../../etc/passwd"}, cwd=tmp_path)
        # fallback hash, 12 chars, no slashes
        assert "/" not in sid
        assert "." not in sid
        assert len(sid) == 12

    def test_empty_session_id_falls_back(self, tmp_path):
        sid = ws._resolve_session_id({}, cwd=tmp_path)
        assert len(sid) == 12

    def test_fallback_stable_across_calls(self, tmp_path):
        """gemini #5: fallback contains no timestamp; repeated calls with the same cwd → same id."""
        sid1 = ws._resolve_session_id({}, cwd=tmp_path)
        sid2 = ws._resolve_session_id({}, cwd=tmp_path)
        assert sid1 == sid2

    def test_token_shaped_session_id_falls_back(self, tmp_path):
        """Post-impl two-round audit gpt-5.5 #2: a token of valid-looking chars like ghp_xxx must fall back."""
        sid = ws._resolve_session_id(
            {"session_id": "ghp_REAL_TOKEN_SHAPED_xxx"}, cwd=tmp_path
        )
        assert not sid.startswith("ghp_")
        assert len(sid) == 12  # fallback hash length

    def test_fallback_uses_resolved_cwd(self, tmp_path):
        """Post-impl two-round audit gemini #2: use cwd.resolve() to prevent a symlink-fragmented session."""
        # create a symlink into tmp_path
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "via_link"
        link.symlink_to(real)
        sid_via_real = ws._resolve_session_id({}, cwd=real)
        sid_via_link = ws._resolve_session_id({}, cwd=link)
        assert sid_via_real == sid_via_link


# ============================================================
# _safe_run: gpt-5.5 #4 + gemini #4 (timeout + non-interactive env)
# ============================================================


class TestSafeRun:
    def test_timeout_returns_127(self):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=2),
        ):
            rc, out = ws._safe_run(["git", "status"], cwd=Path("/tmp"))
            assert rc == 127
            assert out == ""

    def test_missing_command_returns_127(self):
        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError("git not found")
        ):
            rc, out = ws._safe_run(["git", "status"], cwd=Path("/tmp"))
            assert rc == 127

    def test_sets_non_interactive_env(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs.get("env") or {})
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            ws._safe_run(["git"], cwd=Path("/tmp"))
        assert captured.get("GIT_TERMINAL_PROMPT") == "0"

    def test_git_optional_locks_disabled(self):
        """Post-impl two-round audit gpt-5.5 #3: GIT_OPTIONAL_LOCKS=0 is what actually disables it."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs.get("env") or {})
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            ws._safe_run(["git"], cwd=Path("/tmp"))
        assert captured.get("GIT_OPTIONAL_LOCKS") == "0"


# ============================================================
# _collect_cwd_status: gpt-5.5 #3 + o3 #1 (no raw branch)
# ============================================================


class TestCollectCwdStatus:
    def test_non_existent_cwd(self):
        status = ws._collect_cwd_status(Path("/nonexistent/sfp/path"))
        assert status["exists"] is False
        assert status["git_branch_status"] == "missing"
        assert status["git_branch_sha256_first8"] is None

    def test_probe_cli_false_skips_subprocess(self, tmp_path):
        with mock.patch("subprocess.run") as mocked:
            status = ws._collect_cwd_status(tmp_path, probe_cli=False)
            mocked.assert_not_called()
        assert status["git_branch_status"] == "unknown"

    def test_branch_name_never_stored_raw(self, tmp_path):
        """gpt-5.5 #3 + o3 #1: even branch=secret-hotfix/john-payroll must not leak."""
        secret_branch = "secret-hotfix/john-payroll"

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "rev-parse"]:
                return mock.Mock(returncode=0, stdout=secret_branch + "\n", stderr="")
            if cmd[:2] == ["git", "status"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            status = ws._collect_cwd_status(tmp_path, probe_cli=True)

        # the secret_branch string must never appear in any status value
        for v in status.values():
            assert secret_branch not in str(v), f"branch leak: {status}"
        assert status["git_branch_status"] == "present"
        assert status["git_branch_sha256_first8"] is not None
        assert len(status["git_branch_sha256_first8"]) == 8

    def test_default_branch_detected(self, tmp_path):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "rev-parse"]:
                return mock.Mock(returncode=0, stdout="main\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            status = ws._collect_cwd_status(tmp_path, probe_cli=True)
        assert status["git_is_default_branch"] is True

    def test_detached_head(self, tmp_path):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "rev-parse"]:
                return mock.Mock(returncode=0, stdout="HEAD\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            status = ws._collect_cwd_status(tmp_path, probe_cli=True)
        assert status["git_branch_status"] == "detached"
        assert status["git_branch_sha256_first8"] is None


# ============================================================
# _collect_open_pr_count: gemini #4 (probe_cli=False default)
# ============================================================


class TestCollectOpenPrCount:
    def test_default_skip_no_subprocess(self, tmp_path):
        """gemini #4: probe_cli=False by default; makes no network call."""
        with mock.patch("subprocess.run") as mocked:
            count = ws._collect_open_pr_count(tmp_path)
            mocked.assert_not_called()
        assert count is None

    def test_no_jq_used(self, tmp_path):
        """gpt-5.5 #2 + gemini #6 + o3 #4 convergent: NEVER uses jq."""
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return mock.Mock(returncode=0, stdout="[]", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            ws._collect_open_pr_count(tmp_path, probe_cli=True)
        for cmd in captured:
            assert "jq" not in cmd

    def test_parses_json_array_length(self, tmp_path):
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=0, stdout='[{"number":1},{"number":2}]', stderr=""),
        ):
            assert ws._collect_open_pr_count(tmp_path, probe_cli=True) == 2

    def test_invalid_json_returns_none(self, tmp_path):
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=0, stdout="not json", stderr=""),
        ):
            assert ws._collect_open_pr_count(tmp_path, probe_cli=True) is None


# ============================================================
# _collect_todo_state
# ============================================================


class TestCollectTodoState:
    def test_no_todo_in_event(self):
        assert ws._collect_todo_state({}) is None

    def test_invalid_todo_shape(self):
        assert ws._collect_todo_state({"todo": "not a dict"}) is None
        assert ws._collect_todo_state({"todo": {"items": "not a list"}}) is None

    def test_counts_statuses(self):
        event = {
            "todo": {
                "items": [
                    {"status": "in_progress"},
                    {"status": "completed"},
                    {"status": "completed"},
                    {"status": "pending"},
                    {"status": "pending"},
                ]
            }
        }
        result = ws._collect_todo_state(event)
        assert result == {
            "items_count": 5,
            "in_progress_count": 1,
            "completed_count": 2,
            "pending_count": 2,
        }


# ============================================================
# _resolve_safe_path: gpt-5.5 #1 (defense-in-depth path traversal)
# ============================================================


class TestResolveSafePath:
    def test_normal_session_id_resolves_under_wip_dir(self, tmp_path):
        wip_dir = tmp_path / "wip"
        wip_dir.mkdir()
        path = ws._resolve_safe_path("abc123", wip_dir=wip_dir)
        assert path.parent == wip_dir.resolve()


# ============================================================
# Atomic write
# ============================================================


class TestWriteSnapshotAtomic:
    def test_write_creates_dir(self, tmp_path):
        wip_dir = tmp_path / "new" / "wip"
        snap = {
            "schema_version": 1,
            "session_id": "abc123",
            "saved_at_iso": "2026-05-03T00:00:00+00:00",
            "trigger": "PreCompact",
            "cwd_sha256_first8": "deadbeef",
        }
        path = ws._write_snapshot_atomic(snap, wip_dir=wip_dir)
        assert path.exists()
        assert wip_dir.is_dir()
        assert json.loads(path.read_text())["session_id"] == "abc123"

    def test_overwrite_same_session(self, tmp_path):
        snap1 = {
            "schema_version": 1,
            "session_id": "abc123",
            "saved_at_iso": "2026-05-03T00:00:00+00:00",
            "trigger": "PreCompact",
            "cwd_sha256_first8": "deadbeef",
        }
        snap2 = dict(snap1, saved_at_iso="2026-05-03T01:00:00+00:00")
        ws._write_snapshot_atomic(snap1, wip_dir=tmp_path)
        path = ws._write_snapshot_atomic(snap2, wip_dir=tmp_path)
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        loaded = json.loads(path.read_text())
        assert loaded["saved_at_iso"] == "2026-05-03T01:00:00+00:00"


# ============================================================
# Integration main() — end-to-end
# ============================================================


class TestMain:
    def test_empty_stdin_uses_fallback_session(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = ws.main(stdin_text="", wip_dir=tmp_path / "wip")
        assert rc == 0
        files = list((tmp_path / "wip").glob("*.json"))
        assert len(files) == 1

    def test_stdout_silent_o3_5(self, tmp_path, monkeypatch, capsys):
        """o3 #5 contract: NEVER print to stdout (compaction injection guard)."""
        monkeypatch.chdir(tmp_path)
        rc = ws.main(
            stdin_text=json.dumps({"session_id": "test_silent"}),
            wip_dir=tmp_path / "wip",
        )
        captured = capsys.readouterr()
        assert captured.out == ""  # stdout must be empty
        # stderr may contain diagnostics
        assert "saved" in captured.err or rc == 0

    def test_invalid_stdin_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = ws.main(stdin_text="{not valid json", wip_dir=tmp_path / "wip")
        assert rc == 0  # graceful

    def test_full_snapshot_no_secrets_no_paths(self, tmp_path, monkeypatch):
        """End-to-end leak guard: the snapshot file contains no raw cwd/branch strings."""
        secret_path = str(tmp_path)
        secret_branch = "secret-customer-foo-bar-baz"

        monkeypatch.chdir(tmp_path)

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "rev-parse"]:
                return mock.Mock(returncode=0, stdout=secret_branch + "\n", stderr="")
            if cmd[:2] == ["git", "status"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            rc = ws.main(
                stdin_text=json.dumps({"session_id": "leak_test"}),
                wip_dir=tmp_path / "wip",
            )
        assert rc == 0
        # read the snapshot file and assert it contains no secret
        files = list((tmp_path / "wip").glob("*.json"))
        assert len(files) == 1
        text = files[0].read_text()
        assert secret_branch not in text
        assert secret_path not in text  # cwd is a hash, not raw

    def test_snapshot_passes_redaction(self, tmp_path, monkeypatch):
        """End-to-end: the written snapshot must pass the _wip_redaction guard."""
        monkeypatch.chdir(tmp_path)
        ws.main(stdin_text="", wip_dir=tmp_path / "wip")
        files = list((tmp_path / "wip").glob("*.json"))
        snap = json.loads(files[0].read_text())
        wr.assert_safe_wip_snapshot(snap)
