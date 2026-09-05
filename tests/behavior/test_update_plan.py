"""Behavior contracts for deciding what an update would change.

docs/UPDATE_ARCHITECTURE.md §1 and §5 phase 2. The planner is pure: it reads
recorded install state, the staged target tree, and each host's verify evidence,
and returns an ordered list of actions plus the payload class of each. It writes
nothing, so a dry run is exactly a plan with nothing applied.

The classification is the point, not decoration. Class 5 — a change to a host's
hook-set membership — is the only one that mutates host-owned configuration, and
therefore the only one with a real blast radius. A planner that cannot tell it
apart from the rest offers the transaction no way to treat it differently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.aqg_update import plan as plan_mod
from scripts.aqg_update.hosts import base


def _hash_tree(root: Path) -> dict:
    """Path -> content hash. A name listing cannot see an in-place rewrite."""
    import hashlib

    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _tree(root: Path, *, version: str, skills: tuple[str, ...]) -> Path:
    """A staged version tree carrying a VERSION and a skill roster."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / "skills").mkdir(exist_ok=True)
    for name in skills:
        (root / "skills" / name).mkdir(exist_ok=True)
    return root


def _state(**overrides) -> dict:
    payload = {
        "schema": 1,
        "channel": "stable",
        "installed_version": "0.15.0",
        "installed_commit": "a1b2c3d",
        "release_sequence": 7,
        "installed_at": "2026-09-02T12:00:00Z",
        "applied_by": "context-helper",
        "hosts": {
            "claude-code": {
                "last_applied_version": "0.15.0",
                "routed_skills": ["aqg-code-construction", "aqg-security-review"],
            }
        },
        "pending": [],
    }
    payload.update(overrides)
    return payload


def _evidence(status: str, client_id: str = "claude-code") -> base.Evidence:
    return base.Evidence(
        client_id=client_id,
        hooks_status=status,
        hooks_detail="",
        recorded_version="0.15.0",
    )


def _plan(state, tree, evidence, **kw):
    return plan_mod.build_plan(
        state=state,
        target=tree,
        evidence=evidence,
        target_commit=kw.get("target_commit", "a1b2c3d"),
    )


# --- nothing to do ------------------------------------------------------------


def test_an_up_to_date_install_plans_no_actions(tmp_path):
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    result = _plan(
        _state(), tree, {"claude-code": _evidence("complete")}
    )
    assert result.actions == ()
    assert result.has_no_actions and result.is_complete


def test_a_first_run_with_no_state_plans_a_full_route(tmp_path):
    """No record means nothing has been routed, not that everything is current."""
    tree = _tree(tmp_path / "v", version="0.15.0", skills=("aqg-code-construction",))
    result = _plan(
        None, tree, {"claude-code": _evidence("missing")}
    )
    kinds = {action.kind for action in result.actions}
    assert "route_skill" in kinds
    assert not result.has_no_actions


# --- the skill roster diff ------------------------------------------------------


def test_a_new_skill_is_planned_as_a_route(tmp_path):
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review", "aqg-brand-new"),
    )
    result = _plan(
        _state(), tree, {"claude-code": _evidence("complete")}
    )
    routed = [a for a in result.actions if a.kind == "route_skill"]
    assert [a.subject for a in routed] == ["aqg-brand-new"]
    assert routed[0].payload_class == 3


def test_a_dropped_skill_is_planned_as_a_prune(tmp_path):
    tree = _tree(tmp_path / "v", version="0.15.0", skills=("aqg-code-construction",))
    result = _plan(
        _state(), tree, {"claude-code": _evidence("complete")}
    )
    pruned = [a for a in result.actions if a.kind == "prune_skill"]
    assert [a.subject for a in pruned] == ["aqg-security-review"]
    assert pruned[0].payload_class == 4


def test_a_rename_is_planned_as_a_prune_and_a_route(tmp_path):
    """A rename has no signal of its own — it is only ever observable as the
    pair, and only against a recorded previous roster."""
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-audit"),
    )
    result = _plan(
        _state(), tree, {"claude-code": _evidence("complete")}
    )
    by_kind = {a.kind: a.subject for a in result.actions}
    assert by_kind["prune_skill"] == "aqg-security-review"
    assert by_kind["route_skill"] == "aqg-security-audit"


def test_a_version_bump_with_an_unchanged_roster_plans_no_skill_work(tmp_path):
    """Class 2: with skills routed by symlink, edited content is already live,
    so no skill is re-routed. (A hook merge IS planned — the evidence was
    gathered against the old tree; see the target-version test.)"""
    tree = _tree(
        tmp_path / "v",
        version="0.16.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    result = _plan(
        _state(), tree, {"claude-code": _evidence("complete")}
    )
    kinds = [a.kind for a in result.actions]
    assert "route_skill" not in kinds and "prune_skill" not in kinds
    assert kinds[-2:] == ["activate_root", "record_state"]


# --- the host hook state --------------------------------------------------------


def test_a_stale_hook_set_is_planned_as_a_class_five_merge(tmp_path):
    """The only class that mutates host-owned config, and the only one the
    transaction must be able to treat differently."""
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    result = _plan(
        _state(), tree, {"claude-code": _evidence("stale")}
    )
    merges = [a for a in result.actions if a.kind == "merge_hooks"]
    assert merges and merges[0].payload_class == 5
    assert result.touches_host_config


def test_a_missing_hook_set_is_also_a_merge(tmp_path):
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    result = _plan(
        _state(), tree, {"claude-code": _evidence("missing")}
    )
    assert any(a.kind == "merge_hooks" for a in result.actions)


def test_a_host_with_no_hook_surface_is_never_planned_a_merge(tmp_path):
    """`not-applicable` means AQG has no way to install hooks there. Planning one
    would retry it forever."""
    tree = _tree(tmp_path / "v", version="0.15.0", skills=("aqg-code-construction",))
    result = _plan(
        _state(hosts={"zed": {"last_applied_version": "0.15.0",
                              "routed_skills": ["aqg-code-construction"]}}),
        tree,
        {"zed": _evidence("not-applicable", client_id="zed")},
    )
    assert not any(a.kind == "merge_hooks" for a in result.actions)
    assert not result.touches_host_config


def test_an_invalid_host_config_is_not_planned_over(tmp_path):
    """`invalid` means the host's config could not be read. Merging into a file
    we could not parse is how a hand-edited settings file gets destroyed."""
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    result = _plan(
        _state(), tree, {"claude-code": _evidence("invalid")}
    )
    assert not any(a.kind == "merge_hooks" for a in result.actions)
    assert any(item.client_id == "claude-code" for item in result.deferred)


# --- what the planner refuses ----------------------------------------------------


def test_a_target_without_a_version_sentinel_is_refused(tmp_path):
    tree = tmp_path / "v"
    (tree / "skills").mkdir(parents=True)
    with pytest.raises(plan_mod.PlanError, match="VERSION"):
        _plan(_state(), tree, {})


def test_a_target_without_a_skills_directory_is_refused(tmp_path):
    """Reading an absent roster as "no skills" would plan a prune of every
    routed skill on every host."""
    tree = tmp_path / "v"
    tree.mkdir()
    (tree / "VERSION").write_text("0.15.0\n", encoding="utf-8")
    with pytest.raises(plan_mod.PlanError, match="skills"):
        _plan(_state(), tree, {})


def test_a_malformed_routed_skill_list_fails_closed(tmp_path):
    tree = _tree(tmp_path / "v", version="0.15.0", skills=("aqg-code-construction",))
    state = _state(
        hosts={"claude-code": {"last_applied_version": "0.15.0", "routed_skills": "nope"}}
    )
    with pytest.raises(plan_mod.PlanError, match="routed_skills"):
        _plan(state, tree, {"claude-code": _evidence("complete")})


# --- the plan is data, not an act -------------------------------------------------


def test_building_a_plan_writes_nothing(tmp_path):
    tree = _tree(
        tmp_path / "v",
        version="0.16.0",
        skills=("aqg-code-construction", "aqg-brand-new"),
    )
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    _plan(
        _state(), tree, {"claude-code": _evidence("stale")}
    )
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_actions_are_ordered_prunes_before_routes(tmp_path):
    """A rename that routed before pruning would have both names present at
    once, and a prune that then matched the new one would remove it."""
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-audit"),
    )
    result = _plan(
        _state(), tree, {"claude-code": _evidence("complete")}
    )
    kinds = [a.kind for a in result.actions]
    assert kinds.index("prune_skill") < kinds.index("route_skill")


# --- fixes from audit aud__j9t0AZCjYJrBMd2 ------------------------------------


def test_a_new_target_version_forces_a_hook_merge_despite_complete_evidence(tmp_path):
    """`verify` answers against the tree it ran in, so `complete` means "matches
    the OLD canonical hook set". Trusting it for a new target is how a release
    that changes the hook set would install none of it."""
    tree = _tree(
        tmp_path / "v",
        version="0.16.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    result = _plan(_state(), tree, {"claude-code": _evidence("complete")})
    assert any(a.kind == "merge_hooks" for a in result.actions)


def test_a_new_target_commit_alone_also_forces_a_merge(tmp_path):
    """Content can change without the version string moving."""
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    result = _plan(
        _state(), tree, {"claude-code": _evidence("complete")}, target_commit="deadbee"
    )
    assert any(a.kind == "merge_hooks" for a in result.actions)


def test_a_target_roster_read_without_a_sentinel_raises_rather_than_emptying(tmp_path):
    """The empty-roster branch was unreachable through the entry point and its
    comment was false. An empty roster is a mass prune."""
    tree = tmp_path / "v"
    (tree / "skills").mkdir(parents=True)
    with pytest.raises(plan_mod.PlanError, match="VERSION"):
        plan_mod._target_roster(tree)


def test_an_unmodelled_hook_status_is_refused(tmp_path):
    """The dispatch used to fail OPEN: an unknown status meant "nothing to do"."""
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    evidence = {"claude-code": _evidence("complete")}
    object.__setattr__(evidence["claude-code"], "hooks_status", "brand-new")
    with pytest.raises(plan_mod.PlanError, match="brand-new"):
        _plan(_state(), tree, evidence)


@pytest.mark.parametrize("name", ["..", "a/b", "/abs", ".", ""])
def test_an_unsafe_skill_name_in_state_is_refused(tmp_path, name):
    """A prune subject is joined to a route directory by whatever applies it. A
    corrupted state file must not be able to aim that."""
    tree = _tree(tmp_path / "v", version="0.15.0", skills=("aqg-code-construction",))
    state = _state(
        hosts={"claude-code": {"last_applied_version": "0.15.0",
                               "routed_skills": [name]}}
    )
    with pytest.raises(plan_mod.PlanError, match="skill name"):
        _plan(state, tree, {"claude-code": _evidence("complete")})


def test_evidence_filed_under_the_wrong_host_is_refused(tmp_path):
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    with pytest.raises(plan_mod.PlanError, match="claude-code"):
        _plan(_state(), tree, {"claude-code": _evidence("complete", client_id="codex")})


def test_outstanding_pending_work_blocks_planning(tmp_path):
    """Planning over a half-applied previous update treats its rosters as
    settled fact."""
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    state = _state(pending=[{"client_id": "claude-code", "reason": "left over"}])
    with pytest.raises(plan_mod.PlanError, match="pending"):
        _plan(state, tree, {"claude-code": _evidence("complete")})


def test_a_plan_with_deferrals_is_not_complete(tmp_path):
    tree = _tree(
        tmp_path / "v",
        version="0.15.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    result = _plan(_state(), tree, {"claude-code": _evidence("invalid")})
    assert result.has_no_actions
    assert not result.is_complete


def test_the_root_activation_is_part_of_the_plan(tmp_path):
    """Classes 1 and 2 are free BECAUSE the root swap activates them. A plan
    whose correctness rests on a step it does not contain cannot be checked."""
    tree = _tree(
        tmp_path / "v",
        version="0.16.0",
        skills=("aqg-code-construction", "aqg-security-review"),
    )
    result = _plan(_state(), tree, {"claude-code": _evidence("complete")})
    kinds = [a.kind for a in result.actions]
    assert "activate_root" in kinds
    assert kinds.index("activate_root") < kinds.index("record_state")


@pytest.mark.parametrize(
    "state",
    [
        {"hosts": "not a mapping", "installed_version": "0.15.0", "pending": []},
        {"hosts": {"claude-code": "not a mapping"}, "installed_version": "0.15.0",
         "pending": []},
    ],
)
def test_a_malformed_state_shape_is_a_typed_refusal(tmp_path, state):
    """All of these already failed closed — as raw AttributeError/TypeError
    rather than the refusal the docstring promises."""
    tree = _tree(tmp_path / "v", version="0.15.0", skills=("aqg-code-construction",))
    with pytest.raises(plan_mod.PlanError):
        _plan(state, tree, {"claude-code": _evidence("complete")})
