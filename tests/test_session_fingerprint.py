"""Tests for scripts/_session_fingerprint.py — session fingerprint collector.

Each test is annotated with the accepted finding from the three-pass audit (audit_id 2e5407d6).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _session_fingerprint as sfp  # noqa: E402
from _surface_redaction import check_fingerprint  # noqa: E402


# ============================================================
# _file_stats: gemini #2 + o3 #5 (large-file size guard + chunked hash)
# ============================================================


class TestFileStats:
    def test_missing_file_returns_exists_false(self):
        result = sfp._file_stats(Path("/nonexistent/path/x"))
        assert result == {"exists": False}

    def test_small_file_full_hash(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_bytes(b"hello world")
        result = sfp._file_stats(f)
        assert result["exists"] is True
        assert result["size_bytes"] == 11
        expected = hashlib.sha256(b"hello world").hexdigest()[:8]
        assert result["sha256_first8"] == expected

    def test_oversized_file_skips_hash(self, tmp_path):
        """gemini #2: files > 5MB get sha256_first8=None, but size_bytes is still correct."""
        f = tmp_path / "big.bin"
        # 6MB file
        size = 6 * 1024 * 1024
        with f.open("wb") as fh:
            fh.write(b"\x00" * size)
        result = sfp._file_stats(f)
        assert result["exists"] is True
        assert result["size_bytes"] == size
        assert result["sha256_first8"] is None

    def test_chunked_hash_consistent_with_oneshot(self, tmp_path):
        """gemini #2: chunked hash = oneshot hash (size well under 5MB)."""
        # file larger than chunk size but well under 5MB
        f = tmp_path / "medium.bin"
        data = b"x" * (sfp.HASH_CHUNK_BYTES * 3 + 100)
        f.write_bytes(data)
        result = sfp._file_stats(f)
        assert result["sha256_first8"] == hashlib.sha256(data).hexdigest()[:8]

    def test_directory_treated_as_missing(self, tmp_path):
        result = sfp._file_stats(tmp_path)  # a dir, not a file
        assert result == {"exists": False}


# ============================================================
# _json_counts: gemini #3 (guard against len(scalar) TypeError)
# ============================================================


class TestJsonCounts:
    def test_missing_file_all_none(self, tmp_path):
        result = sfp._json_counts(
            tmp_path / "nope.json", key_to_field={"hooks": "hooks_count"}
        )
        assert result == {"hooks_count": None}

    def test_valid_dict_value_counted(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"hooks": {"a": 1, "b": 2, "c": 3}}))
        result = sfp._json_counts(f, key_to_field={"hooks": "hooks_count"})
        assert result == {"hooks_count": 3}

    def test_valid_list_value_counted(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"items": [1, 2, 3, 4]}))
        result = sfp._json_counts(f, key_to_field={"items": "items_count"})
        assert result == {"items_count": 4}

    def test_scalar_value_returns_none_no_typeerror(self, tmp_path):
        """gemini #3: hooks=false (scalar) can't be len()'d — returns None, doesn't raise."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"hooks": False, "permissions": "denied"}))
        result = sfp._json_counts(
            f,
            key_to_field={"hooks": "hooks_count", "permissions": "permissions_count"},
        )
        assert result == {"hooks_count": None, "permissions_count": None}

    def test_null_value_treated_as_zero(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"hooks": None}))
        result = sfp._json_counts(f, key_to_field={"hooks": "hooks_count"})
        assert result == {"hooks_count": 0}

    def test_invalid_json_returns_all_none(self, tmp_path):
        f = tmp_path / "broken.json"
        f.write_text("{not valid json")
        result = sfp._json_counts(f, key_to_field={"hooks": "hooks_count"})
        assert result == {"hooks_count": None}

    def test_top_level_array_returns_all_none(self, tmp_path):
        f = tmp_path / "arr.json"
        f.write_text("[1, 2, 3]")
        result = sfp._json_counts(f, key_to_field={"hooks": "hooks_count"})
        assert result == {"hooks_count": None}


# ============================================================
# _env_dir_status: o3 #3 (NEVER returns raw path/value)
# ============================================================


class TestEnvDirStatus:
    def test_unset_env(self, monkeypatch):
        monkeypatch.delenv("__SFP_TEST_VAR__", raising=False)
        result = sfp._env_dir_status("__SFP_TEST_VAR__")
        assert result == {"set": False, "is_directory": False}

    def test_empty_env(self, monkeypatch):
        monkeypatch.setenv("__SFP_TEST_VAR__", "   ")
        result = sfp._env_dir_status("__SFP_TEST_VAR__")
        assert result == {"set": False, "is_directory": False}

    def test_existing_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("__SFP_TEST_VAR__", str(tmp_path))
        result = sfp._env_dir_status("__SFP_TEST_VAR__")
        assert result == {"set": True, "is_directory": True}

    def test_nonexistent_path(self, monkeypatch):
        monkeypatch.setenv("__SFP_TEST_VAR__", "/nonexistent/sfp/path")
        result = sfp._env_dir_status("__SFP_TEST_VAR__")
        assert result == {"set": True, "is_directory": False}

    def test_never_leaks_raw_path(self, monkeypatch):
        """o3 #3 contract: returned dict NEVER contains the raw path/value."""
        secret = "/tmp/secret/sfp/should/not/leak/anywhere"
        monkeypatch.setenv("__SFP_TEST_VAR__", secret)
        result = sfp._env_dir_status("__SFP_TEST_VAR__")
        # no value (after stringification) should contain the secret substring
        for v in result.values():
            assert secret not in str(v), f"raw path leak: {result}"

    def test_verify_aqg_root_pass(self, monkeypatch, tmp_path):
        # simulate a valid AQG checkout
        (tmp_path / "VERSION").write_text("0.0.0")
        (tmp_path / "scripts").mkdir()
        monkeypatch.setenv("__AQG_TEST_ROOT__", str(tmp_path))
        result = sfp._env_dir_status("__AQG_TEST_ROOT__", verify_aqg_root=True)
        assert result["verify_root"] == "pass"
        # still NEVER contains raw path
        for v in result.values():
            assert str(tmp_path) not in str(v)

    def test_verify_aqg_root_fail_when_missing_files(self, monkeypatch, tmp_path):
        # is a dir but missing VERSION + scripts/
        monkeypatch.setenv("__AQG_TEST_ROOT__", str(tmp_path))
        result = sfp._env_dir_status("__AQG_TEST_ROOT__", verify_aqg_root=True)
        assert result["verify_root"] == "fail"

    def test_verify_aqg_root_fail_when_unset(self, monkeypatch):
        monkeypatch.delenv("__AQG_TEST_ROOT__", raising=False)
        result = sfp._env_dir_status("__AQG_TEST_ROOT__", verify_aqg_root=True)
        assert result == {"set": False, "is_directory": False, "verify_root": "fail"}


# ============================================================
# _python_version: gemini #1 (sys.version_info, NOT subprocess)
# ============================================================


class TestPythonVersion:
    def test_format(self):
        v = sfp._python_version()
        # major.minor.micro, all int
        parts = v.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_no_subprocess_called(self):
        """gemini #1: must NOT call subprocess for python version."""
        with mock.patch("subprocess.run") as mocked:
            sfp._python_version()
            mocked.assert_not_called()


# ============================================================
# _extract_version_token: gpt-5.5 #3 (semver-ish; reject path/URL)
# ============================================================


class TestExtractVersionToken:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("git version 2.45.2", "2.45.2"),
            ("gh version 2.81.0 (2025-05-19)", "2.81.0"),
            ("GNU bash, version 5.2.21(1)-release (arm64-apple-darwin25)", "5.2.21"),
            ("Python 3.13.0", "3.13.0"),
            ("git version 2.45.2 /usr/bin/git", "2.45.2"),  # path doesn't pollute extraction
            ("https://example.com 1.0", "1.0"),
            ("no digits here only words", None),
            ("", None),
        ],
    )
    def test_known_outputs(self, raw, expected):
        assert sfp._extract_version_token(raw) == expected

    def test_path_only_input_returns_none(self):
        # a path with no digits should be ignored
        assert sfp._extract_version_token("/usr/local/bin/something") is None

    def test_oversized_line_rejected(self):
        # an over-long first line is rejected outright (guards against weird wrappers)
        big = "x " * 1000 + "1.2.3"
        assert sfp._extract_version_token(big) is None


# ============================================================
# _cli_version: gpt-5.5 #4 (probe_cli=False ⇒ no subprocess)
# ============================================================


class TestCliVersion:
    def test_probe_cli_false_skips_subprocess(self):
        """gpt-5.5 #4: --no-cli ⇒ probe_cli=False ⇒ NEVER call subprocess."""
        with mock.patch("subprocess.run") as mocked:
            result = sfp._cli_version("git", probe_cli=False)
            assert result is None
            mocked.assert_not_called()

    def test_missing_cli_returns_none(self):
        """probe_cli=True but the CLI doesn't exist → None, doesn't raise."""
        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError("not found")
        ):
            assert sfp._cli_version("nonexistent_cli", probe_cli=True) is None

    def test_timeout_returns_none(self):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10),
        ):
            assert sfp._cli_version("git", probe_cli=True) is None

    def test_nonzero_exit_returns_none(self):
        completed = mock.Mock(returncode=1, stdout="error\n")
        with mock.patch("subprocess.run", return_value=completed):
            assert sfp._cli_version("git", probe_cli=True) is None

    def test_happy_path(self):
        completed = mock.Mock(returncode=0, stdout="git version 2.50.1\n")
        with mock.patch("subprocess.run", return_value=completed):
            assert sfp._cli_version("git", probe_cli=True) == "2.50.1"


# ============================================================
# _gh_auth_summary: o3 #2 (NEVER includes username), gpt-5.5 #4
# ============================================================


class TestGhAuthSummary:
    def test_probe_cli_false_skips_subprocess(self):
        """gpt-5.5 #4."""
        with mock.patch("subprocess.run") as mocked:
            result = sfp._gh_auth_summary(probe_cli=False)
            assert result == {"logged_in": False}
            mocked.assert_not_called()

    def test_logged_out_returns_logged_in_false(self):
        completed = mock.Mock(returncode=1, stdout="You are not logged in")
        with mock.patch("subprocess.run", return_value=completed):
            assert sfp._gh_auth_summary(probe_cli=True) == {"logged_in": False}

    def test_no_gh_cli_returns_logged_in_false(self):
        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError("gh not found")
        ):
            assert sfp._gh_auth_summary(probe_cli=True) == {"logged_in": False}

    def test_logged_in_extracts_host_and_scopes_only(self):
        gh_output = (
            "github.com\n"
            "  X Logged in to github.com account alice (...)\n"
            "  X Active account: true\n"
            "  X Git operations protocol: https\n"
            "  X Token: gho_xxx\n"
            "  X Token scopes: 'repo', 'workflow', 'read:org'\n"
        )
        completed = mock.Mock(returncode=0, stdout=gh_output)
        with mock.patch("subprocess.run", return_value=completed):
            result = sfp._gh_auth_summary(probe_cli=True)
        assert result == {
            "logged_in": True,
            "host": "github.com",
            "scopes_count": 3,
        }

    def test_username_alice_never_in_returned_dict(self):
        """o3 #2: username NEVER appears in any value of the returned dict."""
        username = "alice_top_secret"
        gh_output = (
            f"github.com\n  X Logged in to github.com account {username} (...)\n"
            f"  X Token scopes: 'repo'\n"
        )
        completed = mock.Mock(returncode=0, stdout=gh_output)
        with mock.patch("subprocess.run", return_value=completed):
            result = sfp._gh_auth_summary(probe_cli=True)
        for v in result.values():
            assert username not in str(v), f"username leak: {result}"

    def test_token_never_leaks(self):
        token = "gho_LEAKED_TOKEN_xxx"
        gh_output = f"github.com\n  X Logged in to github.com\n  X Token: {token}\n"
        completed = mock.Mock(returncode=0, stdout=gh_output)
        with mock.patch("subprocess.run", return_value=completed):
            result = sfp._gh_auth_summary(probe_cli=True)
        for v in result.values():
            assert token not in str(v)

    def test_does_not_call_show_token_flag(self):
        """o3 #2 contract: NEVER calls `gh auth status --show-token`."""
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return mock.Mock(returncode=1, stdout="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            sfp._gh_auth_summary(probe_cli=True)
        for cmd in captured:
            assert "--show-token" not in cmd


# ============================================================
# _path_dirs_count
# ============================================================


class TestPathDirsCount:
    def test_count_unique_entries(self, monkeypatch):
        monkeypatch.setenv(
            "PATH",
            os.pathsep.join(["/a", "/b", "/a", "/c"]),  # /a repeated
        )
        assert sfp._path_dirs_count() == 3

    def test_empty_path(self, monkeypatch):
        monkeypatch.setenv("PATH", "")
        assert sfp._path_dirs_count() == 0

    def test_filters_empty_entries(self, monkeypatch):
        monkeypatch.setenv("PATH", os.pathsep.join(["/a", "", "  ", "/b"]))
        assert sfp._path_dirs_count() == 2

    def test_never_returns_raw_paths(self, monkeypatch):
        """contract: returns only the count, never the path string."""
        monkeypatch.setenv("PATH", "/secret/dir/a")
        # the function returns an int — the type itself guarantees no path
        result = sfp._path_dirs_count()
        assert isinstance(result, int)


# ============================================================
# collect_session_fingerprint: integration + gemini #4 (home derivation)
# ============================================================


class TestCollectSessionFingerprint:
    def test_returns_all_expected_categories(self, tmp_path):
        # use tmp_path to simulate the home dir
        fp = sfp.collect_session_fingerprint(
            aqg_root=None,
            claude_skills_dir=tmp_path / ".claude" / "skills",
            codex_skills_dir=tmp_path / ".codex" / "skills",
            probe_cli=False,
        )
        # all 16 surfaces
        expected_keys = {
            "claude_md",
            "codex_agents_md",
            "claude_skills_dir",
            "codex_skills_dir",
            "claude_settings_json",
            "env_AQG_ROOT",
            "env_XDG_DATA_HOME",
            "env_CODEX_HOME",
            "python3_version",
            "git_version",
            "gh_version",
            "bash_version",
            "model_default_status",
            "sandbox_status",
            "path_dirs_count",
            "gh_auth",
        }
        assert set(fp.keys()) == expected_keys

    def test_home_derived_from_skills_dir_parent(self, tmp_path):
        """gemini #4: claude_home is derived from claude_skills_dir.parent."""
        # build a fake home: /tmp/foo/.claude/{skills/, CLAUDE.md, settings.json}
        fake_claude_home = tmp_path / "myhome" / ".claude"
        (fake_claude_home / "skills").mkdir(parents=True)
        (fake_claude_home / "CLAUDE.md").write_text("# fake")
        fp = sfp.collect_session_fingerprint(
            aqg_root=None,
            claude_skills_dir=fake_claude_home / "skills",
            codex_skills_dir=tmp_path / "myhome" / ".codex" / "skills",
            probe_cli=False,
        )
        assert fp["claude_md"]["exists"] is True
        assert fp["claude_md"]["size_bytes"] == len(b"# fake")

    def test_passes_redaction_guard(self, tmp_path):
        """end-to-end: collect output must pass _surface_redaction.check_fingerprint."""
        fp = sfp.collect_session_fingerprint(
            aqg_root=None,
            claude_skills_dir=tmp_path / "skills",
            codex_skills_dir=tmp_path / "skills2",
            probe_cli=False,
        )
        result = check_fingerprint(fp)
        assert result.is_safe, f"violations: {result.violations}"

    def test_model_default_and_sandbox_status_present(self, tmp_path):
        """gpt-5.5 #5: §12 not_probed category explicitly sets *_status=warn."""
        fp = sfp.collect_session_fingerprint(
            aqg_root=None,
            claude_skills_dir=tmp_path / "skills",
            codex_skills_dir=tmp_path / "skills2",
            probe_cli=False,
        )
        assert fp["model_default_status"] == "warn"
        assert fp["sandbox_status"] == "warn"

    def test_probe_cli_false_no_cli_calls(self, tmp_path):
        """gpt-5.5 #4: probe_cli=False ⇒ no subprocess calls (except sys.version_info)."""
        with mock.patch("subprocess.run") as mocked:
            fp = sfp.collect_session_fingerprint(
                aqg_root=None,
                claude_skills_dir=tmp_path / "skills",
                codex_skills_dir=tmp_path / "skills2",
                probe_cli=False,
            )
            mocked.assert_not_called()
        # still OK to get python3_version
        assert fp["python3_version"] is not None
        assert fp["git_version"] is None
        assert fp["gh_auth"] == {"logged_in": False}


# ============================================================
# full-integration fingerprint contains no raw path / token / username
# ============================================================


class TestNoRawValueLeak:
    def test_full_fingerprint_text_does_not_contain_raw_secrets(
        self, tmp_path, monkeypatch
    ):
        """end-to-end guard: the serialized fingerprint text contains no raw secret/path strings."""
        secret_path = "/tmp/SFP_SECRET_PATH_xxx"
        secret_token = "ghp_SFP_FAKE_TOKEN_xxx"
        username = "SFP_FAKE_USER_xxx"
        monkeypatch.setenv("AQG_ROOT", secret_path)
        monkeypatch.setenv("XDG_DATA_HOME", secret_path)

        # mock gh output containing username + token
        gh_output = (
            f"github.com\n  X Logged in to github.com account {username} (...)\n"
            f"  X Token: {secret_token}\n"
            f"  X Token scopes: 'repo'\n"
        )

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["gh", "auth"]:
                return mock.Mock(returncode=0, stdout=gh_output)
            return mock.Mock(returncode=0, stdout="git version 2.50.1\n")

        with mock.patch("subprocess.run", side_effect=fake_run):
            fp = sfp.collect_session_fingerprint(
                aqg_root=None,
                claude_skills_dir=tmp_path / "skills",
                codex_skills_dir=tmp_path / "skills2",
                probe_cli=True,
            )

        serialized = json.dumps(fp)
        assert secret_path not in serialized
        assert secret_token not in serialized
        assert username not in serialized
        # can still get host/scope but doesn't extract username
        assert fp["gh_auth"]["host"] == "github.com"
        assert fp["gh_auth"]["scopes_count"] == 1
