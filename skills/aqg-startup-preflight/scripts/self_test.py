#!/usr/bin/env python3
"""Self-test for aqg_preflight.py.

Locks the production-readiness fixes from the Batch-1 dual-audit (audit ebfba0c5):
- C1 fail-closed: failed fetch / rev-list / gh must surface as blockers.
- C2 file-vs-dir: last_sync + context_file_status must not crash on a
  directory and must not count a directory as a present file.
- P1 redact: origin URL userinfo (credentials) must never reach the report.
- P3 sanitize: control/ANSI chars in git/gh output are stripped.
- P4 traversal: --required-file absolute / `..` paths are rejected.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aqg_preflight as preflight

# Disable session dedup by default so the many main()-calling tests below each run the
# FULL report (a marker written by one test must not dedup the next — they run in-process
# in sequence). The dedup-specific tests re-enable it explicitly with their own window +
# isolated state dir.
os.environ["AQG_PREFLIGHT_DEDUP_WINDOW_S"] = "0"


# --------------------------------------------------------------------------
# Existing coverage
# --------------------------------------------------------------------------
def test_missing_path_blocker() -> None:
    missing = Path(tempfile.gettempdir()) / "aqg-preflight-missing-path"
    report = preflight.repo_report("missing", missing, do_fetch=False)
    assert "missing repo path" in report.get("blockers", []), report


def test_status_failure_not_dirty() -> None:
    original_git = preflight.git

    def fake_git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
        if args[:3] == ("status", "--short", "--branch"):
            return 128, "fatal: not a git repository"
        if args[:2] == ("branch", "--show-current"):
            return 0, "main"
        if args[:4] == ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
            return 1, "fatal"
        return 0, ""

    try:
        preflight.git = fake_git
        with tempfile.TemporaryDirectory() as tmp:
            report = preflight.repo_report("bad", Path(tmp), do_fetch=False)
        assert report["dirty"] is None, report
        assert "git status unavailable" in report.get("blockers", []), report
    finally:
        preflight.git = original_git


# --------------------------------------------------------------------------
# C1 — fail-closed on subprocess failure
# --------------------------------------------------------------------------
def _clean_repo_git(fetch_rc: int = 0, revlist_rc: int = 0):
    def fake_git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
        if args[:1] == ("fetch",):
            return fetch_rc, ("" if fetch_rc == 0 else "fatal: could not read from remote")
        if args[:2] == ("branch", "--show-current"):
            return 0, "main"
        if args[:3] == ("status", "--short", "--branch"):
            return 0, "## main...origin/main"
        if args[:4] == ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
            return 0, "origin/main"
        if args[:1] == ("rev-list",):
            return revlist_rc, ("0\t0" if revlist_rc == 0 else "fatal: bad revision")
        return 0, ""

    return fake_git


def test_fetch_failure_is_blocker() -> None:
    original = preflight.git
    try:
        preflight.git = _clean_repo_git(fetch_rc=1)
        with tempfile.TemporaryDirectory() as tmp:
            report = preflight.repo_report("r", Path(tmp), do_fetch=True)
        assert any("fetch" in b for b in report["blockers"]), report["blockers"]
    finally:
        preflight.git = original


def test_revlist_failure_is_blocker() -> None:
    original = preflight.git
    try:
        preflight.git = _clean_repo_git(revlist_rc=128)
        with tempfile.TemporaryDirectory() as tmp:
            report = preflight.repo_report("r", Path(tmp), do_fetch=False)
        assert report.get("behind") is None, report
        assert any("ahead/behind" in b for b in report["blockers"]), report["blockers"]
    finally:
        preflight.git = original


# --------------------------------------------------------------------------
# recent commits — surface HEAD/history so a stale handoff baseline is visible
# at preflight time (a handoff saying "main = <old>" is caught when the report
# shows HEAD is actually further ahead). Advisory-only: NEVER a blocker.
# --------------------------------------------------------------------------
def _repo_git_with_log(log_rc: int, log_out: str):
    """Clean-repo fake_git whose `git log` returns (log_rc, log_out)."""
    base = _clean_repo_git()

    def fake_git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
        if args[:1] == ("log",):
            return log_rc, log_out
        return base(repo, *args, timeout=timeout)

    return fake_git


def test_recent_commits_captured() -> None:
    original = preflight.git
    log_out = "abc1234 feat: add thing\ndef5678 fix: a bug\n9012345 docs: a note"
    try:
        preflight.git = _repo_git_with_log(0, log_out)
        with tempfile.TemporaryDirectory() as tmp:
            report = preflight.repo_report("r", Path(tmp), do_fetch=False)
        assert report["recent_commits"] == log_out, report.get("recent_commits")
        assert "feat: add thing" in report["recent_commits"]
        # advisory only — recent commits must never inject a blocker.
        assert not any("commit" in b.lower() for b in report["blockers"]), report["blockers"]
    finally:
        preflight.git = original


def test_recent_commits_empty_repo_graceful() -> None:
    original = preflight.git
    try:
        # A fresh `git init` with no commits: `git log` exits non-zero. The report
        # must degrade to "<none>", not surface the fatal text or raise.
        preflight.git = _repo_git_with_log(
            128, "fatal: your current branch 'main' does not have any commits yet"
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = preflight.repo_report("r", Path(tmp), do_fetch=False)
        assert report["recent_commits"] == "<none>", report.get("recent_commits")
        assert not any("commit" in b.lower() for b in report["blockers"]), report["blockers"]
    finally:
        preflight.git = original


def test_recent_commits_sanitized() -> None:
    original = preflight.git
    try:
        # A commit subject carrying ANSI + a control char must be stripped before
        # it reaches the agent-facing report (same path as status/fetch output).
        preflight.git = _repo_git_with_log(0, "abc1234 feat: \x1b[31mred\x1b[0m\x07 subject")
        with tempfile.TemporaryDirectory() as tmp:
            report = preflight.repo_report("r", Path(tmp), do_fetch=False)
        assert "\x1b" not in report["recent_commits"], repr(report["recent_commits"])
        assert "\x07" not in report["recent_commits"], repr(report["recent_commits"])
        assert "red subject" in report["recent_commits"], repr(report["recent_commits"])
    finally:
        preflight.git = original


def test_recent_commits_fence_safe_backticks() -> None:
    original = preflight.git
    try:
        # A commit subject carrying ``` would otherwise close the markdown fence it
        # renders inside and inject live markdown into the agent-facing report. The
        # rendered value must contain NO backtick (neutralized to "'", matching the
        # hook's cwd-path convention) so the ``` fence cannot be broken.
        preflight.git = _repo_git_with_log(0, "abc1234 fix ``` then **pwn** heading")
        with tempfile.TemporaryDirectory() as tmp:
            report = preflight.repo_report("r", Path(tmp), do_fetch=False)
        assert "`" not in report["recent_commits"], repr(report["recent_commits"])
        # the visible text survives, only the fence-breaking char is replaced.
        assert "then **pwn** heading" in report["recent_commits"], repr(report["recent_commits"])
    finally:
        preflight.git = original


def test_status_fence_safe_backticks() -> None:
    # #364: a filename in `git status --short` carrying ``` would otherwise close
    # the markdown fence the status block renders inside. The stored value must
    # contain NO backtick so the fence cannot be broken.
    original = preflight.git

    def fake_git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
        if args[:3] == ("status", "--short", "--branch"):
            return 0, "## main\n?? ev``` then ## INJECTED il.txt"
        if args[:2] == ("branch", "--show-current"):
            return 0, "main"
        if args[:4] == ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
            return 1, "fatal"
        return 0, ""

    try:
        preflight.git = fake_git
        with tempfile.TemporaryDirectory() as tmp:
            report = preflight.repo_report("r", Path(tmp), do_fetch=False)
        assert "`" not in report["status"], repr(report["status"])
        assert "then ## INJECTED il.txt" in report["status"], repr(report["status"])
    finally:
        preflight.git = original


def test_gh_lines_fence_safe_backticks() -> None:
    # #364: a PR/issue title (attacker-influenceable) carrying ``` must not close
    # the fence the gh listing renders inside.
    original = preflight.run
    try:
        preflight.run = lambda cmd, cwd=None, timeout=30: (
            0,
            "42\tOPEN\tfix ``` then ## INJECTED title\tbug",
        )
        ok, text = preflight.gh_lines("o/r", "pr")
        assert ok is True, (ok, text)
        assert "`" not in text, repr(text)
        assert "then ## INJECTED title" in text, repr(text)
    finally:
        preflight.run = original


def test_sanitize_external_strips_bidi_zerowidth() -> None:
    # #364 (grok f2): Unicode bidi overrides + zero-width / format controls let
    # visually-deceptive untrusted text reach the report. They must be stripped;
    # ordinary visible text must survive unchanged.
    # Build the chars with chr() so this test source contains NO literal invisible
    # characters (stays reviewable + won't trip bidi scanners). The full deceptive
    # strip class: ALM, ZWSP, LRM/RLM, word-joiner, bidi embeddings/overrides,
    # bidi isolates, BOM.
    strip = [0x061C, 0x200B, 0x200E, 0x200F, 0x2060, 0x202A, 0x202B, 0x202C,
             0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF]
    clean = preflight.sanitize_external("safe" + "".join(chr(c) for c in strip) + "end")
    for c in strip:
        assert chr(c) not in clean, (hex(c), repr(clean))
    assert clean == "safeend", repr(clean)
    # ZWNJ (U+200C) / ZWJ (U+200D) are deliberately PRESERVED — they shape
    # legitimate emoji ligatures + Persian/Indic text, so they must NOT be stripped.
    kept = preflight.sanitize_external("a" + chr(0x200C) + chr(0x200D) + "b")
    assert chr(0x200C) in kept and chr(0x200D) in kept, repr(kept)
    # a plain ASCII string is unchanged (no over-stripping).
    assert preflight.sanitize_external("abc1234 feat: add thing") == "abc1234 feat: add thing"


def test_gh_lines_reports_failure() -> None:
    original = preflight.run
    try:
        preflight.run = lambda cmd, cwd=None, timeout=30: (1, "gh: not authenticated")
        ok, text = preflight.gh_lines("o/r", "pr")
        assert ok is False, (ok, text)
        assert "gh failed" in text, text
    finally:
        preflight.run = original


def test_main_decision_not_clean_on_gh_failure() -> None:
    original_run = preflight.run

    def fake_run(cmd, cwd=None, timeout=30):
        joined = " ".join(cmd)
        if cmd[:1] == ["git"]:
            if "fetch" in cmd:
                return 0, ""
            if "--show-toplevel" in cmd:
                return 0, str(Path.cwd())
            if "--show-current" in cmd:
                return 0, "main"
            if "status" in cmd:
                return 0, "## main...origin/main"
            if "rev-list" in cmd:
                return 0, "0\t0"
            if "rev-parse" in cmd and "@{u}" in joined:
                return 0, "origin/main"
            if "remote" in cmd:
                return 0, "https://github.com/o/r.git"
            return 0, ""
        if cmd[:1] == ["gh"]:
            return 1, "gh: not authenticated"
        return 0, ""

    argv_backup = sys.argv
    try:
        preflight.run = fake_run
        with tempfile.TemporaryDirectory() as tmp:
            sys.argv = ["aqg_preflight.py", "--repo", tmp, "--no-fetch"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = preflight.main()
        out = buf.getvalue()
        assert rc == 0, out
        decision = out.split("## Preflight decision", 1)[1]
        assert "none detected" not in decision.lower(), decision
        assert "gh" in decision.lower() or "github" in decision.lower(), decision
    finally:
        preflight.run = original_run
        sys.argv = argv_backup


# --------------------------------------------------------------------------
# C2 — file vs directory
# --------------------------------------------------------------------------
def test_last_sync_handles_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "docs").mkdir()
        (root / "docs" / "STACK_STATUS.md").mkdir()  # a directory, not a file
        result = preflight.last_sync(root)  # must not raise
        assert "Last sync" not in result, result  # did not parse a directory as a file


def test_context_file_status_directory_not_ok() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "AGENTS.md").mkdir()  # directory shadowing a required file name
        rows = preflight.context_file_status(root, ["AGENTS.md"])
        agents_row = next(r for r in rows if "AGENTS.md" in r)
        assert not agents_row.startswith("OK"), agents_row


# --------------------------------------------------------------------------
# P1 — credential redaction
# --------------------------------------------------------------------------
def test_origin_url_redacts_credentials() -> None:
    assert (
        preflight.redact_url("https://user:secretpw@github.com/o/r.git")
        == "https://github.com/o/r.git"
    )
    assert (
        preflight.redact_url("https://github.com/o/r.git")
        == "https://github.com/o/r.git"
    )
    # ssh form has no :// userinfo — left intact
    assert preflight.redact_url("git@github.com:o/r.git") == "git@github.com:o/r.git"


def test_github_origin_redacts() -> None:
    original = preflight.git
    try:
        preflight.git = lambda repo, *args, timeout=30: (
            0,
            "https://alice:ghp_tokenvalue@github.com/o/r.git",
        )
        slug, url = preflight.github_origin(Path("."))
        assert slug == "o/r", slug
        assert "alice" not in url and "ghp_tokenvalue" not in url, url
    finally:
        preflight.git = original


# --------------------------------------------------------------------------
# P3 — sanitize control / ANSI
# --------------------------------------------------------------------------
def test_sanitize_strips_control_and_ansi() -> None:
    raw = "feat\x1b[31mDANGER\x1b[0m\x00\x07 line\nsecond\ttab"
    clean = preflight.sanitize_external(raw)
    for bad in ("\x1b", "\x00", "\x07"):
        assert bad not in clean, repr(clean)
    assert "\n" in clean and "\ttab" in clean  # newline + tab preserved
    assert "DANGER" in clean and "feat" in clean and "second" in clean


# --------------------------------------------------------------------------
# P4 — required-file traversal guard
# --------------------------------------------------------------------------
def test_required_file_rejects_absolute_and_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert preflight.is_safe_relative(root, "docs/x.md") is True
        assert preflight.is_safe_relative(root, "/etc/passwd") is False
        assert preflight.is_safe_relative(root, "../escape.md") is False


# --------------------------------------------------------------------------
# CODE_MAP advisory (--check-codemap) — state-surface only, never a blocker
# --------------------------------------------------------------------------
def test_is_meta_file_classification() -> None:
    for meta in (
        "README.md", "LICENSE", "package.json", "VERSION",
        "requirements-dev.txt", "CLAUDE.md", "CODE_MAP.md", "Makefile",
        "quality-gates.json",
    ):
        assert preflight._is_meta_file(meta), meta
    for clutter in ("foo.py", "data.csv", "notes.txt", "server.js", "scratch.md"):
        assert not preflight._is_meta_file(clutter), clutter
    # prefix-collision negatives: names that merely START with a meta word but
    # continue into a non-meta token are clutter, not meta (audit 5ed70b41 f2)
    for collision in (
        "readme_parser.py", "license_server.py", "noticeboard.py",
        "requirements_audit.py", "authorship.txt", "copyingmachine.go",
    ):
        assert not preflight._is_meta_file(collision), collision


def test_codemap_status_missing_and_present() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert any("missing" in n for n in preflight.codemap_status(root))
        (root / "CODE_MAP.md").write_text("# map", encoding="utf-8")
        assert any("present" in n for n in preflight.codemap_status(root))


def test_codemap_status_codemap_no_underscore_variant() -> None:
    # CODEMAP.md (no underscore) is an accepted variant (audit 9bb81768 f3)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "CODEMAP.md").write_text("# map", encoding="utf-8")
        assert any("present" in n for n in preflight.codemap_status(root))


def test_codemap_status_all_meta_zero_clutter() -> None:
    # a root populated ONLY with meta files reports zero clutter (audit f3)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for meta in ("README.md", "LICENSE", "pyproject.toml", "VERSION",
                     "SECURITY.md", "Makefile", "CLAUDE.md"):
            (root / meta).write_text("x", encoding="utf-8")
        notes = preflight.codemap_status(root)
        assert any("root non-meta files: 0" in n for n in notes), notes


def test_codemap_status_listdir_failure_reports_unknown() -> None:
    # listdir failure must report "unknown", not a false 0 (audit 5ed70b41 f1)
    original = preflight.os.listdir

    def boom(_path):
        raise OSError("permission denied")

    try:
        preflight.os.listdir = boom
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CODE_MAP.md").write_text("# map", encoding="utf-8")
            notes = preflight.codemap_status(root)
        assert any("unknown" in n for n in notes), notes
        assert not any("root non-meta files: 0" in n for n in notes), notes
    finally:
        preflight.os.listdir = original


def test_codemap_status_clutter_flagged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i in range(15):
            (root / f"loose{i}.py").write_text("x", encoding="utf-8")
        notes = preflight.codemap_status(root)
        assert any("clean-root convention" in n for n in notes), notes


def test_codemap_advisory_is_never_a_blocker() -> None:
    original_git = preflight.git
    argv_backup = sys.argv
    try:
        preflight.git = _clean_repo_git()
        with tempfile.TemporaryDirectory() as tmp:
            sys.argv = [
                "aqg_preflight.py", "--project-root", tmp, "--repo", tmp,
                "--no-fetch", "--no-github", "--check-codemap",
            ]
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = preflight.main()
        out = buf.getvalue()
        assert rc == 0, out
        assert "Project hygiene (advisory" in out, out
        assert "CODE_MAP.md: missing" in out, out
        # advisory must NOT leak into the blocker decision
        decision = out.split("## Preflight decision", 1)[1]
        assert "none detected" in decision.lower(), decision
        assert "code_map" not in decision.lower(), decision
        assert "hygiene" not in decision.lower(), decision
    finally:
        preflight.git = original_git
        sys.argv = argv_backup


def test_codemap_advisory_absent_without_flag() -> None:
    original_git = preflight.git
    argv_backup = sys.argv
    try:
        preflight.git = _clean_repo_git()
        with tempfile.TemporaryDirectory() as tmp:
            sys.argv = ["aqg_preflight.py", "--project-root", tmp, "--repo", tmp,
                        "--no-fetch", "--no-github"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                preflight.main()
        out = buf.getvalue()
        assert "Project hygiene" not in out, out  # opt-in: silent without flag
    finally:
        preflight.git = original_git
        sys.argv = argv_backup


# --------------------------------------------------------------------------
# B6 — STACK_STATUS Last-sync staleness advisory (#183 Tier B; never a blocker)
# --------------------------------------------------------------------------
def _at(iso: str) -> "preflight.dt.datetime":
    """A fixed tz-aware UTC `now` for deterministic age math."""
    return preflight.dt.datetime.fromisoformat(iso).replace(tzinfo=preflight.dt.timezone.utc)


def test_stack_status_advisory_stale() -> None:
    adv = preflight.stack_status_advisory(
        "- Last sync: 2026-05-01", now=_at("2026-05-20T00:00:00")
    )
    assert adv is not None, "19 days old must advise"
    assert "19 days" in adv, adv
    assert "STACK_STATUS" in adv and "2026-05-01" in adv, adv


def test_stack_status_advisory_fresh_is_none() -> None:
    adv = preflight.stack_status_advisory(
        "Last sync: 2026-05-18", now=_at("2026-05-20T00:00:00")
    )
    assert adv is None, adv  # 2 days old — within threshold


def test_stack_status_advisory_boundary_7d() -> None:
    # exactly 7 days → no advisory (strict > 7d); 8 days → advisory
    assert preflight.stack_status_advisory(
        "Last sync: 2026-05-13", now=_at("2026-05-20T00:00:00")
    ) is None
    assert preflight.stack_status_advisory(
        "Last sync: 2026-05-12", now=_at("2026-05-20T00:00:00")
    ) is not None


def test_stack_status_advisory_no_date_is_none() -> None:
    # last_sync() non-date returns (missing / unreadable / not-found) → no advisory
    for line in (
        "not configured: docs/STACK_STATUS.md missing",
        "unreadable: docs/STACK_STATUS.md (OSError)",
        "Last sync line not found",
    ):
        assert preflight.stack_status_advisory(line, now=_at("2026-05-20T00:00:00")) is None, line


def test_stack_status_advisory_future_date_is_none() -> None:
    # clock skew / future-dated sync must NOT warn (negative age)
    assert preflight.stack_status_advisory(
        "Last sync: 2026-06-01", now=_at("2026-05-20T00:00:00")
    ) is None


def test_stack_status_advisory_invalid_date_is_none() -> None:
    # an ISO-shaped but invalid date (month 13) must not crash → no advisory
    assert preflight.stack_status_advisory(
        "Last sync: 2026-13-40", now=_at("2026-05-20T00:00:00")
    ) is None


def test_stack_status_advisory_rejects_overlong_numeric_runs() -> None:
    # audit f93ffaab gemini f1: a date embedded in a longer numeric run is
    # malformed → must NOT be silently truncated to a valid date (→ None).
    for malformed in ("Last sync: 2026-05-123", "Last sync: 12026-05-01",
                      "Last sync: 2026-05-019"):
        assert preflight.stack_status_advisory(
            malformed, now=_at("2030-01-01T00:00:00")
        ) is None, malformed


def test_stack_status_advisory_rendered_and_never_blocker() -> None:
    # integration: a far-stale STACK_STATUS.md surfaces the advisory line in the
    # report but NEVER leaks into the blocker decision (mirrors codemap advisory).
    original_git = preflight.git
    argv_backup = sys.argv
    try:
        preflight.git = _clean_repo_git()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "STACK_STATUS.md").write_text(
                "# Stack\n- Last sync: 2026-01-01\n", encoding="utf-8"
            )
            sys.argv = ["aqg_preflight.py", "--project-root", tmp, "--repo", tmp,
                        "--no-fetch", "--no-github"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = preflight.main()
        out = buf.getvalue()
        assert rc == 0, out
        assert "stack_status_advisory:" in out, out
        assert "advisory" in out.lower()
        decision = out.split("## Preflight decision", 1)[1]
        assert "stack_status_advisory" not in decision, decision
        assert "stack_status" not in decision.lower(), decision
    finally:
        preflight.git = original_git
        sys.argv = argv_backup


def test_concurrency_advisory_dirty_and_worktrees() -> None:
    # dirty at session start + >1 registered worktree both surface as advisory
    # notes (parallel-session rule PR #311).
    def fake_git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
        if args[:2] == ("worktree", "list"):
            return 0, (
                "worktree /a\nHEAD x\nbranch refs/heads/main\n\n"
                "worktree /b\nHEAD y\nbranch refs/heads/feat\n"
            )
        return 0, ""

    original = preflight.git
    try:
        preflight.git = fake_git
        notes = preflight.concurrency_advisory(Path("/tmp/x"), {"dirty": True})
        assert any("not this session" in n.lower() for n in notes), notes
        assert any("2 git worktrees" in n for n in notes), notes
    finally:
        preflight.git = original


def test_concurrency_advisory_clean_single_worktree_silent() -> None:
    # clean tree + single worktree -> nothing to advise (the common good case).
    def fake_git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
        if args[:2] == ("worktree", "list"):
            return 0, "worktree /a\nHEAD x\nbranch refs/heads/main\n"
        return 0, ""

    original = preflight.git
    try:
        preflight.git = fake_git
        assert preflight.concurrency_advisory(Path("/tmp/x"), {"dirty": False}) == []
    finally:
        preflight.git = original


def test_concurrency_advisory_rendered_and_never_blocker() -> None:
    # clean repo but multiple worktrees: the advisory renders in the report yet
    # NEVER leaks into the blocker decision (mirrors codemap / stack_status).
    original_git = preflight.git
    argv_backup = sys.argv

    def fake_git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
        if args[:2] == ("branch", "--show-current"):
            return 0, "main"
        if args[:3] == ("status", "--short", "--branch"):
            return 0, "## main...origin/main"  # clean
        if args[:4] == ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
            return 0, "origin/main"
        if args[:1] == ("rev-list",):
            return 0, "0\t0"
        if args[:2] == ("worktree", "list"):
            return 0, (
                "worktree /a\nHEAD x\nbranch refs/heads/main\n\n"
                "worktree /b\nHEAD y\nbranch refs/heads/feat\n"
            )
        return 0, ""

    try:
        preflight.git = fake_git
        with tempfile.TemporaryDirectory() as tmp:
            sys.argv = ["aqg_preflight.py", "--project-root", tmp, "--repo", tmp,
                        "--no-fetch", "--no-github"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = preflight.main()
        out = buf.getvalue()
        assert rc == 0, out
        assert "concurrency (advisory" in out.lower(), out
        assert "worktrees registered" in out, out
        decision = out.split("## Preflight decision", 1)[1]
        assert "none detected" in decision.lower(), decision
        assert "concurrency" not in decision.lower(), decision
    finally:
        preflight.git = original_git
        sys.argv = argv_backup


def test_concurrency_advisory_worktree_list_failure() -> None:
    # rc!=0 from `git worktree list` -> no worktree note and no crash; the dirty
    # note still fires independently. Also pins that --porcelain is passed.
    seen_args: list[tuple] = []

    def fake_git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
        seen_args.append(args)
        if args[:2] == ("worktree", "list"):
            return 128, "fatal: not a git repository"
        return 0, ""

    original = preflight.git
    try:
        preflight.git = fake_git
        notes = preflight.concurrency_advisory(Path("/tmp/x"), {"dirty": True})
        assert any("not this session" in n.lower() for n in notes), notes  # dirty survives
        # no worktree-COUNT note on failure (the dirty note's "git worktree add"
        # advice legitimately contains the word "worktree", so match the count phrase)
        assert not any("worktrees registered" in n for n in notes), notes
        assert ("worktree", "list", "--porcelain") in seen_args, seen_args
    finally:
        preflight.git = original


def test_concurrency_advisory_skipped_for_missing_repo() -> None:
    # a missing repo (exists=False) is rendered, but the concurrency advisory is
    # skipped at the call site — no worktree count misattributed from CWD.
    original_git = preflight.git
    argv_backup = sys.argv

    def fake_git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
        if args[:2] == ("worktree", "list"):
            return 0, "worktree /a\n\nworktree /b\n"  # would fire if the guard were absent
        return 0, ""

    try:
        preflight.git = fake_git
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            sys.argv = ["aqg_preflight.py", "--project-root", tmp, "--repo", str(missing),
                        "--no-fetch", "--no-github"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = preflight.main()
        out = buf.getvalue()
        assert rc == 0, out
        assert "exists: False" in out, out
        assert "concurrency (advisory" not in out, out
    finally:
        preflight.git = original_git
        sys.argv = argv_backup


# --------------------------------------------------------------------------
# WS-8 §10.1 — session-level dedup
# --------------------------------------------------------------------------
def test_dedup_recent_age_fresh() -> None:
    with tempfile.TemporaryDirectory() as sd:
        os.environ["AQG_PREFLIGHT_STATE_DIR"] = sd
        try:
            proj = Path("/some/project-a")
            preflight.dedup_touch(proj)
            age = preflight.dedup_recent_age(proj, time.time(), 300)
            assert age is not None and 0 <= age < 300, age
        finally:
            os.environ.pop("AQG_PREFLIGHT_STATE_DIR", None)


def test_dedup_recent_age_stale_returns_none() -> None:
    with tempfile.TemporaryDirectory() as sd:
        os.environ["AQG_PREFLIGHT_STATE_DIR"] = sd
        try:
            proj = Path("/some/project-b")
            preflight.dedup_touch(proj)
            m = preflight._dedup_marker_path(proj)
            old = time.time() - 10_000
            os.utime(m, (old, old))
            assert preflight.dedup_recent_age(proj, time.time(), 300) is None
        finally:
            os.environ.pop("AQG_PREFLIGHT_STATE_DIR", None)


def test_dedup_window_zero_disables() -> None:
    with tempfile.TemporaryDirectory() as sd:
        os.environ["AQG_PREFLIGHT_STATE_DIR"] = sd
        try:
            proj = Path("/some/project-c")
            preflight.dedup_touch(proj)
            assert preflight.dedup_recent_age(proj, time.time(), 0) is None
        finally:
            os.environ.pop("AQG_PREFLIGHT_STATE_DIR", None)


def test_dedup_no_marker_returns_none() -> None:
    with tempfile.TemporaryDirectory() as sd:
        os.environ["AQG_PREFLIGHT_STATE_DIR"] = sd
        try:
            assert preflight.dedup_recent_age(Path("/never/touched"), time.time(), 300) is None
        finally:
            os.environ.pop("AQG_PREFLIGHT_STATE_DIR", None)


def _clean_git_stub(cmd, cwd=None, timeout=30):
    joined = " ".join(cmd)
    if cmd[:1] == ["git"]:
        if "fetch" in cmd:
            return 0, ""
        if "--show-toplevel" in cmd:
            return 0, str(Path.cwd())
        if "--show-current" in cmd:
            return 0, "main"
        if "status" in cmd:
            return 0, "## main...origin/main"
        if "rev-list" in cmd:
            return 0, "0\t0"
        if "rev-parse" in cmd and "@{u}" in joined:
            return 0, "origin/main"
        if "remote" in cmd:
            return 0, "https://github.com/o/r.git"
        return 0, ""
    return 0, ""


def test_main_dedups_second_run_and_force_bypasses() -> None:
    # Only an AUTHORITATIVE (--force) run writes the marker; the redundant non-force
    # invocation dedups; a --force invocation always runs full.
    original_run = preflight.run
    argv_backup = sys.argv
    with tempfile.TemporaryDirectory() as sd, tempfile.TemporaryDirectory() as repo:
        os.environ["AQG_PREFLIGHT_STATE_DIR"] = sd
        os.environ["AQG_PREFLIGHT_DEDUP_WINDOW_S"] = "300"  # re-enable (module default is 0)
        base = ["aqg_preflight.py", "--project-root", repo, "--repo", repo, "--no-fetch", "--no-github"]
        try:
            preflight.run = _clean_git_stub
            # authoritative (--force) run: full report + writes marker
            sys.argv = list(base) + ["--force"]
            b1 = io.StringIO()
            with redirect_stdout(b1):
                assert preflight.main() == 0
            assert "Preflight decision" in b1.getvalue(), b1.getvalue()
            # redundant non-force run within window: deduped (no full report)
            sys.argv = list(base)
            b2 = io.StringIO()
            with redirect_stdout(b2):
                assert preflight.main() == 0
            out2 = b2.getvalue()
            assert "session dedup" in out2.lower(), out2
            assert "Preflight decision" not in out2, out2
            # --force again: full report (bypasses dedup)
            sys.argv = list(base) + ["--force"]
            b3 = io.StringIO()
            with redirect_stdout(b3):
                assert preflight.main() == 0
            assert "Preflight decision" in b3.getvalue(), b3.getvalue()
        finally:
            preflight.run = original_run
            sys.argv = argv_backup
            os.environ.pop("AQG_PREFLIGHT_STATE_DIR", None)
            os.environ["AQG_PREFLIGHT_DEDUP_WINDOW_S"] = "0"  # restore module default


def test_main_non_force_full_run_does_not_write_marker() -> None:
    # A non-force full run (headless skill-only, no redundant hook) must NOT leave a
    # marker that could wrongly skip a later cold start (audit 268db651 f2/f3).
    original_run = preflight.run
    argv_backup = sys.argv
    with tempfile.TemporaryDirectory() as sd, tempfile.TemporaryDirectory() as repo:
        os.environ["AQG_PREFLIGHT_STATE_DIR"] = sd
        os.environ["AQG_PREFLIGHT_DEDUP_WINDOW_S"] = "300"
        try:
            preflight.run = _clean_git_stub
            sys.argv = ["aqg_preflight.py", "--project-root", repo, "--repo", repo, "--no-fetch", "--no-github"]
            with redirect_stdout(io.StringIO()):
                assert preflight.main() == 0
            assert not preflight._dedup_marker_path(Path(repo).resolve()).exists()
        finally:
            preflight.run = original_run
            sys.argv = argv_backup
            os.environ.pop("AQG_PREFLIGHT_STATE_DIR", None)
            os.environ["AQG_PREFLIGHT_DEDUP_WINDOW_S"] = "0"


def test_dedup_future_mtime_not_fresh() -> None:
    with tempfile.TemporaryDirectory() as sd:
        os.environ["AQG_PREFLIGHT_STATE_DIR"] = sd
        try:
            proj = Path("/some/project-future")
            preflight.dedup_touch(proj)
            m = preflight._dedup_marker_path(proj)
            future = time.time() + 10_000
            os.utime(m, (future, future))
            # negative age (clock skew) must NOT be treated as fresh
            assert preflight.dedup_recent_age(proj, time.time(), 300) is None
        finally:
            os.environ.pop("AQG_PREFLIGHT_STATE_DIR", None)


def test_dedup_non_regular_marker_rejected() -> None:
    with tempfile.TemporaryDirectory() as sd:
        os.environ["AQG_PREFLIGHT_STATE_DIR"] = sd
        try:
            proj = Path("/some/project-dir-marker")
            m = preflight._dedup_marker_path(proj)
            m.parent.mkdir(parents=True, exist_ok=True)
            m.mkdir()  # a directory planted at the marker path is not a valid marker
            assert preflight.dedup_recent_age(proj, time.time(), 300) is None
        finally:
            os.environ.pop("AQG_PREFLIGHT_STATE_DIR", None)


def test_dedup_window_env_garbage_defaults_300() -> None:
    prior = os.environ.get("AQG_PREFLIGHT_DEDUP_WINDOW_S")
    try:
        os.environ["AQG_PREFLIGHT_DEDUP_WINDOW_S"] = "not-an-int"
        assert preflight._dedup_window_s() == 300
    finally:
        if prior is None:
            os.environ.pop("AQG_PREFLIGHT_DEDUP_WINDOW_S", None)
        else:
            os.environ["AQG_PREFLIGHT_DEDUP_WINDOW_S"] = prior


TESTS = [
    test_missing_path_blocker,
    test_status_failure_not_dirty,
    test_fetch_failure_is_blocker,
    test_revlist_failure_is_blocker,
    test_recent_commits_captured,
    test_recent_commits_empty_repo_graceful,
    test_recent_commits_sanitized,
    test_recent_commits_fence_safe_backticks,
    test_gh_lines_reports_failure,
    test_main_decision_not_clean_on_gh_failure,
    test_last_sync_handles_directory,
    test_context_file_status_directory_not_ok,
    test_origin_url_redacts_credentials,
    test_github_origin_redacts,
    test_sanitize_strips_control_and_ansi,
    test_required_file_rejects_absolute_and_traversal,
    test_is_meta_file_classification,
    test_codemap_status_missing_and_present,
    test_codemap_status_codemap_no_underscore_variant,
    test_codemap_status_all_meta_zero_clutter,
    test_codemap_status_listdir_failure_reports_unknown,
    test_codemap_status_clutter_flagged,
    test_codemap_advisory_is_never_a_blocker,
    test_codemap_advisory_absent_without_flag,
    test_stack_status_advisory_stale,
    test_stack_status_advisory_fresh_is_none,
    test_stack_status_advisory_boundary_7d,
    test_stack_status_advisory_no_date_is_none,
    test_stack_status_advisory_future_date_is_none,
    test_stack_status_advisory_invalid_date_is_none,
    test_stack_status_advisory_rejects_overlong_numeric_runs,
    test_stack_status_advisory_rendered_and_never_blocker,
    test_concurrency_advisory_dirty_and_worktrees,
    test_concurrency_advisory_clean_single_worktree_silent,
    test_concurrency_advisory_rendered_and_never_blocker,
    test_concurrency_advisory_worktree_list_failure,
    test_concurrency_advisory_skipped_for_missing_repo,
    test_dedup_recent_age_fresh,
    test_dedup_recent_age_stale_returns_none,
    test_dedup_window_zero_disables,
    test_dedup_no_marker_returns_none,
    test_main_dedups_second_run_and_force_bypasses,
    test_main_non_force_full_run_does_not_write_marker,
    test_dedup_future_mtime_not_fresh,
    test_dedup_non_regular_marker_rejected,
    test_dedup_window_env_garbage_defaults_300,
]


if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"OK: aqg_preflight self-test passed ({len(TESTS)} tests)")
