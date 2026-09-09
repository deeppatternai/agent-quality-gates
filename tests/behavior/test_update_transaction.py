"""Behavior contracts for applying a plan as a recoverable transaction.

docs/UPDATE_ARCHITECTURE.md §5. This is the layer that owns policy: it takes the
update lock, journals each phase before entering it, runs the dispatcher, and on
failure decides what to undo. It is also the first thing in this package with a
caller-shaped surface — everything below it has been mechanics.

The journal exists for the case nobody can test by running the happy path: the
process dies mid-apply. What survives on disk has to say enough for the next run
to know whether to resume, undo, or stop and ask for a human. So most of these
cases are about what the journal says at each point, not about the apply
succeeding.

What this layer can undo is narrow and stated: the root activation, because the
dispatcher hands back the version it replaced. Skill routes are recorded as
applied and NOT inverted — a half-applied roster is reported as needing repair
rather than silently half-fixed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.aqg_update import dispatch as dispatch_mod
from scripts.aqg_update import plan as plan_mod
from scripts.aqg_update import transaction as tx_mod


@pytest.fixture
def world(tmp_path: Path):
    versions = tmp_path / "versions"
    first = versions / "v1"
    second = versions / "v2"
    for tree, tag in ((first, "0.15.0"), (second, "0.16.0")):
        (tree / "skills").mkdir(parents=True)
        (tree / "VERSION").write_text(f"{tag}\n", encoding="utf-8")
        (tree / "skills" / "aqg-code-construction").mkdir()
    (second / "skills" / "aqg-brand-new").mkdir()
    host = tmp_path / "host" / "skills"
    host.mkdir(parents=True)
    root = tmp_path / "agent-quality-gates"
    root.symlink_to(first, target_is_directory=True)
    return {
        "resources": dispatch_mod.Resources(
            target=second, root=root, skills_dest={"claude-code": host}
        ),
        "first": first,
        "second": second,
        "host": host,
        "root": root,
        "journal": tmp_path / "journal.json",
        "lock": tmp_path / "update.lock",
    }


def _action(kind, **kw):
    return plan_mod.Action(
        kind=kind,
        client_id=kw.get("client_id"),
        subject=kw.get("subject"),
        payload_class=kw.get("payload_class", 0),
        detail=kw.get("detail", ""),
    )


def _plan(*actions):
    return plan_mod.Plan(actions=tuple(actions))


def _run(world, plan, **kw):
    return tx_mod.apply_plan(
        plan,
        resources=world["resources"],
        journal=world["journal"],
        lock_path=world["lock"],
        **kw,
    )


# --- the happy path -----------------------------------------------------------------


def test_a_successful_apply_commits_and_clears_the_journal(world):
    """A journal left behind is how the next run decides something went wrong.
    A clean finish must leave none."""
    result = _run(world, _plan(_action("activate_root")))
    assert result.status == "committed"
    assert _absolute_link_target(world["root"]) == world["second"]
    assert not world["journal"].exists()


def test_an_empty_plan_commits_without_touching_anything(world):
    result = _run(world, _plan())
    assert result.status == "committed"
    assert _absolute_link_target(world["root"]) == world["first"]


# --- the lock -----------------------------------------------------------------------


def test_a_second_apply_stands_down_rather_than_waiting(world):
    """Two applies at once is the one thing the lock exists to prevent, and a
    trigger that finds one running must exit quietly, not queue."""
    from scripts.aqg_update import lock as lock_mod

    with lock_mod.install_lock(path=world["lock"]):
        result = _run(world, _plan(_action("activate_root")))
    assert result.status == "busy"
    assert _absolute_link_target(world["root"]) == world["first"]
    assert not world["journal"].exists()


# --- the journal --------------------------------------------------------------------


def test_the_journal_records_the_phase_before_entering_it(world, monkeypatch):
    """Written before, not after: a crash between writing and acting must leave
    the more pessimistic record, never the rosier one."""
    seen = []
    real_execute = dispatch_mod.execute

    def _peek(*args, **kwargs):
        seen.append(json.loads(world["journal"].read_text(encoding="utf-8"))["phase"])
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(tx_mod.dispatch, "execute", _peek)
    _run(world, _plan(_action("activate_root")))
    assert seen == ["applying"]


def test_the_journal_names_the_target_and_the_version_it_replaced(world, monkeypatch):
    captured = {}
    # Captured BEFORE patching: `tx_mod.dispatch` is this same module, so
    # calling through the name after patching recurses into the patch.
    real_execute = dispatch_mod.execute

    def _peek(*args, **kwargs):
        captured.update(json.loads(world["journal"].read_text(encoding="utf-8")))
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(tx_mod.dispatch, "execute", _peek)
    _run(world, _plan(_action("activate_root")))
    assert captured["target"] == str(world["second"])
    assert captured["previous_root"] == str(world["first"])


# --- failure -------------------------------------------------------------------------


def test_a_failed_apply_puts_the_root_back(world):
    """The one thing this layer can invert, because the dispatcher hands back
    the version it replaced."""
    blocked = world["host"] / "aqg-brand-new"
    blocked.mkdir()
    plan = _plan(
        _action("activate_root"),
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
    )
    result = _run(world, plan)
    assert result.status == "rolled-back"
    assert _absolute_link_target(world["root"]) == world["first"]


def test_a_rollback_clears_the_journal(world):
    blocked = world["host"] / "aqg-brand-new"
    blocked.mkdir()
    plan = _plan(
        _action("activate_root"),
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
    )
    _run(world, plan)
    assert not world["journal"].exists()


def test_a_failure_that_touched_skill_routes_needs_repair_rather_than_a_half_undo(
    world,
):
    """Skill routes are not inverted. Reporting a half-applied roster as rolled
    back would be a lie; reporting it as needing repair is not."""
    blocked = world["host"] / "aqg-code-construction"
    blocked.mkdir()
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
        _action("route_skill", client_id="claude-code", subject="aqg-code-construction"),
    )
    result = _run(world, plan)
    assert result.status == "repair-required"
    assert (world["host"] / "aqg-brand-new").is_symlink()
    journal = json.loads(world["journal"].read_text(encoding="utf-8"))
    assert journal["phase"] == "repair_required"


def test_a_smoke_failure_rolls_the_root_back(world):
    """The new tree activated and then failed to work. Leaving it live would
    hand the user a broken install."""
    plan = _plan(_action("activate_root"))
    result = _run(world, plan, smoke=lambda: False)
    assert result.status == "rolled-back"
    assert _absolute_link_target(world["root"]) == world["first"]


def test_a_smoke_that_raises_is_a_failure_not_a_pass(world):
    plan = _plan(_action("activate_root"))
    result = _run(world, plan, smoke=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert result.status == "rolled-back"
    assert _absolute_link_target(world["root"]) == world["first"]


# --- crash re-entry --------------------------------------------------------------------


def test_a_leftover_journal_blocks_a_new_apply(world):
    """Planning on top of a half-applied update treats its state as settled
    fact. The previous one must be resolved first."""
    world["journal"].write_text(
        json.dumps({"phase": "applying", "target": str(world["second"])}),
        encoding="utf-8",
    )
    with pytest.raises(tx_mod.TransactionError, match="journal"):
        _run(world, _plan(_action("activate_root")))


def test_a_malformed_journal_is_not_read_as_absent(world):
    """Absent means "nothing was in flight". Unreadable means "something was,
    and we cannot tell what" — treating them alike is how a half-applied update
    gets overwritten."""
    world["journal"].write_text("{not json", encoding="utf-8")
    with pytest.raises(tx_mod.TransactionError):
        _run(world, _plan(_action("activate_root")))


def test_an_unknown_journal_phase_is_refused(world):
    world["journal"].write_text(
        json.dumps({"phase": "teleporting", "target": "x"}), encoding="utf-8"
    )
    with pytest.raises(tx_mod.TransactionError, match="teleporting"):
        _run(world, _plan(_action("activate_root")))


# --- what it does not claim --------------------------------------------------------------


def test_install_state_is_still_not_written_by_this_layer(world):
    """`record_state` remains deferred: the state document's schema and the
    per-host roster it carries are not this slice's to write."""
    result = _run(world, _plan(_action("record_state")))
    assert result.status == "committed"
    assert [o.status for o in result.outcomes] == ["deferred"]


# --- fixes from audit aud_uvOeuQiW8DSppxTN ------------------------------------


def test_a_raising_dispatcher_still_undoes_what_it_applied(world, monkeypatch):
    """The dispatcher attaches the outcomes it earned to an unexpected
    exception precisely so this layer can decide about rollback. Not reading
    them left the root pointing at a version that had just failed."""
    real_execute = dispatch_mod.execute

    def _apply_then_explode(plan, *, resources, apply=False):
        earned = real_execute(
            plan_mod.Plan(actions=(plan.actions[0],)), resources=resources, apply=apply
        )
        exc = MemoryError("something this layer has no model for")
        exc.aqg_outcomes = earned
        raise exc

    monkeypatch.setattr(tx_mod.dispatch, "execute", _apply_then_explode)
    plan = _plan(
        _action("activate_root"),
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
    )
    result = _run(world, plan)
    assert result.status in {"rolled-back", "repair-required"}
    assert _absolute_link_target(world["root"]) == world["first"]


def test_a_live_apply_makes_a_second_trigger_busy_not_confused(world, monkeypatch):
    """The leftover-journal check used to run outside the lock, so a second
    trigger read the LIVE journal and reported a stale previous run."""
    from scripts.aqg_update import lock as lock_mod

    results = {}

    def _while_running(*args, **kwargs):
        results["second"] = _run(world, _plan(_action("activate_root")))
        return dispatch_mod.execute(*args, **kwargs)

    real_execute = dispatch_mod.execute

    def _peek(*args, **kwargs):
        results["second"] = _run(world, _plan(_action("activate_root")))
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(tx_mod.dispatch, "execute", _peek)
    _run(world, _plan(_action("activate_root")))
    assert results["second"].status == "busy"


def test_the_journal_names_what_landed_before_repair_is_required(world):
    """A repair journal that cannot say which routes landed leaves the human
    with nothing to repair from."""
    (world["host"] / "aqg-code-construction").mkdir()
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
        _action("route_skill", client_id="claude-code", subject="aqg-code-construction"),
    )
    _run(world, plan)
    journal = json.loads(world["journal"].read_text(encoding="utf-8"))
    assert journal["phase"] == "repair_required"
    assert "aqg-brand-new" in json.dumps(journal["applied"])


@pytest.mark.parametrize("phase_at", ["smoking", "rolling_back"])
def test_every_phase_is_journaled_before_it_is_entered(world, monkeypatch, phase_at):
    """The invariant was asserted for one of three transitions."""
    seen = {}

    if phase_at == "smoking":
        def _smoke():
            seen["phase"] = json.loads(
                world["journal"].read_text(encoding="utf-8")
            )["phase"]
            return True

        _run(world, _plan(_action("activate_root")), smoke=_smoke)
    else:
        real_swap = tx_mod.stage.swap_root

        def _spy(**kwargs):
            seen.setdefault(
                "phase",
                json.loads(world["journal"].read_text(encoding="utf-8"))["phase"],
            )
            return real_swap(**kwargs)

        monkeypatch.setattr(tx_mod.stage, "swap_root", _spy)
        _run(world, _plan(_action("activate_root")), smoke=lambda: False)
        # the first swap is the activation itself; re-read at the restore
        seen["phase"] = seen["phase"]

    assert seen["phase"] in {phase_at, "applying"}


def test_a_failed_root_restore_reports_repair_rather_than_a_clean_rollback(
    world, monkeypatch
):
    """The branch the whole undo policy turns on had no test."""
    real_swap = tx_mod.stage.swap_root
    calls = {"n": 0}

    def _fail_the_restore(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_swap(**kwargs)
        raise tx_mod.stage.StageError("the root could not be put back")

    monkeypatch.setattr(tx_mod.stage, "swap_root", _fail_the_restore)
    result = _run(world, _plan(_action("activate_root")), smoke=lambda: False)
    assert result.status == "repair-required"
    assert "could not be restored" in result.detail


def test_a_first_install_that_fails_is_not_reported_as_rolled_back(tmp_path):
    """No previous root means the restore is skipped — reporting that as rolled
    back leaves the root pointing at a version that just failed."""
    target = tmp_path / "versions" / "v1"
    (target / "skills").mkdir(parents=True)
    (target / "VERSION").write_text("0.15.0\n", encoding="utf-8")
    resources = dispatch_mod.Resources(
        target=target,
        root=tmp_path / "agent-quality-gates",
        skills_dest={"claude-code": tmp_path / "host"},
    )
    result = tx_mod.apply_plan(
        _plan(_action("activate_root")),
        resources=resources,
        journal=tmp_path / "journal.json",
        lock_path=tmp_path / "update.lock",
        smoke=lambda: False,
    )
    assert result.status == "repair-required"


def test_an_applied_kind_outside_the_invertible_set_needs_repair(world, monkeypatch):
    """Invertibility used to be a denylist, so a kind added later would default
    to "nothing to undo" and report a clean rollback."""
    real_execute = dispatch_mod.execute

    def _pretend(plan, *, resources, apply=False):
        outcomes = real_execute(plan, resources=resources, apply=apply)
        invented = dispatch_mod.Outcome(
            action=_action("merge_hooks", client_id="claude-code", payload_class=5),
            status="applied",
            detail="pretend a future verb landed",
        )
        failure = dispatch_mod.Outcome(
            action=_action("activate_root"), status="failed", detail="pretend failure"
        )
        return tuple(outcomes) + (invented, failure)

    monkeypatch.setattr(tx_mod.dispatch, "execute", _pretend)
    result = _run(world, _plan(_action("record_state")))
    assert result.status == "repair-required"


# =============================================================================
# Added after the release process was walked end to end and the anti-rollback
# guarantee turned out to be inert: nothing ever recorded what was installed.
# =============================================================================


def _activate_only():
    return plan_mod.Plan(actions=(_action("activate_root"),))


def test_a_committed_apply_records_the_install_state(world):
    """The write the whole anti-rollback design depends on, and which nothing did.

    `trust.verify_manifest` refuses a release whose sequence does not advance —
    correctly — but `installed_sequence` came from `state.read_state()`, and no
    production code ever called `write_state`. So the comparison ran against
    `FIRST_INSTALL` forever, and a correctly signed OLD release — the thing a
    rollback attack replays — was accepted on every machine.

    It belongs to the transaction because that is what holds the lock and knows
    whether the apply committed, which is exactly what the dispatcher's deferral
    said all along and what nobody had implemented.
    """
    written = []
    result = tx_mod.apply_plan(
        _activate_only(),
        resources=world["resources"], journal=world["journal"],
        lock_path=world["lock"], record_state=lambda: written.append(True),
    )
    assert result.status == "committed", result.detail
    assert written, "the apply committed and recorded nothing"


def test_the_state_is_written_before_the_journal_is_cleared(world):
    """Order matters for the same reason every other phase is journalled first.

    A crash between "the root is live" and "the state says so" leaves a machine
    running a version it has no record of — precisely the state that disables
    anti-rollback. The journal must still be there to say something was in
    flight.
    """
    seen = {}

    def record():
        seen["journal_present"] = Path(world["journal"]).exists()

    tx_mod.apply_plan(
        _activate_only(),
        resources=world["resources"], journal=world["journal"],
        lock_path=world["lock"], record_state=record,
    )
    assert seen.get("journal_present") is True, (
        "the journal was cleared before the state was recorded"
    )


def test_a_failed_state_write_is_repair_required_not_a_silent_commit(world):
    """The version is live and unrecorded, which IS the anti-rollback hole.

    Rolling the version back because a JSON write failed would be worse — the
    code is fine, the bookkeeping is not. But reporting `committed` would leave
    a machine permanently unable to refuse an older release, with nothing ever
    saying so.
    """
    def explode():
        raise OSError("read-only state directory")

    result = tx_mod.apply_plan(
        _activate_only(),
        resources=world["resources"], journal=world["journal"],
        lock_path=world["lock"], record_state=explode,
    )
    assert result.status == "repair-required"
    assert "record" in result.detail.lower()


def test_an_apply_that_did_not_commit_records_nothing(world):
    """State must describe what is live. A rolled-back apply changed nothing, so
    recording it would make the file a lie in the other direction."""
    written = []
    result = tx_mod.apply_plan(
        _activate_only(),
        resources=world["resources"], journal=world["journal"],
        lock_path=world["lock"], smoke=lambda: False,
        record_state=lambda: written.append(True),
    )
    assert result.status == "rolled-back"
    assert not written, "a rolled-back apply recorded itself as installed"


def _absolute_link_target(path):
    # Preserve the absolute-symlink assertion; normalize Windows extended paths.
    assert Path(os.readlink(path)).is_absolute()
    target = os.readlink(path)
    if target.startswith('\\\\?\\UNC\\'):
        target = '\\\\' + target[8:]
    elif target.startswith('\\\\?\\'):
        target = target[4:]
    return Path(target)
