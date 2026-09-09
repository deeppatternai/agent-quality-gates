"""Skill-triggered apply keeps the current invocation's resources pinned.

The existing context helper starts the existing updater without check-only.
Its physical skill pin prevents the resource-reader race from audit F14.
Explicit check-only remains supported and must report deferred, not current.
Launcher, apply transaction and concurrent resource reads are tested separately:
the launcher probe runs a real detached Python child; network and host evidence
are fixtures so these tests cannot update the developer's installation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.aqg_update import run as run_mod

# `conftest.pytest_configure` sets AQG_NO_UPDATE_CHECK for the whole run so a
# plain `pytest` cannot reach the network or stage an upgrade from a suite that
# only meant to assert a JSON envelope. This suite is one of the few that has to
# watch the check actually run, and it isolates everything the check touches in
# `tmp_path`, so it takes the switch back off by name.
pytestmark = pytest.mark.usefixtures("live_update_channel")


def _helper() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts/_aqg_context.sh"


# --- R1: the existing skill-side trigger applies -----------------------------

@pytest.mark.parametrize("host_pending", [False, True])
def test_the_skill_trigger_enables_apply(tmp_path, monkeypatch, host_pending):
    """Proven by what the child process is actually told, not by reading the
    helper for a flag — a text assertion here once passed while the trigger
    launched nothing at all.

    Reuses the probe from `test_update_install_layout.py` rather than building a
    second one: that harness is already demonstrated to work, and a fixture I
    wrote fresh for this failed for reasons that had nothing to do with the
    behaviour under test.
    """
    from tests.behavior.test_update_install_layout import _nudge_probe

    # A subdirectory of its own: `_nudge_probe` builds `<dir>/aqg`, and sharing
    # tmp_path with anything else that touches that name makes the failure look
    # like the trigger not firing.
    fresh = tmp_path / "probe-root"
    fresh.mkdir()
    seen = _nudge_probe(fresh, {})
    assert seen, "the trigger launched no process at all"
    assert seen.get("argv") is not None, (
        "the probe does not record argv, so this cannot check the flag"
    )
    assert "--check-only" not in seen["argv"], (
        f"a hookless skill invocation still cannot apply an update: "
        f"{seen['argv']}"
    )
    assert Path(seen["cwd"]) == Path(seen["aqg_root"])
    # Exercise those actual launcher arguments through the real update CLI and
    # transaction. Only release verification and host planning use fixtures;
    # signature refusals and host blockers have their own regression suites.
    from types import SimpleNamespace
    from tests.behavior.test_update_run import _install, _clean_plan, _write_keyring

    fixture_root, first, second = _install(tmp_path)
    physical = fixture_root.resolve()
    home = tmp_path / "home"
    home.mkdir()
    root = home / ".deeppattern/agent-quality-gates"
    root.parent.mkdir()
    root.symlink_to(physical, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # The real launcher passes its physical root, which the updater maps back
    # to the canonical managed entrance. Do not bypass that mapping in the test.
    monkeypatch.setenv("AQG_ROOT", str(physical))
    monkeypatch.setenv("AQG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("AQG_UPDATE_CHANNEL", "stable")
    monkeypatch.setenv("AQG_UPDATE_REMOTE", "origin")
    keyring = _write_keyring(tmp_path)
    load_keys = run_mod.trust.load_trusted_keys
    monkeypatch.setattr(run_mod.trust, "load_trusted_keys", lambda _: load_keys(keyring))
    found = SimpleNamespace(manifest={"version": "0.17.0"}, commit=second,
                            key_id="k", release_sequence=9)
    monkeypatch.setattr(run_mod.acquire, "available_release", lambda *a, **kw: found)
    if host_pending:
        from scripts.aqg_update.hosts.base import Evidence
        evidence = {"claude-code": Evidence(
            client_id="claude-code", hooks_status="stale",
            hooks_detail="hook configuration needs reconciliation",
            recorded_version="0.16.0", routed_skills=("aqg-demo",),
        )}
        monkeypatch.setattr(run_mod, "_collect_evidence", lambda *a, **kw: evidence)
        # Keep the real planner and its root-relative host blocker.
    else:
        monkeypatch.setattr(run_mod, "_collect_evidence", lambda *a, **kw: {})
        _clean_plan(monkeypatch, second)
    assert run_mod.main(seen["argv"]) == 0
    assert (root / "VERSION").read_text().strip() == (
        "0.16.0" if host_pending else "0.17.0"
    )
    assert physical.is_dir()
    if host_pending:
        import json
        record = json.loads((tmp_path / "state" / run_mod.LAST_CHECK_FILENAME).read_text())
        assert record["outcome"] == "pending"
        assert root.resolve() == physical


@pytest.mark.parametrize("skill,filename,root_name", [
    ("aqg-project-status", "aqg_project_status.py", "_aqg_root()"),
    ("aqg-decision-capture", "aqg_decision_capture.py", "_AQG_ROOT"),
])
@pytest.mark.parametrize("matching_pin", [True, False, None, "explicit-other-root"])
def test_resource_readers_keep_the_script_pin_only_when_it_matches(
    tmp_path, skill, filename, root_name, matching_pin,
):
    """Read real versioned resources after the entrance has moved to B."""
    import shutil
    import sys

    repo = _helper().parents[1]
    first, second = tmp_path / "versions/a", tmp_path / "versions/b"
    for tree, marker in ((first, "A"), (second, "B")):
        (tree / "scripts").mkdir(parents=True)
        (tree / "VERSION").write_text(marker, encoding="utf-8")
        (tree / "scripts/marker").write_text(marker, encoding="utf-8")
    script = first / "skills" / skill / "scripts" / filename
    script.parent.mkdir(parents=True)
    shutil.copyfile(repo / "skills" / skill / "scripts" / filename, script)
    entrance = tmp_path / "aqg"
    entrance.symlink_to(first, target_is_directory=True)
    read_root = "module['_aqg_root']()" if root_name.endswith("()") else "module['_AQG_ROOT']"
    command = [sys.executable, "-c",
               "import os,runpy,sys; os.unlink(sys.argv[2]); "
               "os.symlink(sys.argv[3],sys.argv[2],target_is_directory=True); "
               "os.environ.update({'AQG_ROOT': sys.argv[3]} "
               "if sys.argv[4] == 'explicit-other-root' else {}); "
               "module=runpy.run_path(sys.argv[1]); "
               f"print(({read_root} / 'scripts/marker').read_text())",
               str(script), str(entrance), str(second), str(matching_pin)]
    env = {**os.environ, "AQG_ROOT": str(entrance),
           "AQG_SKILL_ROOT": str(second) if matching_pin is False else "",
           "AQG_NO_UPDATE_CHECK": "1"}
    if matching_pin:
        # Exercise the real helper's exported pin across the shell/Python
        # boundary, including native Windows environment conversion.
        # A matching invocation pin wins even over a later explicit root change.
        command = ["bash", "-c", 'source "$1" && shift && exec "$@"',
                   "bash", str(_helper()), *command]
    done = subprocess.run(
        command, env=env,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ("A" if matching_pin else "B")


def test_context_exports_a_separate_skill_pin(tmp_path):
    first = tmp_path / "versions/a"
    first.mkdir(parents=True)
    (first / "VERSION").write_text("1\n", encoding="utf-8")
    entrance = tmp_path / "aqg"
    entrance.symlink_to(first, target_is_directory=True)
    done = subprocess.run(
        ["bash", "-c", 'source "$1" && test "$AQG_SKILL_ROOT" = "$aqg_root" '
         '&& test -f "$AQG_SKILL_ROOT/VERSION"', "bash", str(_helper())],
        env={**os.environ, "AQG_ROOT": str(entrance), "AQG_NO_UPDATE_CHECK": "1"},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert done.returncode == 0, done.stderr


def test_the_session_start_hook_still_applies():
    """The other half. Deferring everywhere would mean updates never land on the
    hosts that CAN take a hook — which is most of them."""
    hook = (
        Path(__file__).resolve().parents[2]
        / "agent-packs/claude-code/hooks/sessionstart_update_check.sh"
    ).read_text(encoding="utf-8")
    assert "--check-only" not in hook, (
        "the session-start path stopped applying; nothing would ever update"
    )


# --- R2: a deferred update is not "current" -----------------------------------

def test_an_available_update_that_was_not_applied_is_not_reported_as_current(
    tmp_path, monkeypatch
):
    """`report()` reads only the outcome, so `current` here told a machine with
    a pending update that it was up to date — the silent non-updating install
    wearing a different hat."""
    class _Found:
        manifest = {"version": "9.9.9", "release_sequence": 99, "commit": "a" * 40}
        commit = "a" * 40
        key_id = "stable-2026-09"
        release_sequence = 99

    monkeypatch.setattr(run_mod.acquire, "available_release", lambda *a, **k: _Found())
    monkeypatch.setattr(run_mod.state, "read_state", lambda *a, **k: None)

    result = run_mod._check_locked(
        root=tmp_path, remote="origin", channel="stable",
        keyring={}, state_root=tmp_path / "state", apply=False,
    )

    assert result.outcome != "current", (
        "an available-but-deferred update was reported as up to date"
    )
    assert result.outcome in run_mod.OUTCOMES, (
        f"{result.outcome!r} is not a declared outcome, so nothing can render it"
    )
    assert "9.9.9" in result.detail


def test_the_report_says_an_update_is_waiting(tmp_path):
    """What a user actually sees. An outcome nothing renders is the same defect
    as an outcome that lies."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    run_mod.record(
        run_mod.CheckResult(outcome="deferred", detail="9.9.9 is available"),
        state_root=state_root,
    )
    lines = run_mod.report(state_root=state_root)
    joined = "\n".join(lines)
    assert "deferred" in joined or "available" in joined, joined
    assert "current" not in joined.lower(), (
        f"the report still reads as up to date: {joined}"
    )
