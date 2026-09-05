"""Behavior contracts for the update check that runs itself.

docs/UPDATE_ARCHITECTURE.md §10 and §13 (PR6). This is the step that turns the
machinery on, and it runs unattended at session start on every install. Its
contract is therefore mostly about what it must NOT do:

* it must never raise and never exit non-zero, whatever happens inside it — a
  traceback at session start is a broken shell, and a non-zero exit from a hook
  is a session that reports a problem the user cannot act on;
* it must never write to stdout, because a SessionStart hook's stdout is a JSON
  channel and plain text there aborts a headless session;
* it must be inert until a keyring ships, so the automatic channel opens when
  the Owner lands a key rather than when this code merges.

Everything it *does* do is recorded, because a silent automatic system with no
record is one nobody can debug after the fact.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.aqg_update import run as run_mod


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("AQG_STATE_ROOT", str(root))
    return root


def _last(state_root: Path):
    path = state_root / run_mod.LAST_CHECK_FILENAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# --- the gates, in the order they are checked -------------------------------------------


def test_the_kill_switch_stops_everything(tmp_path, state_root, monkeypatch):
    """§10. A user who has turned this off must not have a fetch attempted at
    them, so the switch is checked before anything else."""
    monkeypatch.setenv(run_mod.KILL_SWITCH, "1")
    result = run_mod.check(root=tmp_path, remote="origin", channel="stable")
    assert result.outcome == "disabled"


def test_a_build_with_no_pinned_keyring_does_nothing(tmp_path, state_root):
    """The automatic channel opens when a key ships, not when this code merges.

    There is no `release-trust.json` yet, so every install that takes this
    release runs a check that stops here — which is the intended state, not a
    failure to report.
    """
    result = run_mod.check(
        root=tmp_path, remote="origin", channel="stable",
        keyring_path=tmp_path / "absent.json",
    )
    assert result.outcome == "no-keyring"


def test_a_check_that_ran_recently_is_not_repeated(tmp_path, state_root, monkeypatch):
    """Sessions start many times a day; a remote does not need telling."""
    run_mod.record(run_mod.CheckResult(outcome="current"), state_root=state_root)
    result = run_mod.check(
        root=tmp_path, remote="origin", channel="stable",
        keyring_path=tmp_path / "absent.json",
    )
    assert result.outcome == "too-soon"


def test_an_old_check_does_not_suppress_a_new_one(tmp_path, state_root, monkeypatch):
    stale = run_mod.CheckResult(outcome="current")
    run_mod.record(stale, state_root=state_root, at=0.0)
    result = run_mod.check(
        root=tmp_path, remote="origin", channel="stable",
        keyring_path=tmp_path / "absent.json",
    )
    assert result.outcome == "no-keyring"


# --- what must never happen ---------------------------------------------------------------


def test_an_unexpected_failure_is_recorded_and_swallowed(tmp_path, state_root, monkeypatch):
    """A traceback out of a session-start hook is a broken shell.

    The failure is not hidden — it lands in the record `doctor` reads — but it
    does not reach the user's terminal as a crash.
    """
    def explode(*args, **kwargs):
        raise RuntimeError("the remote did something nobody expected")

    monkeypatch.setattr(run_mod.acquire, "available_release", explode)
    keyring = _write_keyring(tmp_path)
    result = run_mod.check(
        root=tmp_path, remote="origin", channel="stable", keyring_path=keyring
    )
    assert result.outcome == "failed"
    assert "nobody expected" in result.detail
    assert _last(state_root)["outcome"] == "failed"


def test_main_always_exits_zero(tmp_path, state_root, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(run_mod, "check", explode)
    assert run_mod.main([]) == 0


def test_main_writes_nothing_to_stdout(tmp_path, state_root, monkeypatch, capsys):
    """A SessionStart hook's stdout is a JSON channel: plain text there is read
    as a malformed event and aborts a headless session."""
    monkeypatch.setenv(run_mod.KILL_SWITCH, "1")
    run_mod.main([])
    assert capsys.readouterr().out == ""


def test_the_record_survives_a_check_that_did_nothing(tmp_path, state_root):
    """Otherwise `doctor` cannot tell "checked, nothing to do" from "never
    checked", and the second is the one worth acting on."""
    run_mod.check(
        root=tmp_path, remote="origin", channel="stable",
        keyring_path=tmp_path / "absent.json",
    )
    recorded = _last(state_root)
    assert recorded["outcome"] == "no-keyring"
    assert recorded["checked_at"] > 0


# --- reporting ------------------------------------------------------------------------------


def test_doctor_reports_a_recorded_failure(tmp_path, state_root):
    run_mod.record(
        run_mod.CheckResult(outcome="failed", detail="signature refused"),
        state_root=state_root,
    )
    lines = run_mod.report(state_root=state_root)
    assert any("failed" in line for line in lines)
    assert any("signature refused" in line for line in lines)


def test_doctor_says_so_when_no_check_has_ever_run(tmp_path, state_root):
    lines = run_mod.report(state_root=state_root)
    assert any("never" in line.lower() for line in lines)


def test_doctor_lists_what_is_pending(tmp_path, state_root):
    """§7: hook-set membership is the one payload class that mutates a host's
    own config, and no adapter can apply it yet. It is deferred rather than
    silently skipped, so it has to be visible somewhere."""
    run_mod.record(
        run_mod.CheckResult(
            outcome="applied",
            pending=("claude-code: hook set membership changed",),
        ),
        state_root=state_root,
    )
    lines = run_mod.report(state_root=state_root)
    assert any("hook set membership" in line for line in lines)


def _write_keyring(tmp_path: Path) -> Path:
    fixture = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "release_test_key.json").read_text(
            encoding="utf-8"
        )
    )
    path = tmp_path / "release-trust.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "keys": [
                    {
                        "key_id": fixture["key_id"],
                        "algorithm": "rsa-pkcs1v15-sha256",
                        "modulus_hex": fixture["modulus_hex"],
                        "exponent": fixture["exponent"],
                        "revoked": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


# --- the trigger itself ----------------------------------------------------------------------


HOOK = (
    Path(__file__).resolve().parents[2]
    / "agent-packs/claude-code/hooks/sessionstart_update_check.sh"
)


def _hook_code() -> str:
    """The launcher's executable lines, with comments stripped."""
    return "\n".join(
        line for line in HOOK.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_the_hook_is_a_thin_launcher(tmp_path):
    """The hook is a trigger, not the update. Everything it does has to be
    cheap enough to happen before a prompt appears."""
    body = [
        line for line in HOOK.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(body) <= 20, f"the launcher has grown to {len(body)} lines"


def test_the_hook_writes_nothing_to_stdout_and_exits_zero(tmp_path, monkeypatch):
    """Both halves are load-bearing: a non-zero exit is a session that reports a
    problem nobody can act on, and stdout is the JSON channel."""
    done = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True,
        env={**os.environ, run_mod.KILL_SWITCH: "1", "PATH": os.environ.get("PATH", "")},
        timeout=30,
    )
    assert done.returncode == 0
    assert done.stdout == b""


def test_the_hook_returns_immediately(tmp_path, monkeypatch):
    """The check is detached. A hook that waited for a fetch would put a network
    round trip in front of every session."""
    import time

    started = time.monotonic()
    subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True,
        env={**os.environ, "AQG_ROOT": str(Path(__file__).resolve().parents[2])},
        timeout=30,
    )
    assert time.monotonic() - started < 5.0


# --- the end to end -------------------------------------------------------------------------


def _install(tmp_path: Path):
    """An install shaped the way §5 says: a symlinked root over a versions dir.

    Built out of a real git repository, because the swap, the staging and the
    planner all talk to git — a mocked one would test the wiring against a
    fiction of it.
    """
    origin = tmp_path / "origin"
    origin.mkdir()

    def git(repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=True
        )

    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@example.com")
    git(origin, "config", "user.name", "T")
    (origin / "scripts").mkdir()
    (origin / "scripts" / "_aqg_context.sh").write_text("# helper\n", encoding="utf-8")
    # The planner refuses a tree with no skills/ roster rather than reading it as
    # "no skills", which would plan a prune of every routed skill on every host.
    (origin / "skills" / "aqg-demo").mkdir(parents=True)
    (origin / "skills" / "aqg-demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (origin / "VERSION").write_text("0.16.0\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "v1")
    first = git(origin, "rev-parse", "HEAD").stdout.decode().strip()

    (origin / "VERSION").write_text("0.17.0\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "v2")
    second = git(origin, "rev-parse", "HEAD").stdout.decode().strip()

    install = tmp_path / "install"
    versions = install / "versions"
    versions.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(versions / first)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(versions / first), "checkout", "-q", "--detach", first],
        capture_output=True, check=True,
    )
    root = install / "aqg"
    os.symlink(versions / first, root)
    return root, first, second


def _clean_plan(monkeypatch, target_commit):
    """Force a plan that touches no host configuration.

    The planner is exercised by its own suite; what these tests are about is
    everything after it. A real fixture cannot produce a clean plan here — the
    adapters see this machine's actual hosts, so a skill in the fixture tree
    produces a route for every one of them.
    """
    def only_activate(**kwargs):
        return run_mod.plan_mod.Plan(
            actions=(
                run_mod.plan_mod.Action(
                    kind="activate_root", client_id=None, subject=None,
                    payload_class=0, detail=f"activate {target_commit[:12]}",
                ),
            ),
            deferred=(),
        )

    monkeypatch.setattr(run_mod.plan_mod, "build_plan", only_activate)

def test_a_verified_release_is_staged_swapped_and_recorded(tmp_path, state_root, monkeypatch):
    """The whole point of the feature, end to end.

    `acquire` is stubbed with a release it has already verified — its own
    refusals are covered by `test_update_acquire.py`, and repeating them here
    would test that module twice and this one not at all. What is under test is
    everything after: stage, plan, apply, swap, record.
    """
    root, first, second = _install(tmp_path)

    class _Found:
        manifest = {"version": "0.17.0", "release_sequence": 9}
        commit = second
        key_id = "k"
        release_sequence = 9

    monkeypatch.setattr(run_mod.acquire, "available_release", lambda *a, **k: _Found())
    _clean_plan(monkeypatch, second)
    keyring = _write_keyring(tmp_path)

    result = run_mod.check(
        root=root, remote="origin", channel="stable",
        keyring_path=keyring, state_root=state_root,
    )
    assert result.outcome == "applied", result.detail
    assert os.path.realpath(root) == str((tmp_path / "install/versions" / second).resolve())
    assert (Path(os.path.realpath(root)) / "VERSION").read_text().strip() == "0.17.0"
    assert _last(state_root)["outcome"] == "applied"


def test_a_release_that_fails_its_smoke_check_is_rolled_back(
    tmp_path, state_root, monkeypatch
):
    """The swap is the one action this layer can invert, and the smoke check is
    what decides whether to. A root that cannot bootstrap itself — no context
    helper — must not be left live."""
    root, first, second = _install(tmp_path)

    class _Found:
        manifest = {"version": "0.17.0", "release_sequence": 9}
        commit = second
        key_id = "k"
        release_sequence = 9

    monkeypatch.setattr(run_mod.acquire, "available_release", lambda *a, **k: _Found())
    _clean_plan(monkeypatch, second)
    monkeypatch.setattr(run_mod, "_smoke", lambda target: False)

    result = run_mod.check(
        root=root, remote="origin", channel="stable",
        keyring_path=_write_keyring(tmp_path), state_root=state_root,
    )
    assert result.outcome == "rolled-back"
    assert os.path.realpath(root) == str((tmp_path / "install/versions" / first).resolve())


def test_the_smoke_check_fails_a_tree_with_no_context_helper(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_mod._smoke(empty) is False
    (empty / "scripts").mkdir()
    (empty / "scripts" / "_aqg_context.sh").write_text("#\n", encoding="utf-8")
    assert run_mod._smoke(empty) is True


# --- the hook is actually installed -----------------------------------------------------------


def test_the_update_check_is_in_the_managed_hook_set():
    """§13: this is the PR that turns automation on, and a hook nobody installs
    turns nothing on.

    Adding a member to the hook set is itself a payload-class-5 change (§1) — the
    one class that mutates a host's own configuration — so it is asserted here
    rather than left to be noticed.
    """
    import scripts.install_aqg_hooks as installer

    specs = installer._aqg_hook_specs(Path(__file__).resolve().parents[2])
    commands = [
        hook["command"]
        for group in specs["SessionStart"]
        for hook in group["hooks"]
    ]
    assert any("sessionstart_update_check.sh" in command for command in commands)


def test_the_update_check_hook_is_not_blocking():
    """A SessionStart hook that can fail the session is a session that a slow
    remote can stop from starting."""
    import scripts.install_aqg_hooks as installer

    assert "sessionstart_update_check.sh" not in installer._AQG_BLOCKING_HOOK_SCRIPTS


def test_doctor_surfaces_the_update_check(tmp_path, state_root):
    """The automatic path is silent by design, so `doctor` is the only place a
    user can find out what it has been doing."""
    import scripts.aqg_doctor as doctor

    run_mod.record(
        run_mod.CheckResult(outcome="failed", detail="signature refused"),
        state_root=state_root,
    )
    results = doctor.check_update_channel()
    assert any("signature refused" in r.detail for r in results)


def test_an_install_predating_the_version_layout_is_refused_not_guessed_at(
    tmp_path, state_root, monkeypatch
):
    """Every install in the field today is a plain checkout, not a symlink.

    The versions directory is derived from where the root POINTS. On a root that
    points nowhere — a real directory — deriving it from the path instead would
    silently pick that checkout's own parent and stage a sibling of the user's
    repository. It has to refuse and say what to do.
    """
    plain = tmp_path / "plain-checkout"
    (plain / "scripts").mkdir(parents=True)

    class _Found:
        manifest = {"version": "0.17.0", "release_sequence": 9}
        commit = "a" * 40
        key_id = "k"
        release_sequence = 9

    monkeypatch.setattr(run_mod.acquire, "available_release", lambda *a, **k: _Found())
    result = run_mod.check(
        root=plain, remote="origin", channel="stable",
        keyring_path=_write_keyring(tmp_path), state_root=state_root,
    )
    assert result.outcome == "failed"
    assert "migrated by hand" in result.detail
    assert not list(tmp_path.glob("versions")), "it staged into a guessed directory"


def test_a_record_that_cannot_be_written_does_not_fail_the_check(
    tmp_path, state_root, monkeypatch
):
    """The record is how a silent system is debugged, but it is not the check.

    A state directory that cannot be written — a full disk, a permission the
    user changed — must not turn an update that worked into one reported as
    failed.
    """
    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(run_mod, "_atomic_write", refuse)
    result = run_mod.check(
        root=tmp_path, remote="origin", channel="stable",
        keyring_path=tmp_path / "absent.json", state_root=state_root,
    )
    assert result.outcome == "no-keyring"
    assert _last(state_root) is None


# =============================================================================
# Added after audit aud_VSIiTCkVDGwpL4WG (2 × fundamentally-flawed). See
# .aqg/adjudication/. Two independent defects meant the automatic channel never
# ran at all, and every trigger test was green.
# =============================================================================


def test_the_launcher_actually_starts_a_check(tmp_path, monkeypatch):
    """The test that was missing, and the one that mattered.

    The other three assert the launcher's prohibitions — exits zero, silent
    stdout, returns fast — and a launcher that does *nothing* satisfies all
    three perfectly. Two independent defects made it do exactly that: `setsid`
    does not exist on macOS, and running `run.py` as a script puts
    `scripts/aqg_update` on `sys.path`, where neither package root resolves.
    """
    import time

    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "AQG_ROOT": str(Path(__file__).resolve().parents[2]),
        "AQG_STATE_ROOT": str(state),
    }
    env.pop(run_mod.KILL_SWITCH, None)
    subprocess.run(["bash", str(HOOK)], capture_output=True, env=env, timeout=30)

    deadline = time.time() + 25
    record_path = state / run_mod.LAST_CHECK_FILENAME
    while time.time() < deadline and not record_path.exists():
        time.sleep(0.25)
    assert record_path.exists(), (
        "the launcher returned but no check ever ran; the detached process died "
        "or was never started"
    )


def test_the_launcher_scrubs_the_environment_it_hands_over(tmp_path):
    """The updater is what checks signatures, so anything that can steer its
    interpreter defeats every check after it.

    `PYTHONPATH` is the obvious one, and AQG has a tamper guard watching the
    checkout and nothing watching the environment — so this is a way around a
    control that exists rather than a restatement of one that does not.
    """
    # Non-comment lines only. The first version of this read the whole file and
    # was tripped by the comment explaining why PYTHONPATH is dangerous — a test
    # that fails on its own documentation.
    body = _hook_code()
    assert "env -i" in body, "the child inherits the caller's environment"
    assert "-E" in body, "PYTHON* variables are not ignored"
    assert "PYTHONPATH" not in body, "PYTHONPATH is forwarded"


def test_the_launcher_does_not_depend_on_setsid(tmp_path):
    """util-linux, absent on macOS. The command failed, `|| true` swallowed it,
    and nothing ran — on the platform this repository is developed on."""
    assert "setsid" not in _hook_code()


# --- the design correction ---------------------------------------------------------------


def test_an_incomplete_plan_is_not_applied_at_all(tmp_path, state_root, monkeypatch):
    """The critical finding, and the reason this slice's design changed.

    Swapping the root while holding skill or hook work back leaves the host
    describing the OLD tree while serving the NEW one. Every AQG hook command
    ends in `|| true`, so a hook whose script vanished from under the root stops
    running and says nothing — the guardrail is gone, silently. So a plan that
    is not complete is not applied.
    """
    root, first, second = _install(tmp_path)

    class _Found:
        manifest = {"version": "0.17.0", "release_sequence": 9}
        commit = second
        key_id = "k"
        release_sequence = 9

    monkeypatch.setattr(run_mod.acquire, "available_release", lambda *a, **k: _Found())

    real_build = run_mod.plan_mod.build_plan

    def with_a_route(**kwargs):
        built = real_build(**kwargs)
        extra = run_mod.plan_mod.Action(
            kind="route_skill", client_id="claude-code", subject="aqg-demo",
            payload_class=3, detail="a skill was added upstream",
        )
        return run_mod.plan_mod.Plan(
            actions=built.actions + (extra,), deferred=built.deferred
        )

    monkeypatch.setattr(run_mod.plan_mod, "build_plan", with_a_route)
    result = run_mod.check(
        root=root, remote="origin", channel="stable",
        keyring_path=_write_keyring(tmp_path), state_root=state_root,
    )
    assert result.outcome == "pending"
    assert any("aqg-demo" in item for item in result.pending)
    assert os.path.realpath(root) == str(
        (tmp_path / "install/versions" / first).resolve()
    ), "the root was swapped despite work being held back"


def test_a_pending_notice_is_not_erased_by_the_next_check(tmp_path, state_root):
    """The only surface telling a human that class-5 work is outstanding lived
    in a single-slot record the next check overwrote. A notice that expires on
    its own is worse than none: its absence reads as "nothing outstanding"."""
    run_mod.record(
        run_mod.CheckResult(outcome="pending", pending=("claude-code: hook set changed",)),
        state_root=state_root,
    )
    run_mod.record(run_mod.CheckResult(outcome="current"), state_root=state_root)
    assert any(
        "hook set changed" in item for item in (_last(state_root)["pending"] or [])
    )


def test_a_future_timestamp_does_not_suppress_every_check_forever(
    tmp_path, state_root
):
    """The record is a file the user can write. Unbounded, a timestamp a year
    ahead turns the check off permanently and silently."""
    run_mod.record(
        run_mod.CheckResult(outcome="current"), state_root=state_root,
        at=9_999_999_999.0,
    )
    result = run_mod.check(
        root=tmp_path, remote="origin", channel="stable",
        keyring_path=tmp_path / "absent.json", state_root=state_root,
    )
    assert result.outcome == "no-keyring"


def test_a_stalled_run_still_rate_limits_the_next_session(tmp_path, state_root, monkeypatch):
    """The record used to be written only on completion, so a run that never
    finished never rate-limited — and every later session started another one.

    A SIGKILL cannot be simulated in-process, so what is asserted is the
    ORDERING that makes surviving one possible: the record already exists at the
    moment the network is first touched. The first version of this test raised
    `KeyboardInterrupt` instead, which the runner's own boundary catches — so it
    exercised the failure path and passed with the in-flight write deleted.
    """
    seen = {}

    def observe(*args, **kwargs):
        seen["record_existed"] = (state_root / run_mod.LAST_CHECK_FILENAME).exists()
        raise RuntimeError("stopping here")

    monkeypatch.setattr(run_mod.acquire, "available_release", observe)
    run_mod.check(
        root=tmp_path, remote="origin", channel="stable",
        keyring_path=_write_keyring(tmp_path), state_root=state_root,
    )
    assert seen.get("record_existed") is True, (
        "the network was touched before anything was recorded; a run killed here "
        "would leave no trace, and every later session would start another one"
    )


def test_the_smoke_check_looks_through_the_root_not_at_the_staged_tree(tmp_path):
    """It used to stat a file inside the STAGED tree, which is true whether or
    not the swap happened — so it could not detect the failure it exists for."""
    root = tmp_path / "root"
    staged = tmp_path / "staged"
    (staged / "scripts").mkdir(parents=True)
    (staged / "scripts" / "_aqg_context.sh").write_text("#\n", encoding="utf-8")
    broken = tmp_path / "broken"
    broken.mkdir()
    os.symlink(broken, root)
    assert run_mod._smoke(root) is False, (
        "the smoke check passed a root that cannot bootstrap itself"
    )
    os.unlink(root)
    os.symlink(staged, root)
    assert run_mod._smoke(root) is True


def test_a_staging_directory_left_by_a_dead_run_is_not_reported_as_busy_forever(
    tmp_path, state_root, monkeypatch
):
    """A commit-named directory is a name collision, not a lock: it cannot tell
    "another run holds this" from "a run crashed and left it". Reporting the
    second as `busy` makes a permanent condition look like a race that will
    clear itself."""
    root, first, second = _install(tmp_path)

    class _Found:
        manifest = {"version": "0.17.0", "release_sequence": 9}
        commit = second
        key_id = "k"
        release_sequence = 9

    monkeypatch.setattr(run_mod.acquire, "available_release", lambda *a, **k: _Found())
    (tmp_path / "install/versions" / second).mkdir(parents=True)
    result = run_mod.check(
        root=root, remote="origin", channel="stable",
        keyring_path=_write_keyring(tmp_path), state_root=state_root,
    )
    assert result.outcome == "pending"
    assert any("left behind" in item or "already staged" in item for item in result.pending)


def test_no_outcome_is_declared_without_a_producer():
    """`offline` was declared and nothing produced it, so a network failure
    surfaced as `current` — telling `doctor` the machine was up to date when
    nobody had looked. The fourth time this feature declared a word nothing
    writes."""
    import inspect

    source = inspect.getsource(run_mod)
    for outcome in run_mod.OUTCOMES:
        assert source.count(f'"{outcome}"') > 1, (
            f"{outcome!r} is declared in OUTCOMES and nothing produces it"
        )


def test_the_launcher_runs_the_installed_aqg_not_whatever_is_in_the_cwd(tmp_path):
    """Found by the first end-to-end rehearsal, not by a test.

    `python3 -m scripts.aqg_update.run` resolves against the CURRENT WORKING
    DIRECTORY, and `-E` makes `PYTHONPATH` inert — so with no `cd`, the trigger
    ran whatever `scripts/aqg_update` happened to be under the user's project,
    or nothing at all. In the rehearsal it read the *development repository's*
    keyring path while checking an install somewhere else entirely.

    `test_the_launcher_actually_starts_a_check` passed throughout, because
    pytest runs with the repository as its cwd.
    """
    import time

    state = tmp_path / "state"
    state.mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "AQG_ROOT": str(root),
        "AQG_STATE_ROOT": str(state),
    }
    env.pop(run_mod.KILL_SWITCH, None)
    subprocess.run(
        ["bash", str(HOOK)], capture_output=True, env=env, timeout=30, cwd=str(elsewhere)
    )
    # Waits for a TERMINAL outcome, not merely for the file. The in-flight
    # record appears immediately, so "the record exists" stopped meaning "the
    # check finished" the moment that marker was added.
    record = state / run_mod.LAST_CHECK_FILENAME
    deadline = time.time() + 60
    recorded = None
    while time.time() < deadline:
        if record.exists():
            recorded = json.loads(record.read_text(encoding="utf-8"))
            if recorded["outcome"] != "interrupted":
                break
        time.sleep(0.25)
    assert recorded is not None, (
        "started from another directory, the launcher ran nothing at all"
    )
    # NOT an assertion about the message. The first version required the repo
    # path to appear in `detail`, which was only true while the outcome was
    # `no-keyring` — landing a real keyring changed the message and broke a test
    # that was never about the message. What must hold is that the pipeline ran
    # in the installed tree rather than dying on an import in someone else's.
    assert recorded["outcome"] in ("current", "pending", "applied", "no-keyring"), (
        f"the check did not complete: {recorded}"
    )
    assert "ModuleNotFoundError" not in recorded["detail"], (
        f"the launcher ran python somewhere without AQG on the path: {recorded}"
    )


def test_a_broken_host_adapter_does_not_kill_the_check_before_it_can_report(
    tmp_path, state_root, monkeypatch
):
    """The adapters are imported lazily, so an import failure lands inside the
    runner's own boundary rather than before it.

    Found in the rehearsal: a host adapter raised `ImportError` at module-import
    time, which is BEFORE `main`'s try block exists. The detached process died
    with a traceback into /dev/null, no record was written, and `doctor` would
    have said "never run" — indistinguishable from "not installed".
    """
    import scripts.aqg_update.hosts as hosts_mod

    def explode():
        raise ImportError("an adapter's dependency is missing on this machine")

    monkeypatch.setattr(hosts_mod, "available_clients", explode)
    result = run_mod.check(
        root=tmp_path, remote="origin", channel="stable",
        keyring_path=_write_keyring(tmp_path), state_root=state_root,
    )
    assert result.outcome in ("failed", "current"), result.outcome
    assert _last(state_root) is not None, "the check died without leaving a record"


def test_a_broken_adapter_is_reported_rather_than_silently_dropped(
    tmp_path, state_root, monkeypatch
):
    """The class, not the instance.

    A host adapter that raises was dropped from the evidence with nothing
    written anywhere, so a permanently broken adapter looked exactly like a
    client that is not installed — the same indistinguishability that made a
    dead runner look like an uninstalled one, repaired one level up and not
    here.
    """
    import scripts.aqg_update.hosts as hosts_mod

    monkeypatch.setattr(hosts_mod, "available_clients", lambda: ("claude-code",))

    class _Broken:
        def verify(self, **kwargs):
            raise RuntimeError("this adapter's dependency is missing")

    monkeypatch.setattr(hosts_mod, "adapter_for", lambda client_id: _Broken())
    dropped = []
    run_mod._collect_evidence(None, dropped)
    assert dropped and "dependency is missing" in dropped[0]


def test_applying_a_release_records_the_sequence_so_an_older_one_is_refused(
    tmp_path, state_root, monkeypatch
):
    """End to end: the guarantee, not the mechanism.

    The point of recording state is that the NEXT check refuses a replayed older
    release. Asserting that a file was written would test the plumbing; this
    asserts the behaviour the plumbing exists for.
    """
    root, first, second = _install(tmp_path)
    _clean_plan(monkeypatch, second)

    class _Found:
        manifest = {"version": "0.17.0", "release_sequence": 9, "commit": second}
        commit = second
        key_id = "stable-2026-09"
        release_sequence = 9

    monkeypatch.setattr(run_mod.acquire, "available_release", lambda *a, **k: _Found())
    result = run_mod.check(
        root=root, remote="origin", channel="stable",
        keyring_path=_write_keyring(tmp_path), state_root=state_root,
    )
    assert result.outcome == "applied", result.detail

    recorded = run_mod.state.read_state()
    assert recorded is not None, "nothing was recorded, so nothing can be refused"
    assert recorded["release_sequence"] == 9
    assert recorded["installed_commit"] == second

    # And the guarantee: the next check hands the verifier a real floor.
    seen = {}

    def capture(repo, *, remote, channel, keyring, installed_sequence):
        seen["floor"] = installed_sequence
        return None

    monkeypatch.setattr(run_mod.acquire, "available_release", capture)
    run_mod.check(
        root=root, remote="origin", channel="stable",
        keyring_path=_write_keyring(tmp_path), state_root=state_root,
        now=9_999_999_999.0,
    )
    assert seen["floor"] == 9, (
        f"the anti-rollback comparison still runs against {seen['floor']!r}"
    )
