"""Tests for scripts/install_pre_commit.py — pre-commit hook installer.

Each test is annotated with the accepted finding it corresponds to from the three-round audit (audit_id 7ebd63e7).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import install_pre_commit as ipc  # noqa: E402


REPO = Path(__file__).resolve().parent.parent


def _make_fake_aqg(tmp_path: Path) -> Path:
    """Construct a minimal fake AQG checkout: VERSION + templates/."""
    root = tmp_path / "fake-aqg"
    (root / "templates").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "VERSION").write_text("0.0.0")
    (root / "templates" / "pre-commit-config.aqg.example.yaml").write_text(
        "# AQG fake template\nrepos:\n  - repo: gitleaks\n    hooks:\n      - id: gitleaks\n"
    )
    return root


def _make_fake_git(tmp_path: Path) -> Path:
    """Init a real git repo via subprocess (cheaper than libgit2)."""
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=repo, check=True, timeout=10
    )
    return repo


# ============================================================
# _safe_run: gpt-5.5 #6 + o3 #5 (timeout + non-interactive env)
# ============================================================


class TestSafeRun:
    def test_sets_non_interactive_env(self):
        rc, out, err = ipc._safe_run(
            ["bash", "-c", "echo $GIT_TERMINAL_PROMPT,$PIP_NO_INPUT,$PRE_COMMIT_COLOR"],
            timeout=5,
        )
        assert rc == 0
        assert out == "0,1,never"

    def test_timeout_returns_127(self):
        # Use unsupported tool to trigger TimeoutExpired path
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1),
        ):
            rc, _, _ = ipc._safe_run(["x"], timeout=1)
            assert rc == 127

    def test_missing_command_returns_127(self):
        rc, _, _ = ipc._safe_run(["this_command_xyz_does_not_exist_aqg"], timeout=2)
        assert rc == 127


# ============================================================
# _resolve_git_toplevel: gpt-5.5 #3 (use rev-parse, not .git/ stat)
# ============================================================


class TestResolveGitToplevel:
    def test_real_git_repo(self, tmp_path):
        repo = _make_fake_git(tmp_path)
        top = ipc._resolve_git_toplevel(repo)
        assert top is not None
        assert top.resolve() == repo.resolve()

    def test_subdir_inside_repo_resolves_to_top(self, tmp_path):
        repo = _make_fake_git(tmp_path)
        sub = repo / "sub" / "deep"
        sub.mkdir(parents=True)
        top = ipc._resolve_git_toplevel(sub)
        assert top is not None
        assert top.resolve() == repo.resolve()

    def test_non_git_returns_none(self, tmp_path):
        non_repo = tmp_path / "plain"
        non_repo.mkdir()
        assert ipc._resolve_git_toplevel(non_repo) is None


# ============================================================
# _check_pre_commit_available: gpt-5.5 #2 + o3 #2 (PATH-safe via sys.executable -m)
# ============================================================


class TestCheckPreCommitAvailable:
    def test_uses_sys_executable_dash_m(self):
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return mock.Mock(returncode=0, stdout="pre-commit 4.0.1", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            ipc._check_pre_commit_available()
        assert any(
            cmd[:3] == [sys.executable, "-m", "pre_commit"] for cmd in captured
        ), captured


# ============================================================
# _install_pre_commit_via_pip: 3-auditor convergent critical
# ============================================================


class TestInstallPreCommitViaPip:
    def test_uses_sys_executable_dash_m_pip(self):
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            ipc._install_pre_commit_via_pip("4.0.1")
        # must use sys.executable -m pip (gpt-5.5 #2 + o3 #2 convergent)
        assert any(
            cmd[:4] == [sys.executable, "-m", "pip", "install"] for cmd in captured
        ), captured
        # must include the pin
        assert any("pre-commit==4.0.1" in cmd for cmd in captured), captured
        # must include --user
        assert any("--user" in cmd for cmd in captured), captured


# ============================================================
# _copy_config: gpt-5.5 #7 + gemini #2 (idempotent byte-identical OK)
# ============================================================


class TestCopyConfig:
    def test_writes_when_missing(self, tmp_path):
        aqg = _make_fake_aqg(tmp_path)
        target = _make_fake_git(tmp_path)
        rc, path, info = ipc._copy_config(target, aqg_root=aqg)
        assert rc == ipc.EXIT_OK
        assert path == target / ".pre-commit-config.yaml"
        assert path.exists()
        assert "gitleaks" in path.read_text()

    def test_byte_identical_existing_ok(self, tmp_path):
        """gpt-5.5 #7 + gemini #2: existing byte-identical file -> treated as OK, continue."""
        aqg = _make_fake_aqg(tmp_path)
        target = _make_fake_git(tmp_path)
        # First write
        ipc._copy_config(target, aqg_root=aqg)
        # Second call (rerun, same template) should succeed without --force
        rc, _, info = ipc._copy_config(target, aqg_root=aqg)
        assert rc == ipc.EXIT_OK
        assert "byte-identical" in info

    def test_diff_existing_no_force_rejects(self, tmp_path):
        aqg = _make_fake_aqg(tmp_path)
        target = _make_fake_git(tmp_path)
        cfg = target / ".pre-commit-config.yaml"
        cfg.write_text("# user-customized config\n")
        rc, _, info = ipc._copy_config(target, aqg_root=aqg)
        assert rc == ipc.EXIT_FILE_EXISTS
        assert "use --force" in info or "exists" in info

    def test_diff_existing_force_overwrites(self, tmp_path):
        aqg = _make_fake_aqg(tmp_path)
        target = _make_fake_git(tmp_path)
        cfg = target / ".pre-commit-config.yaml"
        cfg.write_text("# user-customized config\n")
        rc, _, _ = ipc._copy_config(target, aqg_root=aqg, force=True)
        assert rc == ipc.EXIT_OK
        assert "gitleaks" in cfg.read_text()


# ============================================================
# main(): exit codes + flag handling
# ============================================================


class TestMain:
    def test_non_git_target_exit_usage(self, tmp_path):
        non_repo = tmp_path / "plain"
        non_repo.mkdir()
        rc = ipc.main(["--target-repo", str(non_repo)])
        assert rc == ipc.EXIT_USAGE

    def test_dry_run_no_writes(self, tmp_path):
        target = _make_fake_git(tmp_path)
        rc = ipc.main(["--target-repo", str(target), "--dry-run"])
        assert rc == ipc.EXIT_OK
        assert not (target / ".pre-commit-config.yaml").exists()

    def test_default_no_pip_when_pre_commit_missing(self, tmp_path, capsys):
        """3-auditor convergent critical: does NOT install pip by default."""
        target = _make_fake_git(tmp_path)
        with mock.patch(
            "install_pre_commit._check_pre_commit_available", return_value=False
        ):
            rc = ipc.main(["--target-repo", str(target)])
        assert rc == ipc.EXIT_PIP_FAIL
        captured = capsys.readouterr()
        assert "pre-commit not found" in captured.err
        assert "pipx install" in captured.err
        # does not call pip
        assert not (target / ".pre-commit-config.yaml").exists()

    def test_allow_pip_invokes_pip_install(self, tmp_path):
        target = _make_fake_git(tmp_path)
        # First call returns False (not installed), second returns True (after install)
        check_calls = [False, True]
        pip_calls: list[list[str]] = []

        def fake_check():
            return check_calls.pop(0) if check_calls else True

        def fake_install(version):
            pip_calls.append([sys.executable, "-m", "pip", "install", "--user", f"pre-commit=={version}"])
            return 0, ""

        with (
            mock.patch("install_pre_commit._check_pre_commit_available", side_effect=fake_check),
            mock.patch("install_pre_commit._install_pre_commit_via_pip", side_effect=fake_install),
            mock.patch("install_pre_commit._run_pre_commit_install", return_value=(0, "")),
        ):
            rc = ipc.main(["--target-repo", str(target), "--allow-pip"])
        assert rc == ipc.EXIT_OK
        assert len(pip_calls) == 1

    def test_ci_mode_skips_pip_even_with_allow_pip(self, tmp_path, capsys):
        """o3 #8: --ci-mode (alias --skip-pip) skips pip even with --allow-pip."""
        target = _make_fake_git(tmp_path)
        with mock.patch(
            "install_pre_commit._check_pre_commit_available", return_value=False
        ):
            rc = ipc.main(["--target-repo", str(target), "--allow-pip", "--ci-mode"])
        assert rc == ipc.EXIT_PIP_FAIL  # missing + ci-mode = print + fail
        # does not install
        assert not (target / ".pre-commit-config.yaml").exists()

    def test_skip_pip_alias_works(self, tmp_path):
        target = _make_fake_git(tmp_path)
        with mock.patch(
            "install_pre_commit._check_pre_commit_available", return_value=False
        ):
            rc = ipc.main(["--target-repo", str(target), "--allow-pip", "--skip-pip"])
        assert rc == ipc.EXIT_PIP_FAIL

    def test_no_install_hooks_skips_hook_install(self, tmp_path):
        target = _make_fake_git(tmp_path)
        called: list[bool] = []

        def fake_hook_install(*a, **k):
            called.append(True)
            return 0, ""

        with (
            mock.patch("install_pre_commit._check_pre_commit_available", return_value=True),
            mock.patch("install_pre_commit._run_pre_commit_install", side_effect=fake_hook_install),
        ):
            rc = ipc.main(["--target-repo", str(target), "--no-install-hooks"])
        assert rc == ipc.EXIT_OK
        assert not called

    def test_hook_install_failure_returns_exit_5(self, tmp_path):
        """o3 #4: distinct exit code 5 for hook install fail."""
        target = _make_fake_git(tmp_path)
        with (
            mock.patch("install_pre_commit._check_pre_commit_available", return_value=True),
            mock.patch(
                "install_pre_commit._run_pre_commit_install",
                return_value=(1, "go: command not found"),
            ),
        ):
            rc = ipc.main(["--target-repo", str(target)])
        assert rc == ipc.EXIT_HOOK_INSTALL_FAIL


# ============================================================
# Template content correctness
# ============================================================


class TestTemplateContent:
    def test_default_config_template_exists_and_has_gitleaks(self):
        template = REPO / "templates" / "pre-commit-config.aqg.example.yaml"
        assert template.is_file()
        text = template.read_text()
        assert "gitleaks/gitleaks" in text  # o3 #6: official namespace
        assert "v8.21.2" in text  # rev pin

    def test_default_template_has_no_mutating_hooks_uncommented(self):
        """gpt-5.5 #5: the default config should not enable trailing-whitespace / EOF fixer."""
        template = REPO / "templates" / "pre-commit-config.aqg.example.yaml"
        text = template.read_text()
        # find trailing-whitespace - must be in a commented-out section (prefixed with # )
        for line in text.splitlines():
            stripped = line.strip()
            if "trailing-whitespace" in stripped and not stripped.startswith("#"):
                pytest.fail(f"trailing-whitespace enabled by default: {line}")
            if "end-of-file-fixer" in stripped and not stripped.startswith("#"):
                pytest.fail(f"end-of-file-fixer enabled by default: {line}")

    def test_default_template_has_guard_only_hooks(self):
        template = REPO / "templates" / "pre-commit-config.aqg.example.yaml"
        text = template.read_text()
        # default should include detect-private-key + check-merge-conflict + check-added-large-files
        for hook in ("detect-private-key", "check-merge-conflict", "check-added-large-files"):
            assert hook in text, f"missing guard hook: {hook}"

    def test_gitleaks_allowlist_template_exists(self):
        """gpt-5.5 #9 + gemini #4: ship .gitleaks.aqg.example.toml allowlist sample."""
        template = REPO / "templates" / ".gitleaks.aqg.example.toml"
        assert template.is_file()
        text = template.read_text()
        assert "[allowlist]" in text
        assert "paths" in text
        assert "regexes" in text

    def test_aqg_self_dogfood_pre_commit_config_minimal(self):
        """AQG self dogfood: minimal Gitleaks-only config."""
        cfg = REPO / ".pre-commit-config.yaml"
        assert cfg.is_file()
        text = cfg.read_text()
        assert "gitleaks" in text
        # dogfood = minimal; no mutating hooks
        for hook in ("trailing-whitespace", "end-of-file-fixer"):
            for line in text.splitlines():
                if hook in line and not line.strip().startswith("#"):
                    pytest.fail(f"AQG self dogfood enables mutating hook: {line}")


# ============================================================
# CLI subprocess (smoke)
# ============================================================


def _run_cli(*args, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "install_pre_commit.py"), *args],
        input=stdin, text=True, capture_output=True, timeout=15, check=False,
    )


class TestPostImplFixes:
    """Post-impl two-round audit (audit_id 0ec4194e) regression tests for 6 accepted findings."""

    def test_gitleaks_template_has_extend_use_default(self):
        """gpt-5.5 #1 (CRITICAL): the allowlist sample must contain [extend] useDefault=true.

        Without it, when user activates this sample with `--config=.gitleaks.toml`,
        Gitleaks DISABLES built-in rules entirely — real secrets pass through.
        """
        template = REPO / "templates" / ".gitleaks.aqg.example.toml"
        text = template.read_text()
        assert "[extend]" in text, "missing [extend] block; secrets would pass through"
        assert "useDefault = true" in text or "useDefault=true" in text

    def test_skip_config_flag_skips_copy_but_runs_hooks(self, tmp_path):
        """gpt-5.5 #3 + gemini #1 (2-auditor convergent major): --skip-config lets
        users with an existing config install hooks without being blocked by EXIT_FILE_EXISTS."""
        target = _make_fake_git(tmp_path)
        # Pre-existing different config
        cfg = target / ".pre-commit-config.yaml"
        cfg.write_text("# user-customized\nrepos: []\n")
        original = cfg.read_text()
        hook_install_called: list[bool] = []

        def fake_hook_install(*a, **k):
            hook_install_called.append(True)
            return 0, ""

        with (
            mock.patch("install_pre_commit._check_pre_commit_available", return_value=True),
            mock.patch("install_pre_commit._run_pre_commit_install", side_effect=fake_hook_install),
        ):
            rc = ipc.main(["--target-repo", str(target), "--skip-config"])
        assert rc == ipc.EXIT_OK
        assert hook_install_called == [True]
        # User config NOT touched
        assert cfg.read_text() == original

    def test_atomic_write_via_tempfile(self, tmp_path):
        """gpt-5.5 #4 + gemini #3 (2-auditor): _atomic_write_text uses tempfile + os.replace."""
        target = tmp_path / "pre.yaml"
        ipc._atomic_write_text(target, "content")
        assert target.read_text() == "content"
        # Re-write succeeds (replace works on existing target)
        ipc._atomic_write_text(target, "content2")
        assert target.read_text() == "content2"

    def test_atomic_write_refuses_symlink(self, tmp_path):
        """Security: _atomic_write_text does not write through a symlink."""
        real = tmp_path / "real.yaml"
        real.write_text("real")
        link = tmp_path / "link.yaml"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="symlink"):
            ipc._atomic_write_text(link, "evil")

    def test_resolve_pre_commit_cmd_falls_back_to_which(self):
        """gpt-5.5 #2 (major): a pipx-installed pre-commit cannot use sys.executable -m,
        it must fall back to shutil.which."""
        which_call: list[str] = []

        def fake_safe_run(cmd, **kwargs):
            # First: sys.executable -m pre_commit --version → fail
            # Second (after which): /opt/pipx/.../pre-commit --version → OK
            if cmd[:3] == [sys.executable, "-m", "pre_commit"]:
                return 1, "", "No module named pre_commit"
            if cmd[:1] == ["/fake/pipx/bin/pre-commit"]:
                return 0, "pre-commit 4.0.1", ""
            return 1, "", ""

        with (
            mock.patch("install_pre_commit._safe_run", side_effect=fake_safe_run),
            mock.patch("install_pre_commit.shutil.which", return_value="/fake/pipx/bin/pre-commit"),
        ):
            cmd = ipc._resolve_pre_commit_cmd()
        assert cmd == ["/fake/pipx/bin/pre-commit"]

    def test_resolve_pre_commit_cmd_prefers_dash_m_when_available(self):
        with mock.patch("install_pre_commit._safe_run", return_value=(0, "pre-commit 4.0.1", "")):
            cmd = ipc._resolve_pre_commit_cmd()
        assert cmd == [sys.executable, "-m", "pre_commit"]

    def test_resolve_pre_commit_cmd_returns_none_when_missing(self):
        with (
            mock.patch("install_pre_commit._safe_run", return_value=(1, "", "")),
            mock.patch("install_pre_commit.shutil.which", return_value=None),
        ):
            assert ipc._resolve_pre_commit_cmd() is None

    def test_pip_install_omits_user_in_venv(self, monkeypatch):
        """gemini #2 (major): pip --user fails inside a venv. Detect the venv -> omit --user."""
        captured: list[list[str]] = []

        def fake_safe_run(cmd, **kwargs):
            captured.append(cmd)
            return 0, "", ""

        # Force venv detection
        monkeypatch.setattr(sys, "base_prefix", "/opt/system/python")
        monkeypatch.setattr(sys, "prefix", "/tmp/myvenv")

        with mock.patch("install_pre_commit._safe_run", side_effect=fake_safe_run):
            ipc._install_pre_commit_via_pip("4.0.1")
        assert len(captured) == 1
        assert "--user" not in captured[0]
        assert "pre-commit==4.0.1" in captured[0]

    def test_pip_install_includes_user_outside_venv(self, monkeypatch):
        captured: list[list[str]] = []

        def fake_safe_run(cmd, **kwargs):
            captured.append(cmd)
            return 0, "", ""

        # Force NOT in venv
        monkeypatch.setattr(sys, "base_prefix", sys.prefix)

        with mock.patch("install_pre_commit._safe_run", side_effect=fake_safe_run):
            ipc._install_pre_commit_via_pip("4.0.1")
        assert "--user" in captured[0]

    def test_self_test_does_not_use_bash(self):
        """gemini #4 (nit): self_test switched to sys.executable, no longer depends on bash (Alpine/Windows OK)."""
        import inspect
        src = inspect.getsource(ipc.self_test)
        # after the rewrite there should be no direct 'bash' call
        assert "bash" not in src.lower() or 'sys.executable' in src


class TestCLISmoke:
    def test_dry_run_smoke(self):
        proc = _run_cli("--dry-run", "--target-repo", str(REPO))
        assert proc.returncode == ipc.EXIT_OK

    def test_self_test_smoke(self):
        proc = _run_cli("--self-test")
        assert proc.returncode == ipc.EXIT_OK
