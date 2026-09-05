"""Behavioral regression: sessionstart_preflight.sh must emit the preflight
decision summary on the hook's STDOUT (the SessionStart context channel), not
only on stderr.

Why this exists: the hook used to redirect its whole preflight-summary python
block with `<<'PYEOF' >&2`, sending the decision summary (ahead/behind, dirty,
blockers) to stderr only. SessionStart injects a hook's STDOUT into Claude's
context — stderr is NOT injected (it lands in debug logs and is rendered red by
many terminals). Net effect: Claude started sessions blind to "behind N / dirty
/ blocker", and the user saw spurious red text. This test pins the fix: a fake
preflight prints a unique marker to ITS stdout; the hook must surface that marker
on the hook's stdout, while keeping meta/reminder framing on stderr.

Stream-json safety (2026-06): the summary is emitted as a JSON
hookSpecificOutput.additionalContext object, not plain text. A SessionStart hook's
plain-text stdout corrupts `--input-format stream-json` / headless claude — the
parser reads it as a malformed event line and aborts the session with no output.
test_stdout_is_valid_json_additionalcontext pins that the stdout is valid JSON.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# tests/behavior/<file> -> parents[2] == repo root
REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "agent-packs/claude-code/hooks/sessionstart_preflight.sh"
MARKER = "PREFLIGHT_SUMMARY_MARKER_a1b2c3"


def test_hook_invokes_preflight_with_force() -> None:
    # WS-8 §10.1: the SessionStart hook is the per-session dedup authority — it MUST
    # pass --force so every real session start runs fresh + refreshes the marker (a
    # rapid restart is not deduped by a prior session), while the redundant rule-driven
    # skill invocation is the one that short-circuits.
    src = HOOK.read_text(encoding="utf-8")
    assert '"--force"' in src, "sessionstart hook must pass --force to aqg_preflight.py"
    # --project-root keys the dedup marker on the same resolved root the skill uses,
    # so the hook and the rule-driven skill invocation agree and dedup matches
    # (audit 268db651 f1/f3 — a --repo-only key can diverge from the skill's cwd root).
    assert '"--project-root"' in src, "sessionstart hook must pass --project-root for dedup key alignment"


def _make_fake_aqg_root(root: Path) -> None:
    """Fake AQG checkout whose preflight prints MARKER to stdout, no network."""
    script_dir = root / "skills" / "aqg-startup-preflight" / "scripts"
    script_dir.mkdir(parents=True)
    # Ignores argv (the hook passes --repo <dir>); just proves stdout is surfaced.
    (script_dir / "aqg_preflight.py").write_text(
        "import sys\n"
        f"print({MARKER!r})\n"
        "sys.exit(0)\n"
    )


def _make_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)


def _run_hook(aqg_root: Path, project_dir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "AQG_ROOT": str(aqg_root),
           "CLAUDE_PROJECT_DIR": str(project_dir)}
    return subprocess.run(
        ["bash", str(HOOK), str(project_dir)],
        env=env, text=True, capture_output=True, timeout=30,
    )


def test_preflight_summary_reaches_hook_stdout(tmp_path: Path) -> None:
    aqg_root = tmp_path / "aqg"
    project = tmp_path / "proj"
    _make_fake_aqg_root(aqg_root)
    _make_git_repo(project)

    proc = _run_hook(aqg_root, project)

    # SessionStart hooks must never fail the session.
    assert proc.returncode == 0, (
        f"hook must exit 0; got {proc.returncode}\nstderr:\n{proc.stderr}"
    )
    # Core invariant: the decision summary reaches the context channel (stdout).
    assert MARKER in proc.stdout, (
        "preflight decision summary must reach the hook's STDOUT (SessionStart "
        "context channel); it was missing.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    # Channel separation: meta/reminder framing stays on stderr (not stdout).
    assert "[aqg session-preflight] reminder:" in proc.stderr, (
        "meta reminder line should remain on stderr (framing noise), keeping "
        f"stdout for the decision summary.\nstderr={proc.stderr!r}"
    )


def test_stdout_is_valid_json_additionalcontext(tmp_path: Path) -> None:
    """Stream-json safety: the hook's STDOUT must be a single valid JSON object using
    the SessionStart hookSpecificOutput.additionalContext form. Plain text would be
    read as a malformed event line by `--input-format stream-json` / headless claude
    and abort the session with no init/assistant/result."""
    aqg_root = tmp_path / "aqg"
    project = tmp_path / "proj"
    _make_fake_aqg_root(aqg_root)
    _make_git_repo(project)

    proc = _run_hook(aqg_root, project)

    assert proc.returncode == 0, proc.stderr
    # The whole point of the fix: stdout parses as JSON (never raw text).
    obj = json.loads(proc.stdout)
    hso = obj["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart", obj
    assert MARKER in hso["additionalContext"], (
        f"preflight summary must ride in additionalContext.\nobj={obj!r}"
    )


def _write_fake_preflight(root: Path, body: str) -> None:
    """Install a fake preflight with an arbitrary body under a tmp AQG_ROOT."""
    script_dir = root / "skills" / "aqg-startup-preflight" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "aqg_preflight.py").write_text(body)


def test_nonzero_exit_no_output_reports_state_unknown(tmp_path: Path) -> None:
    """Audit 046e4118 f1: preflight crashing with no output must not look like a
    clean empty summary — the hook surfaces a state-unknown line on stdout while
    still exiting 0 (SessionStart must never fail the session)."""
    aqg_root = tmp_path / "aqg"
    project = tmp_path / "proj"
    _write_fake_preflight(aqg_root, "import sys\nsys.exit(1)\n")  # no stdout/stderr
    _make_git_repo(project)

    proc = _run_hook(aqg_root, project)

    assert proc.returncode == 0, (
        f"hook must exit 0 even when preflight crashes; got {proc.returncode}"
    )
    assert "state unknown" in proc.stdout, (
        "a non-zero preflight exit with no output must surface a state-unknown "
        f"line on the hook's stdout (context channel).\nstdout={proc.stdout!r}"
    )


def test_preflight_child_stderr_reaches_hook_stdout(tmp_path: Path) -> None:
    """The wrapper merges child stdout+stderr; since SessionStart injects only the
    hook's stdout, a preflight that writes to ITS stderr must still reach the
    hook's stdout (otherwise diagnostics would be lost from context)."""
    aqg_root = tmp_path / "aqg"
    project = tmp_path / "proj"
    err_marker = "CHILD_STDERR_MARKER_d4e5f6"
    _write_fake_preflight(
        aqg_root,
        "import sys\n"
        f"print({err_marker!r}, file=sys.stderr)\n"
        "sys.exit(0)\n",
    )
    _make_git_repo(project)

    proc = _run_hook(aqg_root, project)

    assert proc.returncode == 0
    assert err_marker in proc.stdout, (
        "preflight child stderr must be merged into the hook's stdout (context "
        f"channel).\nstdout={proc.stdout!r}"
    )


def test_skip_path_emits_exactly_one_json_object_and_exit0(tmp_path: Path) -> None:
    """A skip path still exits 0 and still may not put PLAIN TEXT on stdout.

    This assertion used to read "stdout must be empty". That encoded a behaviour
    deliberately changed: a session whose preflight cannot run is exactly the
    session most likely to be flying blind, so the discipline lines are now
    emitted on the skip paths too. The invariant BEHIND the old assertion is the
    one that mattered and it is asserted more strongly here -- stdout is either
    empty or ONE valid JSON object, because plain text corrupts a stream-json
    session and two concatenated objects raise `Extra data` in every reader in
    this repo, which would destroy the summary rather than add to it.
    """
    project = tmp_path / "proj"
    _make_git_repo(project)
    env = {k: v for k, v in os.environ.items() if k != "AQG_ROOT"}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    proc = subprocess.run(
        ["bash", str(HOOK), str(project)],
        env=env, text=True, capture_output=True, timeout=30,
    )

    assert proc.returncode == 0, f"skip path must exit 0; got {proc.returncode}"
    assert "AQG_ROOT not set" in proc.stderr, (
        f"skip reason should be on stderr.\nstderr={proc.stderr!r}"
    )

    payload = json.loads(proc.stdout)  # raises on plain text OR on a second object
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "[aqg discipline]" in context, (
        "the skip path must still carry the discipline; a session told nothing at "
        f"all reads as one that was checked and found clean.\ncontext={context!r}"
    )
    assert "invoke the aqg-code-construction skill" in context
    # With no AQG_ROOT the policy cannot be read, so the pointer must say so
    # rather than fabricate a path.
    assert "Ladder + full list: UNRESOLVED" in context


def test_policy_reader_missing_degrades_with_an_address_not_a_shrug(tmp_path: Path) -> None:
    """The failure path an auditor said was untestable, and was self-contradictory.

    The first version told the reader to "read the file directly" and then gave
    the location as UNRESOLVED two lines down -- advice with no address
    (aud_NinV9t0Cx3GiQxzh opus-f5, feat-f3). An AQG_ROOT that resolves but has no
    policy reader now names the conventional path and marks it unverified, and
    the entry-point line survives regardless.
    """
    fake_root = tmp_path / "fake"
    (fake_root / "scripts").mkdir(parents=True)
    project = tmp_path / "proj"
    _make_git_repo(project)

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["AQG_ROOT"] = str(fake_root)
    proc = subprocess.run(
        ["bash", str(HOOK), str(project)],
        env=env, text=True, capture_output=True, timeout=30,
    )

    assert proc.returncode == 0
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "invoke the aqg-code-construction skill" in context, (
        "the entry-point line needs no file and must survive every degradation"
    )
    assert "policy reader not installed at" in context, "name the failure, do not swallow it"
    assert f"{fake_root}/docs/policies/audit-trigger.md (unverified)" in context, (
        "a reader told to open the file must be given its address"
    )
    assert "UNRESOLVED" not in context, (
        "UNRESOLVED is only for the case where AQG_ROOT itself is unset"
    )


def test_discipline_survives_a_non_git_directory(tmp_path: Path) -> None:
    """The case the whole P4 change exists for.

    A non-git cwd used to `exit 0` before anything was emitted, so an agent in a
    scratch directory got no discipline at all. It now gets the full block, read
    from the policy's own published clauses, in one JSON object.
    """
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    env["AQG_ROOT"] = str(REPO)
    proc = subprocess.run(
        ["bash", str(HOOK), str(tmp_path)],
        env=env, text=True, capture_output=True, timeout=30,
    )

    assert proc.returncode == 0
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "preflight did not run (not-a-worktree)" in context, (
        "an unknown state must be stated, not left blank"
    )
    assert "invoke the aqg-code-construction skill" in context
    # Both policy sentences, read from the policy rather than restated here.
    assert "audit is the exception, not the reflex" in context
    assert "deep regardless of size" in context
    assert "Ladder + full list: UNRESOLVED" not in context, (
        "AQG_ROOT resolves here, so the pointer must be a real path"
    )


def test_non_utf8_preflight_output_keeps_summary(tmp_path: Path) -> None:
    """Audit 7f7b8a48 claude f2: a repo emitting non-UTF-8 bytes (e.g. a git status with
    non-UTF-8 filenames) must NOT discard the whole preflight summary. Without
    errors='replace' the UnicodeDecodeError (subset of ValueError) is swallowed and the
    summary is replaced by a generic 'invocation error' line — letting a crafted repo
    suppress all preflight warnings. With the fix the decodable content survives."""
    aqg_root = tmp_path / "aqg"
    project = tmp_path / "proj"
    _write_fake_preflight(
        aqg_root,
        "import sys\n"
        r"sys.stdout.buffer.write(b'PREFLIGHT_OK \xff\xfe done\n')" + "\n"
        "sys.exit(0)\n",
    )
    _make_git_repo(project)

    proc = _run_hook(aqg_root, project)

    assert proc.returncode == 0, proc.stderr
    obj = json.loads(proc.stdout)  # still valid JSON despite the non-UTF8 bytes
    ctx = obj["hookSpecificOutput"]["additionalContext"]
    assert "PREFLIGHT_OK" in ctx and "invocation error" not in ctx, (
        "decodable preflight content must survive non-UTF8 bytes (errors=replace), not "
        f"be replaced by a generic error line.\nobj={obj!r}"
    )


def test_cwd_scope_hint_in_additionalcontext(tmp_path: Path) -> None:
    """Owner 2026-06-11 (B): the auto-preflight only covers the session cwd. When the
    work repo differs (a handoff points to another repo) the model must re-invoke the
    skill on the work repo, not do manual git checks — the injected summary carries that
    cwd-scope hint (root cause of 'auto-preflight ran on the wrong repo')."""
    aqg_root = tmp_path / "aqg"
    project = tmp_path / "proj"
    _make_fake_aqg_root(aqg_root)
    _make_git_repo(project)

    proc = _run_hook(aqg_root, project)

    assert proc.returncode == 0, proc.stderr
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "仅覆盖当前 cwd" in ctx
    assert str(project) in ctx                          # names the cwd repo
    assert "重跑" in ctx and "invoke" in ctx             # nudges re-invoking the skill


def test_cwd_path_with_backtick_is_sanitized(tmp_path: Path) -> None:
    """Audit 2c4654ce f2: a backtick in the cwd path must not survive into the injected
    hint — it would break the inline code span (prompt-injection via dir name). The hook
    replaces it before the f-string."""
    aqg_root = tmp_path / "aqg"
    project = tmp_path / "pr`oj"   # backtick in the dir name
    _make_fake_aqg_root(aqg_root)
    _make_git_repo(project)

    proc = _run_hook(aqg_root, project)

    assert proc.returncode == 0, proc.stderr
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    hint_line = [ln for ln in ctx.splitlines() if "仅覆盖当前 cwd" in ln][0]
    assert "pr`oj" not in hint_line     # the path's raw backtick is gone
    assert "pr'oj" in hint_line         # replaced with a single quote
