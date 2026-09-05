"""Tests for scripts/wip_checkpoint.py — WIP checkpoint (true code snapshot).

Behavior Lock for the aqg-code-construction `wip-checkpoint` task.

Uses REAL temporary git repos: the checkpoint's whole value is in real
git-object operations (temp-index plumbing, refs/aqg-wip, recover). Mocking
subprocess would test our mock, not git's actual behavior — so we drive a
throwaway repo per test and assert on real refs / worktree state.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import wip_checkpoint as wc  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return proc.stdout.strip()


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True).returncode == 0


@pytest.fixture(autouse=True)
def _clean_hook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # isolate main()'s cwd resolution from any AQG_HOOK_PROJECT_DIR in the runner env
    monkeypatch.delenv("AQG_HOOK_PROJECT_DIR", raising=False)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Throwaway git repo with one committed file (tracked.txt = v1)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.txt").write_text("v1\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


class TestSave:
    def test_creates_ref_when_dirty(self, git_repo: Path) -> None:
        (git_repo / "tracked.txt").write_text("v2\n")  # modify tracked
        result = wc.save("sess1", repo=git_repo)
        assert result.action == "created"
        assert _git_ok(git_repo, "rev-parse", "refs/aqg-wip/sess1")

    def test_skips_when_clean(self, git_repo: Path) -> None:
        result = wc.save("sess1", repo=git_repo)
        assert result.action == "skipped_clean"
        assert not _git_ok(git_repo, "rev-parse", "refs/aqg-wip/sess1")

    def test_checkpoint_includes_untracked(self, git_repo: Path) -> None:
        # The whole point over `git stash create`: untracked NEW files are saved.
        (git_repo / "newfile.txt").write_text("brand new\n")
        result = wc.save("sess1", repo=git_repo)
        assert result.action == "created"
        files = _git(git_repo, "ls-tree", "-r", "--name-only", "refs/aqg-wip/sess1")
        assert "newfile.txt" in files.splitlines()

    def test_does_not_touch_worktree_or_index(self, git_repo: Path) -> None:
        # Core promise: checkpoint is invisible — worktree + staging unchanged.
        (git_repo / "tracked.txt").write_text("v2\n")
        (git_repo / "untracked.txt").write_text("u\n")
        before_status = _git(git_repo, "status", "--porcelain")
        before_index = _git(git_repo, "diff", "--cached", "--name-only")
        wc.save("sess1", repo=git_repo)
        assert _git(git_repo, "status", "--porcelain") == before_status
        assert _git(git_repo, "diff", "--cached", "--name-only") == before_index

    def test_does_not_pollute_branch_stash_log(self, git_repo: Path) -> None:
        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sess1", repo=git_repo)
        assert _git(git_repo, "stash", "list") == ""
        assert "aqg-wip" not in _git(git_repo, "branch", "-a")
        assert len(_git(git_repo, "log", "--oneline").splitlines()) == 1

    def test_dedup_same_tree_skips(self, git_repo: Path) -> None:
        (git_repo / "tracked.txt").write_text("v2\n")
        r1 = wc.save("sess1", repo=git_repo)
        assert r1.action == "created"
        sha1 = _git(git_repo, "rev-parse", "refs/aqg-wip/sess1")
        r2 = wc.save("sess1", repo=git_repo)  # nothing changed since
        assert r2.action == "skipped_unchanged"
        assert _git(git_repo, "rev-parse", "refs/aqg-wip/sess1") == sha1  # no churn

    def test_updates_ref_when_content_changes(self, git_repo: Path) -> None:
        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sess1", repo=git_repo)
        sha1 = _git(git_repo, "rev-parse", "refs/aqg-wip/sess1")
        (git_repo / "tracked.txt").write_text("v3\n")
        r2 = wc.save("sess1", repo=git_repo)
        assert r2.action == "created"
        assert _git(git_repo, "rev-parse", "refs/aqg-wip/sess1") != sha1

    def test_invalid_session_id_rejected(self, git_repo: Path) -> None:
        (git_repo / "tracked.txt").write_text("v2\n")
        result = wc.save("../../etc/passwd", repo=git_repo)
        assert result.action == "error"

    def test_checkpoint_commit_uses_fixed_identity(self, git_repo: Path) -> None:
        # machine-made checkpoint must not carry the user's real git identity
        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sess1", repo=git_repo)
        author = _git(git_repo, "log", "-1", "--format=%an", "refs/aqg-wip/sess1")
        assert author == "aqg-wip"

    def test_save_in_unborn_repo_no_head(self, tmp_path: Path) -> None:
        # audit F/claude-f4: unborn repo (no commit) is a named high-risk branch
        repo = tmp_path / "unborn"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t.com")
        _git(repo, "config", "user.name", "t")
        (repo / "newfile.txt").write_text("x\n")  # untracked, no HEAD
        result = wc.save("sess1", repo=repo)
        assert result.action == "created"  # parent-less checkpoint
        files = _git(repo, "ls-tree", "-r", "--name-only", "refs/aqg-wip/sess1")
        assert "newfile.txt" in files.splitlines()

    def test_save_in_detached_head(self, git_repo: Path) -> None:
        # audit F: detached HEAD is a named high-risk branch
        head = _git(git_repo, "rev-parse", "HEAD")
        _git(git_repo, "checkout", "-q", head)  # detach
        (git_repo / "tracked.txt").write_text("v2\n")
        result = wc.save("sess1", repo=git_repo)
        assert result.action == "created"
        assert _git_ok(git_repo, "rev-parse", "refs/aqg-wip/sess1")

    def test_session_id_with_trailing_newline_rejected(self, git_repo: Path) -> None:
        # audit B: re.match+$ accepts "abc\n"; fullmatch must reject it
        (git_repo / "tracked.txt").write_text("v2\n")
        result = wc.save("abc\n", repo=git_repo)
        assert result.action == "error"

    def test_build_error_is_skipped_error_not_clean(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # audit C: a git failure must NOT masquerade as skipped_clean
        (git_repo / "tracked.txt").write_text("v2\n")
        real = wc._run_git

        def fake(repo, args, **kw):  # type: ignore[no-untyped-def]
            if args and args[0] == "write-tree":
                return (1, "")  # force failure
            return real(repo, args, **kw)

        monkeypatch.setattr(wc, "_run_git", fake)
        result = wc.save("sess1", repo=git_repo)
        assert result.action == "skipped_error"
        assert not _git_ok(git_repo, "rev-parse", "refs/aqg-wip/sess1")

    def test_error_does_not_clobber_prior_ref(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # audit C: a failed save must leave the previous checkpoint intact
        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sess1", repo=git_repo)
        sha1 = _git(git_repo, "rev-parse", "refs/aqg-wip/sess1")
        (git_repo / "tracked.txt").write_text("v3\n")
        real = wc._run_git

        def fake(repo, args, **kw):  # type: ignore[no-untyped-def]
            if args and args[0] == "commit-tree":
                return (1, "")
            return real(repo, args, **kw)

        monkeypatch.setattr(wc, "_run_git", fake)
        result = wc.save("sess1", repo=git_repo)
        assert result.action == "skipped_error"
        assert _git(git_repo, "rev-parse", "refs/aqg-wip/sess1") == sha1  # preserved


class TestRecover:
    def test_lists_checkpoint_with_restore_command(self, git_repo: Path) -> None:
        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sess1", repo=git_repo)
        _git(git_repo, "checkout", "--", "tracked.txt")  # simulate lost work
        result = wc.recover(repo=git_repo)
        assert result.shown == 1
        assert "sess1" in result.message
        assert "refs/aqg-wip/sess1" in result.message
        assert "git checkout" in result.message  # restore command present

    def test_no_checkpoints_empty(self, git_repo: Path) -> None:
        result = wc.recover(repo=git_repo)
        assert result.shown == 0
        assert result.message == ""

    def test_finalized_checkpoint_pruned(self, git_repo: Path) -> None:
        # change + checkpoint + commit → HEAD tree == ref tree → auto-prune, not shown
        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sess1", repo=git_repo)
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "finalize")
        result = wc.recover(repo=git_repo)
        assert result.shown == 0
        assert not _git_ok(git_repo, "rev-parse", "refs/aqg-wip/sess1")

    def test_does_not_modify_worktree(self, git_repo: Path) -> None:
        # recover only PROMPTS; it must never auto-restore into the worktree.
        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sess1", repo=git_repo)
        _git(git_repo, "checkout", "--", "tracked.txt")  # lose it
        wc.recover(repo=git_repo)
        assert (git_repo / "tracked.txt").read_text() == "v1\n"  # untouched

    def test_message_has_no_raw_filenames(self, git_repo: Path) -> None:
        # prompt goes into SessionStart context — counts + commands only, no paths.
        (git_repo / "secret_path_file.txt").write_text("x\n")
        wc.save("sess1", repo=git_repo)
        result = wc.recover(repo=git_repo)
        assert "secret_path_file.txt" not in result.message

    def test_prunes_old_checkpoint(self, git_repo: Path) -> None:
        import datetime

        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sess1", repo=git_repo)
        _git(git_repo, "checkout", "--", "tracked.txt")
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
        result = wc.recover(repo=git_repo, max_age_days=7, now=future)
        assert result.pruned >= 1
        assert not _git_ok(git_repo, "rev-parse", "refs/aqg-wip/sess1")

    def test_message_warns_against_auto_run(self, git_repo: Path) -> None:
        # audit D (claude-f1): prompt must tell the agent NOT to auto-run commands
        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sess1", repo=git_repo)
        _git(git_repo, "checkout", "--", "tracked.txt")
        rec = wc.recover(repo=git_repo)
        assert "do NOT run" in rec.message
        assert "OVERWRITES" in rec.message

    def test_no_head_count_not_understated_as_zero(self, tmp_path: Path) -> None:
        # audit A (claude-f3 + deepseek-f1): unborn repo recover must not show 0
        repo = tmp_path / "unborn"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t.com")
        _git(repo, "config", "user.name", "t")
        (repo / "a.txt").write_text("x\n")
        (repo / "b.txt").write_text("y\n")
        wc.save("sess1", repo=repo)
        rec = wc.recover(repo=repo)
        assert rec.shown == 1
        assert "0 file(s)" not in rec.message  # ls-tree fallback counts real files
        assert "2 file(s)" in rec.message


class TestMain:
    def test_save_mode_never_prints_stdout(self, git_repo: Path, capsys) -> None:
        # Stop/PreCompact stdout is injected into context — save must stay silent there.
        (git_repo / "tracked.txt").write_text("v2\n")
        event = json.dumps({"session_id": "sessmain", "cwd": str(git_repo)})
        assert wc.main(["save"], stdin_text=event) == 0
        assert capsys.readouterr().out == ""
        assert _git_ok(git_repo, "rev-parse", "refs/aqg-wip/sessmain")

    def test_recover_mode_prints_stdout(self, git_repo: Path, capsys) -> None:
        (git_repo / "tracked.txt").write_text("v2\n")
        wc.save("sessmain", repo=git_repo)
        _git(git_repo, "checkout", "--", "tracked.txt")
        event = json.dumps({"cwd": str(git_repo)})
        assert wc.main(["recover"], stdin_text=event) == 0
        assert "AQG WIP Checkpoint Recovery" in capsys.readouterr().out

    def test_token_shaped_session_id_falls_back(self, git_repo: Path) -> None:
        sid = wc._resolve_session_id(
            {"session_id": "ghp_FAKE_TOKEN_SHAPED_xxxxxx"}, cwd=git_repo
        )
        assert not sid.startswith("ghp_")  # a leaked token must never become a ref name

    def test_non_git_dir_is_safe_noop(self, tmp_path: Path, capsys) -> None:
        event = json.dumps({"cwd": str(tmp_path)})  # not a git repo
        assert wc.main(["save"], stdin_text=event) == 0
        assert wc.main(["recover"], stdin_text=event) == 0
        assert capsys.readouterr().out == ""  # nothing surfaced, no crash
