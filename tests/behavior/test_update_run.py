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
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.aqg_update import run as run_mod


def test_the_conftest_guard_names_the_switch_this_module_owns():
    """`conftest.py` disarms the automatic channel for the whole suite by writing
    the variable name as a string literal, so that collecting one test file does
    not import the updater. A rename here would leave that literal pointing at
    nothing and silently re-arm every other suite."""
    assert run_mod.KILL_SWITCH == "AQG_NO_UPDATE_CHECK"


# `conftest.py` disables the automatic channel for the whole run so that no
# other suite's session-start adapter can start a real check against the
# developer's state or the network. This suite owns that channel, and isolates
# `AQG_STATE_ROOT`, the keyring and the remote per test, so it takes the switch
# back off. Tests below that want it ON still just set it themselves: the
# fixture applies before the test body.
pytestmark = pytest.mark.usefixtures("live_update_channel")


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
    run_mod.record(run_mod.CheckResult(outcome="current"), state_root=state_root,
                   scope=run_mod._check_scope(tmp_path, 'origin', 'stable', True))
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


# --- the interval that clock gate uses -----------------------------------------------------


def test_the_default_interval_is_one_hour(monkeypatch):
    """The default has to be a number this file states, not one a reader infers
    from a record's age. Debugging wants it short; a release may want it long."""
    monkeypatch.delenv(run_mod.INTERVAL_ENV, raising=False)
    assert run_mod.check_interval_seconds() == 3600


def test_the_interval_can_be_overridden_from_the_environment(monkeypatch):
    """Otherwise every adjustment is a code change and a release."""
    monkeypatch.setenv(run_mod.INTERVAL_ENV, "7200")
    assert run_mod.check_interval_seconds() == 7200


def test_an_override_of_surrounding_whitespace_is_still_read(monkeypatch):
    """`FOO=" 300 "` out of a shell is a typo, not a request for the default."""
    monkeypatch.setenv(run_mod.INTERVAL_ENV, "  300  ")
    assert run_mod.check_interval_seconds() == 300


@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", "soon", "1.5", "3600s", "0", "-1", "-3600",
        # `int()` on its own accepts every one of these. A silently-honoured
        # `3_600` is worse than a rejected one: it looks like it was ignored.
        "3_600", "+ 60", "0x10", "६०", "٣٦٠٠", "６０",
    ],
)
def test_an_unusable_override_falls_back_to_the_default(monkeypatch, raw):
    """This runs on the SessionStart path, where a raised exception is a check
    that silently never happens. A duration must be a positive whole number of
    seconds; anything else is malformed input, and malformed input takes the
    default rather than the process down."""
    monkeypatch.setenv(run_mod.INTERVAL_ENV, raw)
    assert run_mod.check_interval_seconds() == 3600


@pytest.mark.parametrize("raw", ["1", "30", "59", "+30"])
def test_an_override_below_the_floor_is_raised_to_it(monkeypatch, raw):
    """A positive duration under the floor IS a request for a short interval —
    honour it as far as the floor, which exists because anything smaller turns
    every session start into a network round trip. Answering it with the
    default would give the caller the opposite of what they asked for."""
    monkeypatch.setenv(run_mod.INTERVAL_ENV, raw)
    assert run_mod.check_interval_seconds() == run_mod.MIN_CHECK_INTERVAL_SECONDS == 60


@pytest.mark.parametrize("raw", ["604801", "999999999", str(2 ** 63)])
def test_an_override_above_the_ceiling_is_lowered_to_it(monkeypatch, raw):
    """The mirror of the floor, and it is the one that matters. `_too_soon`
    already defends the other operand of its comparison — a record dated a year
    ahead does not suppress the check — so leaving the interval unbounded would
    have reached the identical permanent, silent disablement of a self-updater
    that verifies signatures, through the input this change adds. Stopping
    checks is `AQG_NO_UPDATE_CHECK`'s job, and that switch is the one a person
    looking for it can find."""
    monkeypatch.setenv(run_mod.INTERVAL_ENV, raw)
    assert run_mod.check_interval_seconds() == run_mod.MAX_CHECK_INTERVAL_SECONDS == 7 * 24 * 3600


def test_the_clock_gate_actually_reads_the_override(tmp_path, state_root, monkeypatch):
    """The constant being configurable is worth nothing if `_too_soon` still
    closes over the old one. A record two hours old is stale under the one-hour
    default and fresh under a three-hour override.

    Each leg re-writes the record first, and that is the whole point. The first
    version did not, and the `no-keyring` path calls `_finish`, which records at
    `now` — so the second leg was reading a record it had just written seconds
    ago, and passed under any interval at all. It went green against a mutant
    whose gate compared against `DEFAULT_CHECK_INTERVAL_SECONDS` and ignored the
    override entirely, which is precisely the defect this test exists to catch.
    """
    def outcome_with(interval):
        run_mod.record(
            run_mod.CheckResult(outcome="current"), state_root=state_root,
            at=time.time() - 2 * 3600,
            scope=run_mod._check_scope(tmp_path, 'origin', 'stable', True),
        )
        if interval is None:
            monkeypatch.delenv(run_mod.INTERVAL_ENV, raising=False)
        else:
            monkeypatch.setenv(run_mod.INTERVAL_ENV, str(interval))
        return run_mod.check(
            root=tmp_path, remote="origin", channel="stable",
            keyring_path=tmp_path / "absent.json",
        ).outcome

    # Fails in both directions: a longer interval must suppress the check, and a
    # shorter one must let it through. One leg alone can be satisfied by an
    # interval the gate never read.
    assert outcome_with(None) == "no-keyring"
    assert outcome_with(3 * 3600) == "too-soon"
    assert outcome_with(60) == "no-keyring"


def test_an_override_does_not_revive_a_future_timestamp(tmp_path, state_root, monkeypatch):
    """The record is a file the user can write, and the future-timestamp guard
    is what stops one dated a year ahead from turning the check off forever. A
    configurable interval must not reintroduce the denial of service by
    reaching the comparison before that guard."""
    monkeypatch.setenv(run_mod.INTERVAL_ENV, "60")
    run_mod.record(
        run_mod.CheckResult(outcome="current"), state_root=state_root,
        at=9_999_999_999.0,
    )
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
        env={
            **os.environ,
            "AQG_ROOT": str(Path(__file__).resolve().parents[2]),
            # This is the only test that starts a REAL check, and it used to
            # say neither where its state goes nor which remote it talks to. Its
            # detached child recorded into the developer's own `aqg-state`, and
            # `check()` applies by default -- so it could stamp an install-state
            # that does not describe the tree it names. Observed once.
            #
            # BOTH lines are needed, and a state root alone is worse than
            # nothing: an empty one has no throttle record, so the 20-hour gate
            # that used to suppress this by accident now lets it through on
            # EVERY run. The unresolvable remote is what stops the child before
            # it can fetch or apply, while leaving it real enough that a
            # launcher which waited for it would still blow the assertion below.
            "AQG_STATE_ROOT": str(tmp_path / "state"),
            "AQG_UPDATE_REMOTE": "aqg-test-remote-that-does-not-exist",
        },
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
    assert os.path.realpath(root) == str((tmp_path / "install/versions/0.17.0").resolve())
    assert (Path(os.path.realpath(root)) / "VERSION").read_text().strip() == "0.17.0"
    assert _last(state_root)["outcome"] == "applied"


def test_real_swap_reports_old_rules_then_current_check_clears_repaired_notice(tmp_path, state_root, monkeypatch):
    root, first, second = _install(tmp_path)
    home = tmp_path / 'rules-home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setenv('CODEX_HOME', str(home / '.codex'))
    monkeypatch.setenv('APPDATA', str(home / 'AppData/Roaming'))
    rule = home / '.claude/CLAUDE.md'
    rule.parent.mkdir()
    heading = '## Agent Quality Gates (AQG) engineering discipline\n'
    rule.write_text(heading + f'aqg-code-construction: {tmp_path}/versions/old/docs/policies/audit-trigger.md')
    original = rule.read_bytes()

    class Found:
        manifest = {'version': '0.17.0', 'release_sequence': 9}
        commit = second
        key_id = 'k'
        release_sequence = 9

    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: Found())
    _clean_plan(monkeypatch, second)
    keyring = _write_keyring(tmp_path)
    args = dict(root=root, remote='origin', channel='stable', keyring_path=keyring, state_root=state_root)
    result = run_mod.check(**args, now=10000)
    assert result.outcome == 'applied'
    assert (root / 'VERSION').read_text().strip() == '0.17.0'
    assert any('rules: claude-code:' in item for item in _last(state_root)['pending'])
    assert rule.read_bytes() == original
    assert run_mod.state.read_state(path=state_root / run_mod.state.STATE_FILENAME)['pending'] == []
    rule.write_text(heading + f'aqg-code-construction: {root}/docs/policies/audit-trigger.md')
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: None)
    assert run_mod.check(**args, now=20000).outcome == 'current'
    assert _last(state_root)['pending'] == []


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


def test_the_launcher_forwards_the_interval_override(tmp_path):
    """`env -i` scrubs the environment, so a variable the module reads is dead
    on the one path where the interval is actually consulted unless the
    launcher is told to carry it. Asserting the module alone would have shipped
    an override that works in tests and nowhere else."""
    assert f'{run_mod.INTERVAL_ENV}="${{{run_mod.INTERVAL_ENV}:-}}"' in _hook_code(), (
        "the SessionStart launcher does not forward the interval override "
        "through its `env -i` allowlist"
    )


def _forwarded_names() -> set:
    """The variable names between `env -i` and the `sh -c` it execs.

    Anchored to the argument list rather than scanned from the whole file: a
    file-wide `NAME=` scan produces the same set whether or not `-i` is there,
    and `env` without `-i` hands the child the entire parent environment.
    """
    body = _hook_code()
    start = body.index("env -i") + len("env -i")
    end = body.index("sh -c", start)
    # A leading quote is tolerated: `env -i "PYTHONPATH=/x"` forwards it just as
    # surely as the unquoted form, and must not slip past for want of a match.
    return set(re.findall(r"""(?:^|\s)["']?([A-Za-z_][A-Za-z0-9_]*)=""", body[start:end]))


def test_the_launcher_forwards_exactly_its_allowlist(tmp_path):
    """A frozen set, not a denylist of the two names that came to mind.

    The previous version asserted only that no `PYTHON*` or `LD_PRELOAD` entry
    was present, which is thin in both directions: `LD_LIBRARY_PATH`, `LD_AUDIT`,
    `BASH_ENV` (sourced by bash before a non-interactive `-c`), `IFS` and
    `GIT_SSH_COMMAND` would all have passed it, and it was computed from a
    file-wide scan that could not see `-i` disappear. Equality makes any change
    to the perimeter a change someone has to make here, deliberately, too.
    """
    assert _forwarded_names() == {
        "HOME", "PATH", "LANG", "USERPROFILE",
        "AQG_ROOT", "AQG_STATE_ROOT", "AQG_UPDATE_INTERVAL_SECONDS",
    }


def test_the_launcher_survives_a_hostile_interval_value(tmp_path):
    """The one forwarded variable a user is invited to set, given a payload.

    An audit called this construction a critical shell-metacharacter breakout —
    a value containing a quote closing the quoted region early and injecting
    `LD_PRELOAD=` into the `env` argument list. It is not: the result of a
    parameter expansion is not re-parsed as shell syntax. But "not re-parsed" is
    a language rule quoted in a review, and this is the perimeter around the
    process that verifies release signatures, so it is asserted by running it.
    """
    fragment = "\n".join(
        line for line in _hook_code().splitlines()
        if "env -i" in line or "=" in line or "sh -c" in line
    ).replace("nohup ", "").replace("</dev/null >/dev/null 2>&1 &", "")
    fragment = fragment.replace(
        "exec python3 -E -s -m scripts.aqg_update.run", "exec env"
    )

    payload = '\'" LD_PRELOAD=/tmp/evil.so $(touch {}) `id` ;echo pwned "\''.format(
        tmp_path / "PWNED"
    ).strip("'")
    proc = subprocess.run(
        ["bash", "-c", fragment],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "AQG_ROOT": str(tmp_path), run_mod.INTERVAL_ENV: payload},
    )

    child = dict(
        line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
    )
    assert child.get(run_mod.INTERVAL_ENV) == payload, (
        f"the value did not arrive intact: {proc.stdout!r} {proc.stderr!r}"
    )
    assert "LD_PRELOAD" not in child, f"injected into the child environment: {child}"
    assert not (tmp_path / "PWNED").exists(), "the payload executed"
    # The payload's own text appearing in `env`'s output is not a failure — it
    # is the proof, because it is there as one variable's value and not as a
    # variable of its own. What would be a failure is a NAME the allowlist does
    # not have — `env` and `sh` add these four themselves, OLDPWD because the
    # launcher's `cd "$AQG_ROOT"` is what makes `-m` resolve.
    assert set(child) - {"PWD", "OLDPWD", "SHLVL", "_"} <= _forwarded_names(), child


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
    (tmp_path / "install/versions/0.17.0").mkdir(parents=True)
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


def test_manual_upgrade_reads_the_target_version_and_preserves_old_tree(tmp_path, state_root, monkeypatch):
    root, first, second = _install(tmp_path)
    old = root.resolve()
    _clean_plan(monkeypatch, second)
    result = run_mod.apply_commit(root=root, commit=second, state_root=state_root)
    assert result.outcome == 'applied', result.detail
    assert root.resolve().name == '0.17.0'
    assert old.name == first and (old / 'VERSION').read_text().strip() == '0.16.0'
    record = run_mod.state.read_state()
    assert record['installed_commit'] == second
    assert record['installed_version'] == '0.17.0'
    again = run_mod.apply_commit(root=root, commit=second, state_root=state_root)
    assert again.outcome == 'current', again.detail
    assert root.resolve().name == '0.17.0'


@pytest.mark.parametrize('short_name', [False, True])
def test_current_commit_and_rollback_work_with_both_directory_names(tmp_path, state_root, monkeypatch, short_name):
    root, first, second = _install(tmp_path)
    if short_name:
        old = root.resolve()
        target = old.with_name('0.16.0')
        root.unlink()
        old.rename(target)
        root.symlink_to(target, target_is_directory=True)
    old = root.resolve()
    _clean_plan(monkeypatch, second)
    same = run_mod.apply_commit(root=root, commit=first, version='0.16.0', state_root=state_root)
    assert same.outcome == 'current', same.detail
    monkeypatch.setattr(run_mod, '_smoke', lambda root: False)
    failed = run_mod.apply_commit(root=root, commit=second, version='0.17.0', state_root=state_root)
    assert failed.outcome == 'rolled-back', failed.detail
    assert root.resolve() == old
    assert (root / 'VERSION').read_text().strip() == '0.16.0'


@pytest.mark.parametrize('label, full_hash', [
    ('0.17.0', False), ('0.17.0-rc.1+build.5', False), ('1.2.3-' + 'x' * 30, True),
])
def test_same_version_different_commit_gets_suffix_without_overwriting(tmp_path, state_root, monkeypatch, label, full_hash):
    root, first, second = _install(tmp_path)
    _clean_plan(monkeypatch, second)
    assert run_mod.apply_commit(root=root, commit=second, version=label, state_root=state_root).outcome == 'applied'
    old = root.resolve()
    origin = tmp_path / 'origin'
    (origin / 'new-marker').write_text('new generation', encoding='utf-8')
    def git(path, *args):
        return subprocess.check_output(['git', '-c', 'core.longpaths=true', '-C', str(path), *args], stderr=subprocess.PIPE).decode().strip()
    git(origin, 'add', 'new-marker')
    git(origin, 'commit', '-m', 'same version new content')
    third = git(origin, 'rev-parse', 'HEAD')
    git(root, 'fetch', 'origin')
    _clean_plan(monkeypatch, third)
    result = run_mod.apply_commit(root=root, commit=third, version=label, state_root=state_root)
    assert result.outcome == 'applied', result.detail
    assert root.resolve().name == (third if full_hash else label + '-' + third[:12])
    if not full_hash:
        assert run_mod.stage.is_release_name(root.resolve().name)
    assert (root / 'new-marker').read_text() == 'new generation'
    assert git(old, 'rev-parse', 'HEAD') == second
    assert not (old / 'new-marker').exists()


@pytest.mark.parametrize('label', ['../escape', r'..\escape', 'C:stream', '/tmp/escape', 'CON', 'release', '1.2.3.' , '1.2.3-' + 'x' * 150])
def test_non_version_labels_keep_commit_directory_names(tmp_path, state_root, monkeypatch, label):
    root, first, second = _install(tmp_path)
    _clean_plan(monkeypatch, second)
    result = run_mod.apply_commit(root=root, commit=second, version=label, state_root=state_root)
    assert result.outcome == 'applied', result.detail
    assert root.resolve() == root.parent / 'versions' / second


@pytest.mark.parametrize('prior_sequence', [None, 5, 9, 12])
def test_matching_head_records_verified_metadata_without_reinstalling(tmp_path, state_root, monkeypatch, prior_sequence):
    root, first, second = _install(tmp_path)
    _clean_plan(monkeypatch, second)
    assert run_mod.apply_commit(root=root, commit=second, state_root=state_root).outcome == 'applied'
    live = root.resolve()
    path = run_mod._state_file(state_root)
    if prior_sequence is None:
        path.unlink()
    else:
        prior = run_mod.state.read_state()
        prior.update(release_sequence=prior_sequence, applied_by='previous', hosts={'codex': {}}, pending=['keep this pending item'])
        run_mod.state.write_state(prior)
    before = path.read_bytes() if path.exists() else None
    class Found:
        manifest = {'version': '0.17.0'}
        commit = second
        key_id = 'k'
        release_sequence = 9
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: Found())
    result = run_mod.check(root=root, remote='origin', channel='stable', keyring_path=_write_keyring(tmp_path), state_root=state_root, now=10000)
    assert result.outcome == 'current', result.detail
    assert root.resolve() == live
    recorded = run_mod.state.read_state()
    assert recorded['release_sequence'] == max(prior_sequence or 0, 9)
    assert recorded['installed_commit'] == second
    if prior_sequence is not None:
        assert recorded['hosts'] == {'codex': {}} and recorded['pending'] == ['keep this pending item']
    if prior_sequence is None or prior_sequence < 9:
        assert recorded['applied_by'] == 'k'
    else:
        assert path.read_bytes() == before
    snapshot = path.read_bytes()
    assert run_mod.apply_commit(root=root, commit=second, state_root=state_root).outcome == 'current'
    assert path.read_bytes() == snapshot  # Manual sequence zero cannot erase verified state.
    observed = []
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: observed.append(k['installed_sequence']))
    run_mod.check(root=root, remote='origin', channel='stable', keyring_path=_write_keyring(tmp_path), state_root=state_root, now=20000)
    assert observed == [recorded['release_sequence']]


def test_matching_head_metadata_write_failure_is_not_reported_current(tmp_path, state_root, monkeypatch):
    root, first, second = _install(tmp_path)
    _clean_plan(monkeypatch, second)
    assert run_mod.apply_commit(root=root, commit=second, state_root=state_root).outcome == 'applied'
    live, before = root.resolve(), run_mod._state_file(state_root).read_bytes()
    class Found:
        manifest = {'version': '0.17.0'}
        commit = second
        key_id = 'k'
        release_sequence = 9
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: Found())
    def fail(*args, **kwargs):
        raise OSError('fixture refuses state write')
    monkeypatch.setattr(run_mod.state, 'write_state', fail)
    result = run_mod.check(root=root, remote='origin', channel='stable', keyring_path=_write_keyring(tmp_path), state_root=state_root)
    assert result.outcome == 'repair-required', result.detail
    assert root.resolve() == live
    assert run_mod._state_file(state_root).read_bytes() == before


@pytest.mark.parametrize('changed', ['sequence', 'live-root'])
def test_matching_head_rechecks_state_and_identity_under_the_transaction_lock(tmp_path, state_root, monkeypatch, changed):
    root, first, second = _install(tmp_path)
    old = root.resolve()
    _clean_plan(monkeypatch, second)
    assert run_mod.apply_commit(root=root, commit=second, state_root=state_root).outcome == 'applied'
    before = run_mod._state_file(state_root).read_bytes()
    class Found:
        manifest = {'version': '0.17.0'}
        commit = second
        key_id = 'k'
        release_sequence = 9
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: Found())
    apply = run_mod.transaction.apply_plan
    def change_before_lock(*args, **kwargs):
        if changed == 'sequence':
            latest = run_mod.state.read_state()
            latest.update(release_sequence=12, applied_by='newer-release')
            run_mod.state.write_state(latest)
        else:
            run_mod.stage.swap_root(root=root, target=old)
        return apply(*args, **kwargs)
    monkeypatch.setattr(run_mod.transaction, 'apply_plan', change_before_lock)
    result = run_mod.check(root=root, remote='origin', channel='stable', keyring_path=_write_keyring(tmp_path), state_root=state_root)
    if changed == 'sequence':
        assert result.outcome == 'current', result.detail
        recorded = run_mod.state.read_state()
        assert recorded['release_sequence'] == 12
        assert recorded['applied_by'] == 'newer-release'
    else:
        assert result.outcome == 'repair-required', result.detail
        assert root.resolve() == old
        assert run_mod._state_file(state_root).read_bytes() == before


def test_a_parent_repository_cannot_supply_the_live_generation_identity(tmp_path, state_root):
    root, first, second = _install(tmp_path)
    fake = tmp_path / 'origin/versions/0.17.0'
    fake.mkdir(parents=True)
    (fake / 'VERSION').write_text('0.17.0', encoding='utf-8')
    root.unlink()
    root.symlink_to(fake, target_is_directory=True)
    result = run_mod.apply_commit(root=root, commit=second, version='0.17.0', state_root=state_root)
    assert result.outcome == 'failed', result.detail
    assert root.resolve() == fake


def test_missing_committed_version_is_refused_without_changing_live_tree(tmp_path, state_root):
    root, first, second = _install(tmp_path)
    origin = tmp_path / 'origin'
    subprocess.run(['git', '-C', str(origin), 'rm', 'VERSION'], capture_output=True, check=True)
    subprocess.run(['git', '-C', str(origin), 'commit', '-m', 'missing sentinel'], capture_output=True, check=True)
    commit = subprocess.check_output(['git', '-C', str(origin), 'rev-parse', 'HEAD']).decode().strip()
    subprocess.run(['git', '-C', str(root), 'fetch', 'origin'], capture_output=True, check=True)
    live = root.resolve()
    result = run_mod.apply_commit(root=root, commit=commit, state_root=state_root)
    assert result.outcome == 'failed'
    assert root.resolve() == live
    assert (root / 'VERSION').read_text().strip() == '0.16.0'


@pytest.mark.parametrize('first_apply', [False, True])
def test_a_different_root_or_check_only_cannot_suppress_apply(tmp_path, state_root, monkeypatch, first_apply):
    root, first, second = _install(tmp_path)
    _clean_plan(monkeypatch, second)
    from types import SimpleNamespace
    found = SimpleNamespace(commit=second, manifest={'version': '0.17.0'}, key_id='k', release_sequence=9)
    calls = []
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: calls.append(a[0]) or found)
    keyring = _write_keyring(tmp_path)
    # An old physical generation fails; a check-only nudge merely finds the release.
    initial = run_mod.check(root=root.resolve() if first_apply else root, remote='origin', channel='stable', keyring_path=keyring, state_root=state_root, now=10000, apply=first_apply)
    assert initial.outcome == ('invalid-root' if first_apply else 'deferred')
    applied = run_mod.check(root=root, remote='origin', channel='stable', keyring_path=keyring, state_root=state_root, now=10001)
    assert applied.outcome == 'applied', applied.detail
    assert (root / 'VERSION').read_text().strip() == '0.17.0'


def test_network_failure_retries_after_backoff_without_waiting_an_hour(tmp_path, state_root, monkeypatch):
    root, _, _ = _install(tmp_path)
    calls = []
    def fail(*a, **k):
        calls.append(1)
        raise RuntimeError('network unavailable')
    monkeypatch.setattr(run_mod.acquire, 'available_release', fail)
    keyring = _write_keyring(tmp_path)
    def attempt(now):
        return run_mod.check(root=root, remote='origin', channel='stable', keyring_path=keyring, state_root=state_root, now=now)
    assert attempt(10000).outcome == 'failed'
    assert attempt(10001).outcome == 'too-soon'
    assert attempt(10301).outcome == 'failed'
    assert len(calls) == 2


def test_legacy_failure_record_does_not_block_a_new_updater(tmp_path, state_root, monkeypatch):
    root, _, _ = _install(tmp_path)
    run_mod.record(run_mod.CheckResult(outcome='failed', detail='old physical root'), state_root=state_root, at=10000)
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: None)
    result = run_mod.check(root=root, remote='origin', channel='stable', keyring_path=_write_keyring(tmp_path), state_root=state_root, now=10001)
    assert result.outcome == 'current'


def test_simultaneous_agents_admit_only_one_check(tmp_path, state_root, monkeypatch):
    import threading
    root, _, _ = _install(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def acquire(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5), 'test did not release acquisition'
        return None
    monkeypatch.setattr(run_mod.acquire, 'available_release', acquire)
    keyring = _write_keyring(tmp_path)
    def check():
        return run_mod.check(root=root, remote='origin', channel='stable', keyring_path=keyring, state_root=state_root, now=10000)
    results = []
    worker = threading.Thread(target=lambda: results.append(check()))
    worker.start()
    try:
        assert entered.wait(5)
        assert check().outcome == 'busy'
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0].outcome == 'current'
    assert calls == [1]
    # An old binary may still overwrite the legacy diagnostic slot.
    run_mod.record(run_mod.CheckResult(outcome='failed'), state_root=state_root, at=10000)
    assert check().outcome == 'too-soon'


@pytest.mark.parametrize('apply', [False, True])
def test_obsolete_physical_roots_are_rejected_before_network(tmp_path, state_root, monkeypatch, apply):
    root, _, _ = _install(tmp_path)
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: pytest.fail('must not fetch'))
    result = run_mod.check(root=root.resolve(), remote='origin', channel='stable',
        keyring_path=_write_keyring(tmp_path), state_root=state_root, now=10000, apply=apply)
    assert result.outcome == 'invalid-root'


def test_discovery_supersedes_an_older_current_apply_record(tmp_path, state_root, monkeypatch):
    from types import SimpleNamespace
    root, _, second = _install(tmp_path)
    _clean_plan(monkeypatch, second)
    keyring = _write_keyring(tmp_path)
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k: None)
    def check(now, apply=True):
        return run_mod.check(root=root, remote='origin', channel='stable', keyring_path=keyring,
            state_root=state_root, now=now, apply=apply)
    assert check(10000).outcome == 'current'
    monkeypatch.setattr(run_mod.acquire, 'available_release', lambda *a, **k:
        SimpleNamespace(commit=second, manifest={'version':'0.17.0'}, key_id='key', release_sequence=9))
    assert check(10001, False).outcome == 'deferred'
    assert check(10002).outcome == 'applied'


def test_prelock_failure_is_rate_limited(tmp_path, state_root, monkeypatch):
    calls = []
    def fail(**kwargs):
        calls.append(1)
        raise run_mod.lock.LockError('permission denied')
    monkeypatch.setattr(run_mod.lock, 'install_lock', fail)
    def check(now):
        return run_mod.check(root=tmp_path, remote='origin', channel='stable', state_root=state_root, now=now)
    assert check(10000).outcome == 'failed'
    assert check(10001).outcome == 'too-soon'
    assert calls == [1]


def test_explicit_interval_also_controls_failure_retry(tmp_path, state_root, monkeypatch):
    monkeypatch.setenv(run_mod.INTERVAL_ENV, '7200')
    scope = run_mod._check_scope(tmp_path, 'origin', 'stable', True)
    run_mod.record(run_mod.CheckResult(outcome='failed'), state_root=state_root, scope=scope, at=10000)
    assert run_mod._too_soon(state_root, 10301, scope=scope)


def test_scope_survives_entrance_swap_and_parent_alias(tmp_path):
    parent = tmp_path / 'real'
    parent.mkdir()
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    entrance = parent / 'entrance'
    entrance.symlink_to(first, target_is_directory=True)
    alias = tmp_path / 'alias'
    alias.symlink_to(parent, target_is_directory=True)
    key = run_mod._check_scope(entrance, 'origin', 'stable', True)
    assert run_mod._check_scope(alias / 'entrance', 'origin', 'stable', True) == key
    entrance.unlink()
    entrance.symlink_to(second, target_is_directory=True)
    assert run_mod._check_scope(entrance, 'origin', 'stable', True) == key


@pytest.mark.parametrize('advance', ['state', 'root', 'activated-target'])
def test_concurrent_updater_cannot_be_overwritten_by_old_plan(tmp_path, state_root, monkeypatch, advance):
    root, first, second = _install(tmp_path)
    _clean_plan(monkeypatch, second)
    original = run_mod.transaction.apply_plan
    live = root.resolve()
    other = live.parent / 'other'
    def concurrent(*args, **kwargs):
        if advance == 'state':
            run_mod._record_installed(channel='stable', version='0.16.0', commit=first,
                sequence=10, key_id='newer', pending=[], state_root=state_root)
        elif advance == 'root':
            other.mkdir()
            (other / 'VERSION').write_text('0.16.0')
            root.unlink()
            root.symlink_to(other, target_is_directory=True)
        else:
            root.unlink()
            root.symlink_to(kwargs['resources'].target, target_is_directory=True)
        return original(*args, **kwargs)
    monkeypatch.setattr(run_mod.transaction, 'apply_plan', concurrent)
    result = run_mod._apply(root=root, commit=second, version='0.17.0', installed=None,
        state_root=state_root, sequence=9, key_id='older')
    assert result.outcome == 'failed', result.detail
    assert 'changed' in result.detail or 'sequence' in result.detail
    if advance == 'activated-target':
        assert (root / 'VERSION').read_text().strip() == '0.17.0'
    else:
        assert root.resolve() == (live if advance == 'state' else other)
    if advance == 'state':
        assert run_mod.state.read_state()['release_sequence'] == 10
    assert not (state_root / run_mod.transaction.JOURNAL_FILENAME).exists()


def test_scoped_pending_survives_other_root_and_legacy_writes(tmp_path, state_root):
    a = run_mod._check_scope(tmp_path/'a', 'origin', 'stable', True)
    b = run_mod._check_scope(tmp_path/'b', 'origin', 'stable', True)
    run_mod.record(run_mod.CheckResult(outcome='pending', pending=('repair A',)),
        state_root=state_root, scope=a, at=10000, identity=['root-A','origin','stable',True])
    run_mod.record(run_mod.CheckResult(outcome='applied'), state_root=state_root, scope=b,
        at=10001, identity=['root-B','origin','stable',True])
    assert json.loads((state_root/f'update-check-{b}.json').read_text())['pending'] == []
    run_mod.record(run_mod.CheckResult(outcome='current'), state_root=state_root, at=10002)
    text = '\n'.join(run_mod.report(state_root=state_root))
    assert 'repair A' in text and 'root-A' in text
    run_mod.record(run_mod.CheckResult(outcome='applied'), state_root=state_root, scope=a, at=10003)
    assert 'repair A' not in '\n'.join(run_mod.report(state_root=state_root))


def test_transaction_precondition_holds_lock_and_precedes_journal(tmp_path):
    from scripts.aqg_update import transaction, dispatch, plan
    lock_path, journal = tmp_path/'lock', tmp_path/'journal'
    def guard():
        with pytest.raises(run_mod.lock.LockBusy):
            with run_mod.lock.install_lock(path=lock_path):
                pytest.fail('precondition must hold the install lock')
        assert not journal.exists()
        raise RuntimeError('baseline changed')
    result = transaction.apply_plan(plan.Plan(),
        resources=dispatch.Resources(root=tmp_path/'root', target=tmp_path/'target'),
        journal=journal, lock_path=lock_path, precondition=guard)
    assert result.status == 'stale-plan'
    assert not journal.exists()
