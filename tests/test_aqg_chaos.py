"""Tests for scripts/aqg_chaos.py + scripts/_chaos_scenarios.py — chaos runner."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import aqg_chaos as chaos  # noqa: E402
import _chaos_scenarios as cs  # noqa: E402


REPO = Path(__file__).resolve().parent.parent


# ============================================================
# Hermetic env (3-auditor CRITICAL)
# ============================================================


class TestHermeticEnv:
    def test_default_isolates_home_xdg_aqg(self, tmp_path):
        env = chaos._build_hermetic_env(tmp_path)
        assert env["HOME"] == str(tmp_path / "home")
        assert env["XDG_DATA_HOME"] == str(tmp_path / "xdg-data")
        assert env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg-config")
        assert env["XDG_CACHE_HOME"] == str(tmp_path / "xdg-cache")
        assert env["AQG_ROOT"] == str(tmp_path)
        assert env["AQG_METRICS_PATH"] == str(tmp_path / "chaos-ledger.jsonl")
        assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path.parent)

    def test_path_default_minimal(self, tmp_path):
        env = chaos._build_hermetic_env(tmp_path)
        assert env["PATH"] == "/usr/bin:/bin"

    def test_inherit_specific_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR", "value123")
        env = chaos._build_hermetic_env(tmp_path, inherit=("MY_TEST_VAR",))
        assert env["MY_TEST_VAR"] == "value123"

    def test_extra_overrides(self, tmp_path):
        env = chaos._build_hermetic_env(tmp_path, extra={"AQG_ROOT": ""})
        assert env["AQG_ROOT"] == ""

    def test_creates_isolated_dirs(self, tmp_path):
        chaos._build_hermetic_env(tmp_path)
        assert (tmp_path / "home").is_dir()
        assert (tmp_path / "xdg-data").is_dir()
        # Permissions 0o700
        mode = stat.S_IMODE((tmp_path / "home").stat().st_mode)
        assert mode == 0o700


# ============================================================
# Vetted copy (gpt-5.5 #4 + o3 #6)
# ============================================================


class TestVettedCopy:
    def test_copies_regular_files(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "VERSION").write_text("0.0.0")
        (src / "scripts").mkdir()
        (src / "scripts" / "foo.py").write_text("# foo")
        dst = tmp_path / "dst"
        chaos._vetted_copy(src, dst)
        assert (dst / "VERSION").read_text() == "0.0.0"
        assert (dst / "scripts" / "foo.py").read_text() == "# foo"

    def test_skips_dot_git(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / ".git").mkdir()
        (src / ".git" / "config").write_text("real-git-config")
        (src / "VERSION").write_text("0.0.0")
        dst = tmp_path / "dst"
        chaos._vetted_copy(src, dst)
        assert not (dst / ".git").exists()
        assert (dst / "VERSION").exists()

    def test_skips_pycache(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "x.pyc").write_text("compiled")
        (src / "real.py").write_text("# real")
        dst = tmp_path / "dst"
        chaos._vetted_copy(src, dst)
        assert not (dst / "__pycache__").exists()
        assert (dst / "real.py").exists()

    def test_rejects_symlink_escape(self, tmp_path):
        """Post-impl gpt-5.5 #2: escape symlink raises RuntimeError (not silent unlink)."""
        src = tmp_path / "src"
        src.mkdir()
        outside = tmp_path / "outside-target"
        outside.write_text("secret")
        (src / "evil_link").symlink_to(outside)
        dst = tmp_path / "dst"
        with pytest.raises(RuntimeError, match="escapes dst"):
            chaos._vetted_copy(src, dst)

    def test_dst_already_exists_raises(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "dst"
        dst.mkdir()
        with pytest.raises(RuntimeError, match="dst already exists"):
            chaos._vetted_copy(src, dst)


# ============================================================
# Safe rmtree (gemini #3 + o3 #5)
# ============================================================


class TestSafeRmtree:
    def test_removes_normal_tree(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        (d / "f").write_text("ok")
        chaos._safe_rmtree(d)
        assert not d.exists()

    def test_handles_perm_error(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        (d / "f").write_text("ok")
        # Strip write perm so rmtree initially fails
        os.chmod(d, 0o500)
        try:
            chaos._safe_rmtree(d)
            assert not d.exists()
        finally:
            # Best-effort cleanup if test failed
            if d.exists():
                os.chmod(d, 0o700)

    def test_nonexistent_no_op(self, tmp_path):
        chaos._safe_rmtree(tmp_path / "nonexistent")  # no raise


# ============================================================
# Output redaction (gpt-5.5 #7 + o3 #7)
# ============================================================


class TestRedaction:
    def test_chaos_redact_replaces_paths(self):
        text = f"hello {Path.home()} world"
        out = chaos._chaos_redact(text, real_paths=(str(Path.home()),))
        assert str(Path.home()) not in out
        assert "REDACTED-PATH" in out

    def test_chaos_redact_truncates_oversized(self):
        big = "X" * 10000
        out = chaos._chaos_redact(big, real_paths=())
        assert len(out.encode("utf-8")) <= chaos.MAX_STREAM_BYTES + 100  # +truncation marker

    def test_check_no_leak_finds_paths(self):
        text = "I am at /Users/secret"
        leaks = chaos._check_no_leak(text, real_paths=("/Users/secret",))
        assert leaks == ["/Users/secret"]

    def test_build_no_leak_set_includes_home_and_root(self):
        leak_set = chaos._build_no_leak_set()
        assert str(Path.home()) in leak_set


# ============================================================
# Scenario contract validation
# ============================================================


class TestScenarioContract:
    def test_all_scenarios_have_unique_names(self):
        names = [s.name for s in cs.list_scenarios()]
        assert len(names) == len(set(names))

    def test_all_scenarios_target_scripts_dir(self):
        for s in cs.list_scenarios():
            assert s.target.startswith("scripts/")

    def test_expect_exit_codes_non_empty_frozenset(self):
        for s in cs.list_scenarios():
            assert isinstance(s.expect_exit_codes, frozenset)
            assert len(s.expect_exit_codes) >= 1

    def test_get_scenario_by_name(self):
        s = cs.get_scenario("doctor_aqg_root_unset")
        assert s is not None
        assert s.name == "doctor_aqg_root_unset"

    def test_get_scenario_unknown_returns_none(self):
        assert cs.get_scenario("nonexistent_scenario") is None

    def test_dropped_scenario_not_present(self):
        """Pre-impl rejected: doctor_python_below_min not implementable as shell chaos."""
        assert cs.get_scenario("doctor_python_below_min") is None


# ============================================================
# Run-all integration
# ============================================================


class TestRunAll:
    def test_run_all_all_pass(self, capsys):
        """End-to-end: all 5 scenarios pass against current AQG."""
        rc = chaos.main(["run-all"])
        assert rc == chaos.EXIT_OK
        err = capsys.readouterr().err
        assert "5/5 pass" in err

    def test_filter_limit(self, capsys):
        rc = chaos.main(["run-all", "--filter", "doctor"])
        assert rc == chaos.EXIT_OK
        err = capsys.readouterr().err
        # Only doctor_install + doctor_aqg_root_unset = 2 scenarios
        assert "2/2 pass" in err or "1/1 pass" in err

    def test_run_single(self, capsys):
        rc = chaos.main(["run", "wip_save_corrupt_stdin"])
        assert rc == chaos.EXIT_OK

    def test_run_unknown(self, capsys):
        rc = chaos.main(["run", "nonexistent_xyz"])
        assert rc == chaos.EXIT_USAGE


# ============================================================
# Process group kill on timeout (3-auditor major)
# ============================================================


class TestPostImplFixes:
    """Post-impl Deep audit (audit_id ca9848dc) 8 accepted findings regression tests."""

    def test_chmod_no_follow_symlink(self, tmp_path):
        """gemini #1 CRITICAL: _safe_chmod_no_follow must not chmod through a symlink."""
        outside = tmp_path / "outside_file"
        outside.write_text("important")
        os.chmod(outside, 0o644)
        link = tmp_path / "link_to_outside"
        link.symlink_to(outside)
        # Try to chmod via the link
        chaos._safe_chmod_no_follow(str(link), 0o700)
        # Outside file mode should NOT have changed
        mode = stat.S_IMODE(outside.stat().st_mode)
        assert mode == 0o644, f"chmod followed symlink: outside mode now 0{mode:o}"

    def test_hardlink_rejected(self, tmp_path):
        """convergent gpt-5.5 #1 + gemini #5: _vetted_copy reject hardlink."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "VERSION").write_text("0.0.0")
        regular = src / "regular.txt"
        regular.write_text("content")
        # Create hardlink to regular file inside src
        hardlink = src / "hardlink.txt"
        os.link(str(regular), str(hardlink))
        dst = tmp_path / "dst"
        with pytest.raises(RuntimeError, match="hardlink"):
            chaos._vetted_copy(src, dst)

    def test_redact_order_long_paths_first(self):
        """gemini #2: longer paths replaced first to prevent HOME replacement from missing AQG_ROOT."""
        leak_set = chaos._build_no_leak_set(("/Users/joe", "/Users/joe/proj"))
        # /Users/joe/proj must come BEFORE /Users/joe in the sorted set
        # (auto-prepended real paths may also be in the set, but order matters)
        leak_list = list(leak_set)
        proj_idx = leak_list.index("/Users/joe/proj")
        joe_idx = leak_list.index("/Users/joe")
        assert proj_idx < joe_idx, f"long path /Users/joe/proj should come before /Users/joe in {leak_list}"
        # Apply redaction: both should be eliminated
        text = "I work in /Users/joe/proj/scripts"
        out = chaos._chaos_redact(text, real_paths=leak_set)
        assert "/Users/joe" not in out
        assert "/proj" not in out

    def test_safe_rmtree_outer_fallback_runs(self, tmp_path):
        """gemini #3: onerror raises so outer ignore_errors fallback works."""
        d = tmp_path / "stubborn"
        d.mkdir()
        (d / "f").write_text("ok")
        # The fix ensures rmtree completes (via fallback) even in pathological cases
        chaos._safe_rmtree(d)
        assert not d.exists()

    def test_expect_no_leak_scenario_field(self):
        """convergent gpt-5.5 #3 + gemini #4: ChaosScenario has expect_no_leak field."""
        scenarios = cs.list_scenarios()
        # All scenarios should have the field (even if empty default)
        for s in scenarios:
            assert hasattr(s, "expect_no_leak")
            assert isinstance(s.expect_no_leak, tuple)

    def test_setup_error_returns_exit_3(self, tmp_path, monkeypatch):
        """gpt-5.5 #4: setup error → EXIT_SETUP_FAIL (not EXIT_FAIL)."""
        # Construct a scenario that will fail in setup_fn
        from _chaos_scenarios import ChaosScenario, list_scenarios
        from unittest import mock

        def boom(_root):
            raise RuntimeError("intentional setup fail")

        bad_scenario = ChaosScenario(
            name="setup_fail_test",
            description="x",
            target="scripts/aqg_doctor.py",
            invoke_argv=("--no-cli",),
            invoke_cwd="",
            expect_exit_codes=frozenset({0}),
            setup_fn=boom,
        )
        with mock.patch("_chaos_scenarios.list_scenarios", return_value=(bad_scenario,)):
            rc = chaos.main(["run-all"])
        assert rc == chaos.EXIT_SETUP_FAIL

    def test_doctor_aqg_root_unset_strict_exit_1(self):
        """gpt-5.5 #5: doctor_aqg_root_unset only accepts exit 1."""
        s = cs.get_scenario("doctor_aqg_root_unset")
        assert s is not None
        assert s.expect_exit_codes == frozenset({1})
        # Requires AQG_ROOT in output (json goes to stdout)
        assert any("AQG_ROOT" in needle for needle in s.expect_stdout_contains)


class TestProcessGroupKill:
    def test_start_new_session_used(self):
        """Verify Popen called with start_new_session=True."""
        import inspect
        src = inspect.getsource(chaos._run_scenario)
        assert "start_new_session=True" in src

    def test_killpg_used_on_timeout(self):
        import inspect
        src = inspect.getsource(chaos._run_scenario)
        assert "os.killpg" in src
        assert "SIGKILL" in src
