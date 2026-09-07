"""A check started by a skill must not swap the tree that skill is reading.

audit aud_Y3qcP8c7jPy4xs_H finding F14. The trigger lives in
`scripts/_aqg_context.sh`, which every skill sources to resolve `$aqg_root` —
and then immediately uses that path to run scripts and read templates out of.
Starting an update at that moment means the swap can land between one
dereference and the next, leaving a skill running with the script from one
version and the template from another. Silent, and the hardest possible shape
to diagnose.

The two paths are not in the same position:

* **session start** (the hook) runs before any skill does. Nothing is reading
  the tree, so applying is safe and stays unchanged.
* **skill execution** (the helper) runs while something is. It checks, and
  leaves the swap to the next session start — or, on a host with no hooks at
  all, to the next skill invocation, by which time the previous one is over.

The cost is that an update can take one session longer to take effect. That is
cheap next to a half-old tree nobody can see.

A second, independent defect surfaced while fixing this: `check(apply=False)`
returned `outcome="current"` with a detail saying an update was available and
not applied. `report()` reads only the outcome, so `doctor` told a machine with
a pending update that it was up to date.
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


# --- R1: the skill-side trigger does not apply --------------------------------

def test_the_skill_trigger_asks_for_a_check_only(tmp_path):
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
    assert "--check-only" in seen["argv"], (
        f"a skill-started check would swap the tree the skill is reading: "
        f"{seen['argv']}"
    )


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
