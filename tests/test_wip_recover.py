"""Tests for scripts/wip_recover.py — SessionStart hook handler.

Each test is annotated with the accepted finding from the three-round audit
(audit_id 656913cb) it corresponds to.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import wip_recover as wrec  # noqa: E402


def _make_snapshot(
    *,
    session_id: str = "test_session",
    cwd_hash: str = "deadbeef",
    saved_iso: str | None = None,
    schema_version: int = 1,
) -> dict:
    return {
        "schema_version": schema_version,
        "session_id": session_id,
        "saved_at_iso": saved_iso or datetime.now(timezone.utc).isoformat(),
        "trigger": "PreCompact",
        "cwd_sha256_first8": cwd_hash,
        "cwd_status": {
            "exists": True,
            "is_directory": True,
            "git_branch_status": "present",
            "git_dirty": True,
            "modified_files_count": 3,
        },
        "marker": "auto-saved-precompact",
    }


def _write_snap(wip_dir: Path, snap: dict) -> Path:
    wip_dir.mkdir(parents=True, exist_ok=True)
    p = wip_dir / f"{snap['session_id']}.json"
    p.write_text(json.dumps(snap))
    return p


# ============================================================
# _scan_wip_dir: gpt-5.5 #5 (return path + snapshot)
# ============================================================


class TestScanWipDir:
    def test_empty_dir(self, tmp_path):
        assert wrec._scan_wip_dir(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path):
        assert wrec._scan_wip_dir(tmp_path / "no") == []

    def test_returns_path_and_snapshot(self, tmp_path):
        _write_snap(tmp_path, _make_snapshot(session_id="abc"))
        scanned = wrec._scan_wip_dir(tmp_path)
        assert len(scanned) == 1
        assert scanned[0].path.name == "abc.json"
        assert scanned[0].snapshot is not None
        assert scanned[0].snapshot["session_id"] == "abc"

    def test_corrupt_json_returns_none_snapshot(self, tmp_path):
        """o3 #6 + gpt-5.5 #5: corrupt JSON still records the path; snapshot=None."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        scanned = wrec._scan_wip_dir(tmp_path)
        assert len(scanned) == 1
        assert scanned[0].snapshot is None
        assert scanned[0].mtime_iso  # mtime is still recorded


# ============================================================
# _is_too_old / _prune_old: gpt-5.5 #5 + o3 #6
# ============================================================


class TestPrune:
    def test_recent_snapshot_kept(self, tmp_path):
        recent_iso = datetime.now(timezone.utc).isoformat()
        _write_snap(tmp_path, _make_snapshot(session_id="r1", saved_iso=recent_iso))
        scanned = wrec._scan_wip_dir(tmp_path)
        alive, pruned = wrec._prune_old(scanned, max_age_days=7)
        assert pruned == 0
        assert len(alive) == 1

    def test_old_snapshot_pruned(self, tmp_path):
        old_iso = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        path = _write_snap(tmp_path, _make_snapshot(session_id="r1", saved_iso=old_iso))
        scanned = wrec._scan_wip_dir(tmp_path)
        alive, pruned = wrec._prune_old(scanned, max_age_days=7)
        assert pruned == 1
        assert len(alive) == 0
        assert not path.exists()

    def test_corrupt_uses_mtime_fallback(self, tmp_path):
        """o3 #6 accepted: corrupt JSON is pruned using the mtime fallback."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        # set mtime to 10 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        os.utime(str(bad), (old_ts, old_ts))
        scanned = wrec._scan_wip_dir(tmp_path)
        alive, pruned = wrec._prune_old(scanned, max_age_days=7)
        assert pruned == 1
        assert not bad.exists()

    def test_invalid_iso_treated_as_too_old(self):
        assert wrec._is_too_old("not-a-date", max_age_days=7)


# ============================================================
# _filter_by_cwd: gemini #2 (prevent cross-project cross-talk)
# ============================================================


class TestFilterByCwd:
    def test_only_matching_cwd_kept(self, tmp_path):
        _write_snap(tmp_path, _make_snapshot(session_id="proj_a", cwd_hash="aabbccdd"))
        _write_snap(tmp_path, _make_snapshot(session_id="proj_b", cwd_hash="bbccddee"))
        scanned = wrec._scan_wip_dir(tmp_path)
        filtered = wrec._filter_by_cwd(scanned, cwd_hash="aabbccdd")
        assert len(filtered) == 1
        assert filtered[0].snapshot["session_id"] == "proj_a"

    def test_no_match_returns_empty(self, tmp_path):
        _write_snap(tmp_path, _make_snapshot(cwd_hash="aabbccdd"))
        scanned = wrec._scan_wip_dir(tmp_path)
        assert wrec._filter_by_cwd(scanned, cwd_hash="bbccddee") == []


# ============================================================
# _filter_by_schema: gpt-5.5 #7
# ============================================================


class TestFilterBySchema:
    def test_v1_kept(self, tmp_path, capsys):
        _write_snap(tmp_path, _make_snapshot(schema_version=1))
        scanned = wrec._scan_wip_dir(tmp_path)
        filtered = wrec._filter_by_schema(scanned)
        assert len(filtered) == 1

    def test_unknown_version_dropped_with_warn(self, tmp_path, capsys):
        _write_snap(tmp_path, _make_snapshot(session_id="v99", schema_version=99))
        scanned = wrec._scan_wip_dir(tmp_path)
        filtered = wrec._filter_by_schema(scanned)
        assert len(filtered) == 0
        out = capsys.readouterr()
        assert "schema_version" in out.err


# ============================================================
# _format_recovery_message
# ============================================================


class TestFormatMessage:
    def test_empty_returns_empty(self):
        assert wrec._format_recovery_message([], cwd_hash="x", all_projects=False) == ""

    def test_one_snapshot_renders(self):
        snap = _make_snapshot(session_id="abc")
        msg = wrec._format_recovery_message([snap], cwd_hash="deadbeef", all_projects=False)
        assert "abc" in msg
        assert "AQG WIP Recovery" in msg
        assert "current project" in msg

    def test_all_projects_label(self):
        snap = _make_snapshot()
        msg = wrec._format_recovery_message([snap], cwd_hash="x", all_projects=True)
        assert "across all projects" in msg


# ============================================================
# main() integration
# ============================================================


class TestMain:
    def test_empty_dir_no_stdout(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = wrec.main([], wip_dir=tmp_path / "wip")
        assert rc == 0
        out = capsys.readouterr()
        assert out.out == ""

    def test_match_cwd_prints_recovery(self, tmp_path, capsys, monkeypatch):
        # cwd_hash matches the monkeypatched cwd
        cwd_path = tmp_path / "myproj"
        cwd_path.mkdir()
        cwd_hash = wrec._sha256_first8(str(cwd_path.resolve()))
        wip = tmp_path / "wip"
        _write_snap(wip, _make_snapshot(session_id="match", cwd_hash=cwd_hash))
        monkeypatch.chdir(cwd_path)
        rc = wrec.main([], wip_dir=wip, cwd=cwd_path)
        assert rc == 0
        out = capsys.readouterr()
        assert "match" in out.out
        assert "AQG WIP Recovery" in out.out

    def test_mismatch_cwd_silent(self, tmp_path, capsys, monkeypatch):
        wip = tmp_path / "wip"
        _write_snap(wip, _make_snapshot(session_id="other", cwd_hash="ccddeeff"))
        # use diff cwd
        cwd_path = tmp_path / "myproj"
        cwd_path.mkdir()
        rc = wrec.main([], wip_dir=wip, cwd=cwd_path)
        assert rc == 0
        out = capsys.readouterr()
        assert out.out == ""

    def test_all_flag_overrides_cwd_filter(self, tmp_path, capsys):
        wip = tmp_path / "wip"
        _write_snap(wip, _make_snapshot(session_id="other", cwd_hash="ccddeeff"))
        cwd_path = tmp_path / "myproj"
        cwd_path.mkdir()
        rc = wrec.main(["--all"], wip_dir=wip, cwd=cwd_path)
        assert rc == 0
        out = capsys.readouterr()
        assert "other" in out.out

    def test_oversized_file_skipped(self, tmp_path, capsys):
        """Post-impl two-round audit gpt-5.5 #4: > MAX_SNAPSHOT_FILE_BYTES → skip stat-first."""
        wip = tmp_path / "wip"
        wip.mkdir()
        # write a 100KB file
        big = wip / "huge.json"
        big.write_text("{" + "a" * (100 * 1024) + "}")
        cwd_path = tmp_path / "myproj"
        cwd_path.mkdir()
        rc = wrec.main([], wip_dir=wip, cwd=cwd_path)
        assert rc == 0
        err = capsys.readouterr().err
        assert "oversized" in err

    def test_redaction_filter_catches_loaded_snapshot(self, tmp_path, capsys):
        """Post-impl two-round audit gpt-5.5 #1: recover runs _wip_redaction on the disk snapshot it reads.

        Construct a v1 snapshot that contains a raw branch name; recover should skip it and produce no output.
        """
        wip = tmp_path / "wip"
        # write raw directly - bypass the save validator
        wip.mkdir()
        cwd_hash = wrec._sha256_first8(str((tmp_path / "myproj").resolve()))
        bad_snap = {
            "schema_version": 1,
            "session_id": "abc123",
            "saved_at_iso": datetime.now(timezone.utc).isoformat(),
            "trigger": "PreCompact",
            "cwd_sha256_first8": cwd_hash,
            "cwd_status": {
                # add an unknown field containing a secret string
                "raw_branch_name": "secret-payroll-customer-xyz",
            },
        }
        (wip / "abc123.json").write_text(json.dumps(bad_snap))
        cwd_path = tmp_path / "myproj"
        cwd_path.mkdir()
        rc = wrec.main([], wip_dir=wip, cwd=cwd_path)
        assert rc == 0
        captured = capsys.readouterr()
        # secret must not appear in stdout / stderr
        assert "secret-payroll-customer-xyz" not in captured.out
        assert "secret-payroll-customer-xyz" not in captured.err
        # stderr should carry a redaction skip warning
        assert "redaction check failed" in captured.err

    def test_display_cap(self, tmp_path, capsys):
        """Post-impl two-round audit gpt-5.5 #4: format displays at most MAX_DISPLAY_SNAPSHOTS."""
        wip = tmp_path / "wip"
        cwd_path = tmp_path / "myproj"
        cwd_path.mkdir()
        cwd_hash = wrec._sha256_first8(str(cwd_path.resolve()))
        for i in range(wrec.MAX_DISPLAY_SNAPSHOTS + 3):
            _write_snap(wip, _make_snapshot(session_id=f"sess_{i:03d}", cwd_hash=cwd_hash))
        rc = wrec.main([], wip_dir=wip, cwd=cwd_path)
        out = capsys.readouterr().out
        assert "more snapshot(s) omitted" in out

    def test_log_path_redacted_to_tilde(self, tmp_path, monkeypatch, capsys):
        """Post-impl two-round audit gpt-5.5 #5: alive log must not contain an absolute path."""
        monkeypatch.setenv("HOME", str(tmp_path))
        wip = tmp_path / "wip"
        cwd_path = tmp_path / "myproj"
        cwd_path.mkdir()
        cwd_hash = wrec._sha256_first8(str(cwd_path.resolve()))
        _write_snap(wip, _make_snapshot(session_id="sess1", cwd_hash=cwd_hash))
        rc = wrec.main([], wip_dir=wip, cwd=cwd_path)
        err = capsys.readouterr().err
        # tilde-prefixed path appears (rather than a raw absolute path)
        if "alive snapshot" in err:
            for line in err.splitlines():
                if "alive snapshot:" in line:
                    # must not start with /Users or /home; should start with ~ or be a bare filename
                    path_part = line.split("alive snapshot:", 1)[1].strip()
                    assert not path_part.startswith("/Users")
                    assert not path_part.startswith("/home")

    def test_old_snapshot_pruned_silently(self, tmp_path, capsys):
        wip = tmp_path / "wip"
        old_iso = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        path = _write_snap(wip, _make_snapshot(session_id="oldie", saved_iso=old_iso))
        cwd_path = tmp_path / "myproj"
        cwd_path.mkdir()
        rc = wrec.main([], wip_dir=wip, cwd=cwd_path)
        assert rc == 0
        assert not path.exists()  # already pruned
