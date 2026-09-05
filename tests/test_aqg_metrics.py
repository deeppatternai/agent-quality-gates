"""Tests for scripts/aqg_metrics.py — metrics ledger CLI."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import aqg_metrics as am  # noqa: E402


REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    """Provide isolated ledger path for tests."""
    ledger = tmp_path / "metrics-ledger.jsonl"
    monkeypatch.setenv("AQG_METRICS_PATH", str(ledger))
    return ledger


# ============================================================
# Opt-in (3-auditor major)
# ============================================================


class TestOptIn:
    @pytest.mark.parametrize("val,expected", [
        ("1", True), ("true", True), ("yes", True), ("on", True),
        ("TRUE", True), ("Yes", True), ("On", True),
        ("0", False), ("false", False), ("no", False), ("off", False),
        ("FALSE", False), ("False", False),
        ("", False), ("garbage", False), ("anything", False),
    ])
    def test_env_truthy_whitelist(self, val, expected, monkeypatch):
        monkeypatch.setenv("AQG_METRICS", val)
        ns = argparse.Namespace(record_metrics=False)
        assert am._is_metrics_enabled(ns) == expected

    def test_unset_env_disabled(self, monkeypatch):
        monkeypatch.delenv("AQG_METRICS", raising=False)
        ns = argparse.Namespace(record_metrics=False)
        assert am._is_metrics_enabled(ns) is False

    def test_flag_overrides_env_disabled(self, monkeypatch):
        monkeypatch.setenv("AQG_METRICS", "false")
        ns = argparse.Namespace(record_metrics=True)
        assert am._is_metrics_enabled(ns) is True


# ============================================================
# Path resolver (gemini #4: empty XDG safe)
# ============================================================


class TestPathResolver:
    def test_default_home_aqg(self, monkeypatch):
        monkeypatch.delenv("AQG_METRICS_PATH", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        path = am._default_ledger_path()
        assert path == Path.home() / ".aqg" / "metrics-ledger.jsonl"

    def test_xdg_set(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AQG_METRICS_PATH", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        path = am._default_ledger_path()
        assert path == tmp_path / "aqg" / "metrics-ledger.jsonl"

    def test_xdg_empty_string_safe(self, monkeypatch):
        """gemini #4 (minor): empty XDG_DATA_HOME should not → /aqg/..."""
        monkeypatch.delenv("AQG_METRICS_PATH", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", "")
        path = am._default_ledger_path()
        assert str(path).startswith(str(Path.home())), path

    def test_explicit_aqg_metrics_path(self, tmp_path, monkeypatch):
        explicit = tmp_path / "custom.jsonl"
        monkeypatch.setenv("AQG_METRICS_PATH", str(explicit))
        assert am._default_ledger_path() == explicit


# ============================================================
# Atomic append + permissions (3-auditor major + 2-auditor)
# ============================================================


class TestAppendLocked:
    def test_creates_dir_with_0o700(self, tmp_path):
        ledger = tmp_path / "subdir" / "metrics.jsonl"
        record = {"schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
                  "tool": "audit", "result": "pass"}
        am._append_record_locked(ledger, record)
        assert ledger.parent.is_dir()
        mode = stat.S_IMODE(ledger.parent.stat().st_mode)
        assert mode == 0o700, f"dir mode 0{mode:o} ≠ 0o700"

    def test_creates_file_with_0o600(self, tmp_path):
        ledger = tmp_path / "metrics.jsonl"
        record = {"schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
                  "tool": "audit", "result": "pass"}
        am._append_record_locked(ledger, record)
        mode = stat.S_IMODE(ledger.stat().st_mode)
        assert mode == 0o600, f"file mode 0{mode:o} ≠ 0o600"

    def test_appends_one_line_per_record(self, tmp_path):
        ledger = tmp_path / "metrics.jsonl"
        for i in range(3):
            am._append_record_locked(ledger, {
                "schema_version": 1, "ts": f"2026-05-03T00:00:0{i}+00:00",
                "tool": "audit", "result": "pass",
            })
        lines = ledger.read_text().splitlines()
        assert len(lines) == 3
        for line in lines:
            obj = json.loads(line)
            assert obj["tool"] == "audit"

    def test_fsync_called(self, tmp_path):
        """o3 #4: fsync after each record for durability."""
        ledger = tmp_path / "metrics.jsonl"
        with mock.patch("os.fsync") as mocked:
            am._append_record_locked(ledger, {
                "schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
                "tool": "audit", "result": "pass",
            })
            mocked.assert_called()


# ============================================================
# Subcommand: record (opt-in flow)
# ============================================================


class TestCmdRecord:
    def test_no_op_when_not_opt_in(self, isolated_ledger, capsys, monkeypatch):
        monkeypatch.delenv("AQG_METRICS", raising=False)
        rc = am.main(["record", "--tool", "audit", "--result", "pass"])
        assert rc == am.EXIT_OK
        assert not isolated_ledger.exists()
        assert "not enabled" in capsys.readouterr().err

    def test_require_record_returns_exit_3(self, isolated_ledger, monkeypatch):
        monkeypatch.delenv("AQG_METRICS", raising=False)
        rc = am.main(["record", "--require-record", "--tool", "audit", "--result", "pass"])
        assert rc == am.EXIT_NOT_OPT_IN

    def test_record_with_flag(self, isolated_ledger):
        rc = am.main([
            "record", "--record-metrics", "--tool", "audit", "--result", "pass",
            "--duration-ms", "100", "--actor", "claude",
        ])
        assert rc == am.EXIT_OK
        assert isolated_ledger.exists()
        line = isolated_ledger.read_text().strip()
        obj = json.loads(line)
        assert obj["tool"] == "audit"
        assert obj["actor"] == "claude"

    def test_record_with_env(self, isolated_ledger, monkeypatch):
        monkeypatch.setenv("AQG_METRICS", "yes")
        rc = am.main(["record", "--tool", "preflight", "--result", "warn"])
        assert rc == am.EXIT_OK
        line = isolated_ledger.read_text().strip()
        obj = json.loads(line)
        assert obj["tool"] == "preflight"
        assert obj["result"] == "warn"

    def test_record_redaction_fail(self, isolated_ledger):
        rc = am.main([
            "record", "--record-metrics", "--tool", "audit", "--result", "pass",
            "--tool-version", "0.2.6-ghp_FAKE",  # token mid-string
        ])
        assert rc == am.EXIT_FAIL
        assert not isolated_ledger.exists()

    def test_record_json_stdin(self, isolated_ledger, monkeypatch):
        monkeypatch.setattr("sys.stdin", mock.Mock(read=lambda: json.dumps({
            "tool": "doctor", "result": "pass",
        })))
        rc = am.main(["record", "--record-metrics", "--json"])
        assert rc == am.EXIT_OK
        obj = json.loads(isolated_ledger.read_text().strip())
        assert obj["tool"] == "doctor"


# ============================================================
# Subcommand: list
# ============================================================


class TestCmdList:
    def test_empty_ledger(self, isolated_ledger, capsys):
        rc = am.main(["list"])
        assert rc == am.EXIT_OK
        assert capsys.readouterr().out.strip() == "[]"

    def test_list_records(self, isolated_ledger):
        for tool in ("audit", "preflight", "audit"):
            am._append_record_locked(isolated_ledger, {
                "schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
                "tool": tool, "result": "pass",
            })
        # capture
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = am.main(["list"])
        assert rc == am.EXIT_OK
        records = json.loads(buf.getvalue())
        assert len(records) == 3

    def test_list_filter_by_tool(self, isolated_ledger):
        for tool in ("audit", "preflight", "audit"):
            am._append_record_locked(isolated_ledger, {
                "schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
                "tool": tool, "result": "pass",
            })
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            am.main(["list", "--tool", "audit"])
        records = json.loads(buf.getvalue())
        assert len(records) == 2

    def test_list_skips_unknown_schema_version(self, isolated_ledger, capsys):
        """gpt-5.5 #4 + o3 #6 (2-auditor): list skips unknown version + warn."""
        # Write one v99 record
        with isolated_ledger.open("w") as fh:
            fh.write(json.dumps({"schema_version": 99, "ts": "2026-05-03T00:00:00+00:00",
                                 "tool": "audit", "result": "pass"}) + "\n")
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            am.main(["list"])
        records = json.loads(buf.getvalue())
        assert records == []
        assert "unknown schema_version" in capsys.readouterr().err


# ============================================================
# Subcommand: clear
# ============================================================


class TestCmdClear:
    def test_requires_yes_flag(self, isolated_ledger, capsys):
        am._append_record_locked(isolated_ledger, {
            "schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
            "tool": "audit", "result": "pass",
        })
        rc = am.main(["clear"])
        assert rc == am.EXIT_USAGE
        assert isolated_ledger.exists()

    def test_yes_truncates_keeps_inode(self, isolated_ledger):
        """Post-impl gemini #1 (CRITICAL): clear switched to ftruncate instead of unlink (prevents ghost inode race)."""
        am._append_record_locked(isolated_ledger, {
            "schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
            "tool": "audit", "result": "pass",
        })
        rc = am.main(["clear", "--yes"])
        assert rc == am.EXIT_OK
        # File should still exist (inode preserved) but be empty
        assert isolated_ledger.exists()
        assert isolated_ledger.stat().st_size == 0


# ============================================================
# Subcommand: prune (forward-compat)
# ============================================================


class TestCmdPrune:
    def test_drops_old_records(self, isolated_ledger):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()
        am._append_record_locked(isolated_ledger, {
            "schema_version": 1, "ts": old_ts, "tool": "audit", "result": "pass",
        })
        am._append_record_locked(isolated_ledger, {
            "schema_version": 1, "ts": new_ts, "tool": "audit", "result": "pass",
        })
        rc = am.main(["prune", "--days", "90"])
        assert rc == am.EXIT_OK
        lines = isolated_ledger.read_text().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["ts"] == new_ts

    def test_preserves_unknown_schema_by_default(self, isolated_ledger):
        """gpt-5.5 #4 + o3 #6 (2-auditor): prune should not delete unknown schema (forward-compat)."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        # Write v99 record (unknown version, future)
        with isolated_ledger.open("w") as fh:
            fh.write(json.dumps({"schema_version": 99, "ts": old_ts,
                                 "tool": "audit", "result": "pass"}) + "\n")
        rc = am.main(["prune", "--days", "1"])
        assert rc == am.EXIT_OK
        # v99 record preserved
        lines = isolated_ledger.read_text().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["schema_version"] == 99

    def test_destructive_prune_removes_unknown(self, isolated_ledger):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        with isolated_ledger.open("w") as fh:
            fh.write(json.dumps({"schema_version": 99, "ts": old_ts,
                                 "tool": "audit", "result": "pass"}) + "\n")
        rc = am.main(["prune", "--days", "1", "--destructive-prune"])
        assert rc == am.EXIT_OK
        assert isolated_ledger.read_text().strip() == ""


# ============================================================
# Subcommand: status
# ============================================================


class TestPostImplFixes:
    """Post-impl two-round audit (audit_id bcbdea3f) 9 accepted findings regression tests."""

    def test_clear_uses_ftruncate_not_unlink(self, isolated_ledger):
        """gemini #1 CRITICAL: clear uses ftruncate to prevent a ghost inode race."""
        am._append_record_locked(isolated_ledger, {
            "schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
            "tool": "audit", "result": "pass",
        })
        original_inode = isolated_ledger.stat().st_ino
        am.main(["clear", "--yes"])
        # Inode preserved
        assert isolated_ledger.stat().st_ino == original_inode

    def test_unknown_top_level_key_not_echoed(self, isolated_ledger):
        """gpt-5.5 #4 (major): unknown key text NEVER echoed in violation msg."""
        from io import StringIO
        secret_key = "ghp_LEAKED_KEY_NAME_FAKE"
        record = {secret_key: "x", "tool": "audit", "result": "pass"}
        with mock.patch("sys.stdin", mock.Mock(read=lambda: json.dumps(record))):
            buf = StringIO()
            with mock.patch("sys.stderr", buf):
                rc = am.main(["record", "--record-metrics", "--json"])
        assert rc == am.EXIT_FAIL
        err = buf.getvalue()
        assert secret_key not in err, f"secret key leaked in violation: {err}"
        assert "key names suppressed" in err

    def test_negative_days_rejected(self, isolated_ledger):
        """gpt-5.5 #6 (major): reject --days < 0 to prevent a future cutoff from deleting future records."""
        am._append_record_locked(isolated_ledger, {
            "schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
            "tool": "audit", "result": "pass",
        })
        rc = am.main(["prune", "--days", "-1"])
        assert rc == am.EXIT_USAGE
        # Original record preserved
        assert isolated_ledger.read_text().strip() != ""

    def test_partial_write_loop(self, tmp_path):
        """gpt-5.5 #3 (major): os.write loop handles partial writes."""
        ledger = tmp_path / "metrics.jsonl"
        # Track os.write calls; first call returns half, second the rest
        write_log: list[int] = []
        original = os.write

        def fake_write(fd, data):
            if len(write_log) == 0:
                half = max(1, len(data) // 2)
                write_log.append(half)
                return original(fd, data[:half])
            n = original(fd, data)
            write_log.append(n)
            return n

        with mock.patch("os.write", side_effect=fake_write):
            am._append_record_locked(ledger, {
                "schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
                "tool": "audit", "result": "pass",
            })
        # Should have looped at least 2 times
        assert len(write_log) >= 2
        # Final ledger contains the full record
        obj = json.loads(ledger.read_text().strip())
        assert obj["tool"] == "audit"

    def test_list_skips_unsafe_record(self, isolated_ledger, capsys):
        """gpt-5.5 #5 (major): list does not print an already-stored unsafe record."""
        # Manually inject a v1 record with unknown field (passes parse, fails validation)
        with isolated_ledger.open("w") as fh:
            fh.write(json.dumps({
                "schema_version": 1, "ts": "2026-05-03T00:00:00+00:00",
                "tool": "audit", "result": "pass",
                "unknown_field_with_secret": "ghp_LEAK",
            }) + "\n")
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            am.main(["list"])
        records = json.loads(buf.getvalue())
        assert records == []
        err = capsys.readouterr().err
        assert "unsafe" in err
        assert "ghp_LEAK" not in err  # sanitized


class TestCmdStatus:
    def test_status_format(self, isolated_ledger, capsys):
        rc = am.main(["status"])
        assert rc == am.EXIT_OK
        out = capsys.readouterr().out
        info = json.loads(out)
        assert "ledger_path" in info
        assert "opt_in_state" in info
        assert info["opt_in_state"] in ("enabled", "disabled")
