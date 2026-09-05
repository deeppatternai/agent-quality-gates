"""Behavior contracts for executing a plan's actions.

docs/UPDATE_ARCHITECTURE.md §5 phase 5. The dispatcher walks a plan in order and
reports what happened to each action. It owns **mechanics**, not policy: it does
not take the update lock, does not journal, and does not decide what a failure
means. Those belong to the transaction, which is the next slice — and saying so
here is what keeps this module from growing an interface designed against a
consumer that does not exist yet.

Two defaults do the safety work. Execution is **dry by default**, so calling it
wrong reports rather than acts. And an action kind it does not handle is a
refusal, not a skip: the planner and the dispatcher must agree on the whole
vocabulary or the disagreement shows up as work silently not done.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.aqg_update import dispatch as dispatch_mod
from scripts.aqg_update import plan as plan_mod


@pytest.fixture
def world(tmp_path: Path):
    """A staged target tree, a host skills directory, and a root link path."""
    target = tmp_path / "versions" / "v2"
    (target / "skills").mkdir(parents=True)
    (target / "VERSION").write_text("0.16.0\n", encoding="utf-8")
    for name in ("aqg-code-construction", "aqg-brand-new"):
        (target / "skills" / name).mkdir()
    host = tmp_path / "host" / "skills"
    host.mkdir(parents=True)
    return dispatch_mod.Resources(
        target=target,
        root=tmp_path / "agent-quality-gates",
        skills_dest={"claude-code": host},
    )


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


# --- dry by default ---------------------------------------------------------------


def test_nothing_happens_unless_execution_is_asked_for(world):
    """Calling this wrong must report, not act."""
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new")
    )
    outcomes = dispatch_mod.execute(plan, resources=world)
    assert [o.status for o in outcomes] == ["would-apply"]
    assert not (world.skills_dest["claude-code"] / "aqg-brand-new").exists()


def test_a_dry_run_reports_every_action_in_order(world):
    plan = _plan(
        _action("prune_skill", client_id="claude-code", subject="aqg-old"),
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
        _action("activate_root"),
        _action("record_state"),
    )
    outcomes = dispatch_mod.execute(plan, resources=world)
    assert [o.action.kind for o in outcomes] == [
        "prune_skill",
        "route_skill",
        "activate_root",
        "record_state",
    ]


# --- what it actually does --------------------------------------------------------


def test_routing_a_skill_creates_the_route(world):
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new")
    )
    outcomes = dispatch_mod.execute(plan, resources=world, apply=True)
    assert [o.status for o in outcomes] == ["applied"]
    link = world.skills_dest["claude-code"] / "aqg-brand-new"
    assert link.is_symlink()
    assert Path(os.readlink(link)) == world.target / "skills" / "aqg-brand-new"


def test_routing_something_already_routed_reports_no_change(world):
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new")
    )
    dispatch_mod.execute(plan, resources=world, apply=True)
    outcomes = dispatch_mod.execute(plan, resources=world, apply=True)
    assert [o.status for o in outcomes] == ["unchanged"]


def test_pruning_removes_our_route(world):
    route = _action("route_skill", client_id="claude-code", subject="aqg-brand-new")
    dispatch_mod.execute(_plan(route), resources=world, apply=True)
    prune = _action("prune_skill", client_id="claude-code", subject="aqg-brand-new")
    outcomes = dispatch_mod.execute(_plan(prune), resources=world, apply=True)
    assert [o.status for o in outcomes] == ["applied"]
    assert not (world.skills_dest["claude-code"] / "aqg-brand-new").exists()


def test_pruning_something_that_is_not_ours_reports_no_change(world):
    """The routing layer refuses; the dispatcher must report that as a
    non-event rather than a failure, or every run would look broken."""
    theirs = world.skills_dest["claude-code"] / "aqg-brand-new"
    theirs.mkdir()
    prune = _action("prune_skill", client_id="claude-code", subject="aqg-brand-new")
    outcomes = dispatch_mod.execute(_plan(prune), resources=world, apply=True)
    assert [o.status for o in outcomes] == ["unchanged"]
    assert theirs.is_dir()


def test_activating_the_root_points_it_at_the_target(world):
    plan = _plan(_action("activate_root"))
    dispatch_mod.execute(plan, resources=world, apply=True)
    assert Path(os.readlink(world.root)) == world.target


# --- what it refuses or sets aside -------------------------------------------------


def test_a_hook_merge_is_deferred_because_no_adapter_can_apply_one_yet(world):
    """Reporting it as done, or as failed, would both be false. Deferring names
    the actual state: the verb does not exist."""
    plan = _plan(
        _action("merge_hooks", client_id="claude-code", payload_class=5)
    )
    outcomes = dispatch_mod.execute(plan, resources=world, apply=True)
    assert [o.status for o in outcomes] == ["deferred"]
    assert "apply" in outcomes[0].detail


def test_an_unknown_action_kind_is_refused(world):
    """The planner and the dispatcher must agree on the whole vocabulary. A
    silent skip is work not done that nobody reports."""
    plan = _plan(_action("teleport_skill", client_id="claude-code"))
    with pytest.raises(dispatch_mod.DispatchError, match="teleport_skill"):
        dispatch_mod.execute(plan, resources=world, apply=True)


def test_an_action_for_a_host_with_no_destination_is_refused(world):
    plan = _plan(_action("route_skill", client_id="codex", subject="aqg-brand-new"))
    with pytest.raises(dispatch_mod.DispatchError, match="codex"):
        dispatch_mod.execute(plan, resources=world, apply=True)


def test_a_skill_action_without_a_subject_is_refused(world):
    plan = _plan(_action("route_skill", client_id="claude-code"))
    with pytest.raises(dispatch_mod.DispatchError, match="subject"):
        dispatch_mod.execute(plan, resources=world, apply=True)


# --- failure stops, and says where -------------------------------------------------


def test_a_failure_stops_the_run_and_reports_what_ran(world):
    """Continuing past a failure is a policy decision about partially applied
    state, and policy is the transaction's. The dispatcher stops and hands back
    exactly how far it got."""
    blocked = world.skills_dest["claude-code"] / "aqg-code-construction"
    blocked.mkdir()  # a real directory: routing over it is refused
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
        _action("route_skill", client_id="claude-code", subject="aqg-code-construction"),
        _action("activate_root"),
    )
    outcomes = dispatch_mod.execute(plan, resources=world, apply=True)
    assert [o.status for o in outcomes] == ["applied", "failed"]
    assert not world.root.exists()  # the third action never ran


def test_a_failure_names_the_action_that_failed(world):
    blocked = world.skills_dest["claude-code"] / "aqg-code-construction"
    blocked.mkdir()
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-code-construction")
    )
    outcomes = dispatch_mod.execute(plan, resources=world, apply=True)
    assert outcomes[0].status == "failed"
    assert "aqg-code-construction" in outcomes[0].detail


# --- the boundary it does not cross ------------------------------------------------


def test_recording_state_is_left_to_the_transaction(world):
    """The dispatcher has no state document and no lock. Writing install state
    from here would record an apply the transaction has not committed."""
    plan = _plan(_action("record_state"))
    outcomes = dispatch_mod.execute(plan, resources=world, apply=True)
    assert [o.status for o in outcomes] == ["deferred"]
    assert "transaction" in outcomes[0].detail


# --- fixes from audit aud_jMldCyK5pm_juzf6 ------------------------------------
#
# Q5: every refusal case above used a ONE-ACTION plan, which makes "a refusal
# after something already applied" unreachable by construction — and that is the
# one path where the module broke its own promise.


def test_a_refusal_after_a_successful_action_still_reports_what_ran(world):
    """The module promises to hand back exactly how far it got. A DispatchError
    raised mid-walk used to discard the outcomes entirely."""
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
        _action("route_skill", client_id="codex", subject="aqg-brand-new"),
    )
    with pytest.raises(dispatch_mod.DispatchError, match="codex"):
        dispatch_mod.execute(plan, resources=world, apply=True)
    # ...and because the refusal is caught in preflight, nothing ran at all.
    assert not (world.skills_dest["claude-code"] / "aqg-brand-new").exists()


def test_preflight_refuses_before_any_action_mutates(world):
    """A plan that cannot be completed must not be half-executed."""
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
        _action("route_skill", client_id="claude-code"),  # no subject
    )
    with pytest.raises(dispatch_mod.DispatchError, match="subject"):
        dispatch_mod.execute(plan, resources=world, apply=True)
    assert not (world.skills_dest["claude-code"] / "aqg-brand-new").exists()


def test_an_unexpected_failure_still_carries_what_had_been_applied(world, monkeypatch):
    """An exception type this layer has no model for SHOULD abort — but the
    outcomes it earned are exactly what a transaction needs to decide rollback,
    so they travel with it."""
    plan = _plan(
        _action("route_skill", client_id="claude-code", subject="aqg-brand-new"),
        _action("activate_root"),
    )

    def _boom(**_kw):
        raise MemoryError("something this layer has no model for")

    monkeypatch.setattr(dispatch_mod.stage, "swap_root", _boom)
    with pytest.raises(MemoryError) as excinfo:
        dispatch_mod.execute(plan, resources=world, apply=True)
    earned = getattr(excinfo.value, "aqg_outcomes", None)
    assert earned is not None
    assert [o.status for o in earned] == ["applied"]


def test_activating_an_already_current_root_reports_no_change(world):
    plan = _plan(_action("activate_root"))
    dispatch_mod.execute(plan, resources=world, apply=True)
    outcomes = dispatch_mod.execute(plan, resources=world, apply=True)
    assert [o.status for o in outcomes] == ["unchanged"]


def test_a_dry_run_reports_permanently_deferred_kinds_as_deferred(world):
    """A dry run whose report differs from the apply it previews is worth less
    than no dry run."""
    plan = _plan(
        _action("merge_hooks", client_id="claude-code", payload_class=5),
        _action("record_state"),
    )
    assert [o.status for o in dispatch_mod.execute(plan, resources=world)] == [
        "deferred",
        "deferred",
    ]


def test_a_dry_run_also_refuses_a_plan_that_could_not_be_applied(world):
    plan = _plan(_action("route_skill", client_id="codex", subject="aqg-brand-new"))
    with pytest.raises(dispatch_mod.DispatchError, match="codex"):
        dispatch_mod.execute(plan, resources=world)


@pytest.mark.parametrize("field", ["target", "root", "dest"])
def test_a_relative_resource_path_is_refused(tmp_path, field):
    """A relative path resolves against the ambient cwd — a path nobody named,
    which is the whole reason resources are handed over rather than discovered."""
    absolute = tmp_path / "x"
    absolute.mkdir()
    kwargs = {
        "target": absolute,
        "root": absolute / "root",
        "skills_dest": {"claude-code": absolute / "skills"},
    }
    kwargs[{"target": "target", "root": "root"}.get(field, "skills_dest")] = (
        Path("relative")
        if field != "dest"
        else {"claude-code": Path("relative")}
    )
    with pytest.raises(dispatch_mod.DispatchError, match="absolute"):
        dispatch_mod.Resources(**kwargs)
