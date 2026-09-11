"""A host whose hook command does not follow the root must not stop the update.

`HOST_TOUCHING_KINDS` blocks the whole apply for one stated reason
(`run.py:474`): swapping the root while a hook change waits would leave the
host "describing the old tree and serving the new one", and an AQG hook whose
script vanished from under the root fails into ``|| true`` — the guardrail
stops running and says nothing.

That reason is a property of how a host SPELLS its hook command, and it is
false for codex. Codex's command names an absolute path into
``versions/<sha>/`` and carries ``--bundle-sha256``; the command re-hashes the
files it is about to run and refuses (``sys.exit(3)``) on a mismatch. That is a
trust-on-first-use pin, deliberately: a new version's hook code must be
re-approved in Codex ``/hooks``. So after a root swap codex does not serve the
new tree at all — it keeps executing the old one, intact and integrity-checked.

Nothing vanishes, so nothing needs to be held back. Today it is held back
anyway, and because one host-touching action zeroes the entire apply, a
machine with codex installed can never complete an update — silently, with no
instruction to the user about the approval that would clear it.

The deferral is only sound while the pinned tree still exists, which is why
`pinned_command_paths` is checked here AND enforced in `prune_versions`: each
use verifies the other's assumption rather than trusting it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from scripts.aqg_update import plan as plan_mod
from scripts.aqg_update import run as run_mod
from scripts.aqg_update.hosts.base import HostAdapter


@pytest.fixture(autouse=True)
def _own_state_root(tmp_path, monkeypatch):
    """Every file this module's tests write goes to a tmp root, not $HOME.

    `AQG_STATE_ROOT` redirects install state, the update journal and the
    last-check record together (state.py:147). Without it these tests write the
    developer's REAL install record — that is how a run of this suite came to
    leave `release_sequence` at 0 (the anti-rollback floor) and a
    `repair_required` journal that would refuse the next genuine update.
    """
    root = tmp_path / "aqg-state"
    root.mkdir()
    monkeypatch.setenv("AQG_STATE_ROOT", str(root))
    return root


def _plan_with(kind: str, client_id: str) -> plan_mod.Plan:
    return plan_mod.Plan(
        actions=(
            plan_mod.Action(kind="activate_root", client_id=None, subject=None,
                            payload_class=1, detail=""),
            plan_mod.Action(kind=kind, client_id=client_id, subject=None,
                            payload_class=5, detail="managed hook set is stale"),
        ),
        deferred=(),
    )


def test_a_pinned_host_does_not_stop_the_root_swap(tmp_path):
    """R1. The whole point: codex needing approval must not freeze the machine."""
    pinned = tmp_path / "versions" / ("a" * 40) / "scripts" / "run_aqg_codex_hook.py"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("# runner\n", encoding="utf-8")

    blocking, deferrable = run_mod._split_outstanding(
        _plan_with("merge_hooks", "codex").actions,
        root_relative={"codex": False},
        pinned={"codex": (pinned,)},
    )
    assert not blocking, f"a version-pinned host still froze the apply: {blocking}"
    assert [a.client_id for a in deferrable] == ["codex"]


def test_a_root_relative_host_still_stops_it(tmp_path):
    """R3. The rule this narrows must keep holding where its reason holds.

    Given EXISTING pins on purpose. With empty pins this test passes whether or
    not the root-relative check is consulted at all — which is what it did, and
    a mutation deleting that check left it green. The only thing that may save
    a host here is the answer to "does your command follow the root".
    """
    existing = tmp_path / "runner.py"
    existing.write_text("# runner\n", encoding="utf-8")
    blocking, deferrable = run_mod._split_outstanding(
        _plan_with("merge_hooks", "claude-code").actions,
        root_relative={"claude-code": True},
        pinned={"claude-code": (existing,)},
    )
    assert [a.client_id for a in blocking] == ["claude-code"]
    assert not deferrable


def test_a_pinned_host_whose_tree_is_gone_still_stops_it(tmp_path):
    """R2. Deferral is sound only while the pinned tree survives.

    If the paths the command executes are gone, deferring would leave that host
    with no working guardrail at all — the exact silent failure the blocking
    rule exists to prevent, reached by relaxing it.
    """
    blocking, deferrable = run_mod._split_outstanding(
        _plan_with("merge_hooks", "codex").actions,
        root_relative={"codex": False},
        pinned={"codex": (tmp_path / "versions" / "gone" / "runner.py",)},
    )
    assert [a.client_id for a in blocking] == ["codex"], (
        "a host whose pinned tree no longer exists was deferred anyway"
    )
    assert not deferrable


def test_a_pinned_host_that_names_no_paths_still_stops_it():
    """Fail closed. 'Pins nothing' is not evidence that a swap is safe for it —
    it is the default for every adapter that has not been thought about."""
    blocking, _ = run_mod._split_outstanding(
        _plan_with("merge_hooks", "someone-new").actions,
        root_relative={"someone-new": False},
        pinned={"someone-new": ()},
    )
    assert [a.client_id for a in blocking] == ["someone-new"]


@pytest.mark.parametrize("kind", ["route_skill", "prune_skill"])
def test_skill_actions_are_never_deferrable(kind, tmp_path):
    """Skills live UNDER the root, so they follow the swap for every host no
    matter how that host spells its hook command. Only the hook set is at issue."""
    pinned = tmp_path / "r.py"
    pinned.write_text("x", encoding="utf-8")
    blocking, deferrable = run_mod._split_outstanding(
        _plan_with(kind, "codex").actions,
        root_relative={"codex": False},
        pinned={"codex": (pinned,)},
    )
    assert [a.client_id for a in blocking] == ["codex"], (
        f"{kind} was deferred; a skill route does not survive a root swap"
    )
    assert not deferrable


def test_an_adapter_pins_nothing_unless_it_says_so():
    """The default on the contract, so a host nobody thought about fails closed."""

    class _Plain(HostAdapter):
        client_id = "plain"

        def _inspect(self, root=None):
            return "complete", ""

    assert _Plain().pinned_command_paths() == ()


def test_codex_reports_the_paths_its_installed_command_executes(tmp_path):
    """Read from the host's own file through the ownership parser, not a regex.

    A regex over the command would also match an unmanaged hook a user wrote,
    and protecting or deferring on somebody else's command is how AQG would
    start making decisions about configuration it does not own.
    """
    from scripts.aqg_update.hosts.codex import CodexAdapter
    from scripts import install_aqg_codex_hooks as codex_installer

    import shutil

    repo = Path(__file__).resolve().parents[2]
    root = (tmp_path / "versions" / ("b" * 40)).resolve()
    (root / "scripts").mkdir(parents=True)
    (root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    shutil.copy(repo / "scripts" / "run_aqg_codex_hook.py", root / "scripts")
    shutil.copytree(repo / "agent-packs" / "claude-code" / "hooks",
                    root / "agent-packs" / "claude-code" / "hooks")
    hooks_path = tmp_path / "hooks.json"

    assert codex_installer.cmd_apply(hooks_path, root, Path(sys.executable)) == 0
    paths = CodexAdapter(hooks_path=hooks_path, aqg_root=root).pinned_command_paths()

    assert paths, "codex reported no pinned paths despite an installed hook set"
    assert all(str(root) in str(p) for p in paths), (
        f"codex reported paths outside the tree it was installed from: {paths}"
    )


def test_a_codex_command_through_the_root_link_cannot_defer_a_hook_change(tmp_path):
    """A digest pins bytes; only a physical command path also pins a tree."""
    import shutil
    from scripts import install_aqg_codex_hooks as installer
    from scripts.aqg_update.hosts.codex import CodexAdapter

    repo = Path(__file__).resolve().parents[2]
    physical = tmp_path / "versions" / ("c" * 40)
    (physical / "scripts").mkdir(parents=True)
    (physical / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    shutil.copyfile(repo / "scripts/run_aqg_codex_hook.py", physical / "scripts/run_aqg_codex_hook.py")
    shutil.copytree(repo / "agent-packs/claude-code/hooks", physical / "agent-packs/claude-code/hooks")
    root = tmp_path / "agent-quality-gates"
    root.symlink_to(physical, target_is_directory=True)
    config = tmp_path / "hooks.json"
    installer.cmd_apply(config, root, Path(sys.executable))
    pins = CodexAdapter(hooks_path=config, aqg_root=root).pinned_command_paths()
    blocking, deferrable = run_mod._split_outstanding(
        _plan_with("merge_hooks", "codex").actions,
        root_relative={"codex": False}, pinned={"codex": pins},
    )
    assert [action.client_id for action in blocking] == ["codex"]
    assert not deferrable


def test_pruning_never_removes_a_tree_a_host_is_pinned_to(tmp_path, monkeypatch):
    """R4. Structurally, not by asking the caller to remember.

    The live tree is already protected this way (`stage.py:373`). A tree that a
    host's hook command executes is protected for the same reason and must not
    depend on a future caller passing it in — the caller that eventually wires
    pruning up will not know codex exists.
    """
    from scripts.aqg_update import stage as stage_mod
    from tests.behavior.test_update_run import _install

    root, first, _ = _install(tmp_path)
    versions = root.resolve().parent
    for name in ("old", "older", "newest"):
        stage_mod.stage_version(repo=root, commit=first, versions_dir=versions, name=name)

    pinned = versions / "older" / "scripts" / "_aqg_context.sh"
    monkeypatch.setattr(stage_mod, "_host_pinned_paths", lambda: (pinned,))

    removed = stage_mod.prune_versions(
        versions_dir=versions, keep=0, protected=(), repo=root
    )
    assert versions / "older" not in removed, (
        "pruned the version tree a host's hook command executes; those hooks "
        "would then fail on every tool call with nothing having warned anyone"
    )
    assert (versions / "older").exists()
    assert not (versions / "old").exists()
    assert not (versions / "newest").exists()


def test_apply_really_swaps_the_root_when_only_a_pinned_host_is_outstanding(
    tmp_path, monkeypatch
):
    """The wire, end to end, because the rule alone is worth nothing.

    Every other test here calls `_split_outstanding` directly. A mutation that
    computed the split correctly and then never used it would leave all of them
    green and the machine exactly as stalled as before — the shape of hole this
    workstream has now shipped three times, so it is pinned at the layer that
    actually decides.
    """
    import subprocess

    from tests.behavior.test_upgrade_engine import _origin_and_install

    origin, install, first, second = _origin_and_install(tmp_path)

    def a_stale_pinned_host(*, target_commit, **kwargs):
        return run_mod.plan_mod.Plan(
            actions=(
                run_mod.plan_mod.Action(
                    kind="activate_root", client_id=None, subject=None,
                    payload_class=0, detail=f"activate {target_commit[:12]}",
                ),
                run_mod.plan_mod.Action(
                    kind="merge_hooks", client_id="codex", subject=None,
                    payload_class=5, detail="managed hook set is stale",
                ),
            ),
            deferred=(),
        )

    pinned = tmp_path / "pinned-runner.py"
    pinned.write_text("# runner\n", encoding="utf-8")
    monkeypatch.setattr(run_mod.plan_mod, "build_plan", a_stale_pinned_host)
    monkeypatch.setattr(
        run_mod, "_host_facts",
        lambda ids: ({"codex": False}, {"codex": (pinned,)}),
    )
    subprocess.run(
        ["git", "-C", str(install), "fetch", "-q", "origin"],
        capture_output=True, check=True,
    )

    result = run_mod.apply_commit(
        root=install, commit=second, state_root=tmp_path / "state"
    )

    assert result.outcome == "applied", (
        f"a pinned host's pending hook merge still froze the whole update: "
        f"{result.outcome} — {result.detail}"
    )
    assert run_mod.stage.version_commit(install) == second, (
        "the root never moved"
    )
    assert any("codex" in item for item in result.pending), (
        f"the update applied but never told anyone codex needs approval: "
        f"{result.pending}"
    )


def test_a_root_relative_host_still_freezes_the_apply(tmp_path, monkeypatch):
    """The other side of the same wire: narrowing the rule must not remove it."""
    import subprocess

    from tests.behavior.test_upgrade_engine import _origin_and_install

    origin, install, first, second = _origin_and_install(tmp_path)

    def a_stale_root_relative_host(*, target_commit, **kwargs):
        return run_mod.plan_mod.Plan(
            actions=(
                run_mod.plan_mod.Action(
                    kind="activate_root", client_id=None, subject=None,
                    payload_class=0, detail="activate",
                ),
                run_mod.plan_mod.Action(
                    kind="merge_hooks", client_id="claude-code", subject=None,
                    payload_class=5, detail="managed hook set is stale",
                ),
            ),
            deferred=(),
        )

    monkeypatch.setattr(run_mod.plan_mod, "build_plan", a_stale_root_relative_host)
    existing = tmp_path / "runner.py"
    existing.write_text("# runner\n", encoding="utf-8")
    monkeypatch.setattr(
        run_mod, "_host_facts",
        # Pins that EXIST, so only the root-relative answer can decide this.
        lambda ids: ({"claude-code": True}, {"claude-code": (existing,)}),
    )
    subprocess.run(
        ["git", "-C", str(install), "fetch", "-q", "origin"],
        capture_output=True, check=True,
    )

    result = run_mod.apply_commit(
        root=install, commit=second, state_root=tmp_path / "state"
    )
    assert result.outcome == "pending", result.detail
    assert os.path.realpath(install) != str((tmp_path / "versions" / second).resolve()), (
        "the root moved while a host that FOLLOWS it still needed its hooks merged"
    )


def test_a_deferral_does_not_block_the_NEXT_update(tmp_path, monkeypatch):
    """Reported, never recorded — and this is why.

    `build_plan` refuses to plan while the install state carries pending items
    (`plan.py:302`), because half-applied work makes a roster a lie. A deferral
    is not half-applied work: nothing was left in the middle, a human simply
    has an approval to make. Recording it there would make the next update
    refuse on account of the previous one's approval — trading the stall this
    change removes for a slower one, and the first draft of this change did
    exactly that. Caught by running it against a real install, not by a test,
    which is why there is now a test.
    """
    import json
    import subprocess

    from tests.behavior.test_upgrade_engine import _origin_and_install

    origin, install, first, second = _origin_and_install(tmp_path)

    def a_stale_pinned_host(*, target_commit, **kwargs):
        return run_mod.plan_mod.Plan(
            actions=(
                run_mod.plan_mod.Action(
                    kind="activate_root", client_id=None, subject=None,
                    payload_class=0, detail="activate",
                ),
                run_mod.plan_mod.Action(
                    kind="merge_hooks", client_id="codex", subject=None,
                    payload_class=5, detail="managed hook set is stale",
                ),
            ),
            deferred=(),
        )

    pinned = tmp_path / "pinned-runner.py"
    pinned.write_text("# runner\n", encoding="utf-8")
    monkeypatch.setattr(run_mod.plan_mod, "build_plan", a_stale_pinned_host)
    monkeypatch.setattr(
        run_mod, "_host_facts", lambda ids: ({"codex": False}, {"codex": (pinned,)})
    )
    subprocess.run(
        ["git", "-C", str(install), "fetch", "-q", "origin"],
        capture_output=True, check=True,
    )
    state_root = tmp_path / "state"

    result = run_mod.apply_commit(root=install, commit=second, state_root=state_root)
    assert result.outcome == "applied", result.detail
    assert any("codex" in item for item in result.pending), (
        "the caller was not told about the approval it now has to make"
    )

    recorded = json.loads(
        (state_root / "install-state.json").read_text(encoding="utf-8")
    )
    assert recorded["pending"] == [], (
        f"the deferral was written into the install state, so the NEXT update "
        f"will refuse to plan at all: {recorded['pending']}"
    )


def test_host_reconciliation_records_exactly_what_it_always_did(tmp_path, monkeypatch):
    """The two handoffs must not compose.

    `host_reconciliation` already strips every host-touching action and records
    the report. Letting the deferral path run as well would zero what IT
    records — a change to an existing caller's contract, made by accident,
    while adding a feature for a different one.
    """
    import json
    import subprocess

    from tests.behavior.test_upgrade_engine import _origin_and_install

    origin, install, first, second = _origin_and_install(tmp_path)

    def a_stale_pinned_host(*, target_commit, **kwargs):
        return run_mod.plan_mod.Plan(
            actions=(
                run_mod.plan_mod.Action(
                    kind="activate_root", client_id=None, subject=None,
                    payload_class=0, detail="activate",
                ),
                run_mod.plan_mod.Action(
                    kind="merge_hooks", client_id="codex", subject=None,
                    payload_class=5, detail="managed hook set is stale",
                ),
            ),
            deferred=(),
        )

    pinned = tmp_path / "pinned-runner.py"
    pinned.write_text("# runner\n", encoding="utf-8")
    monkeypatch.setattr(run_mod.plan_mod, "build_plan", a_stale_pinned_host)
    monkeypatch.setattr(
        run_mod, "_host_facts", lambda ids: ({"codex": False}, {"codex": (pinned,)})
    )
    subprocess.run(
        ["git", "-C", str(install), "fetch", "-q", "origin"],
        capture_output=True, check=True,
    )
    state_root = tmp_path / "state"

    result = run_mod.apply_commit(
        root=install, commit=second, state_root=state_root, host_reconciliation=True
    )
    assert result.outcome == "applied", result.detail

    recorded = json.loads(
        (state_root / "install-state.json").read_text(encoding="utf-8")
    )
    assert any("merge_hooks" in item for item in recorded["pending"]), (
        f"the reconciling caller's recorded handoff was emptied by the "
        f"deferral path: {recorded['pending']}"
    )


def test_pruning_refuses_when_a_host_cannot_report_its_pins(tmp_path, monkeypatch):
    """The two enforcement points must fail in the SAME direction.

    The apply gate treats an adapter that cannot answer as "blocks". A pruner
    that treated the same silence as "pins nothing" would delete the tree a
    host had ALREADY been deferred onto — the gate's caution undone later by
    the pruner's carelessness, on a machine that was working, with the failure
    landing on every subsequent tool call.
    """
    from scripts.aqg_update import hosts as hosts_mod
    from scripts.aqg_update import stage as stage_mod

    versions = tmp_path / "versions"
    for name in ("old", "newest"):
        (versions / name / "scripts").mkdir(parents=True)
        (versions / name / "VERSION").write_text("0.0.1\n", encoding="utf-8")

    def _explode(client_id):
        raise RuntimeError("adapter is broken today")

    monkeypatch.setattr(hosts_mod, "adapter_for", _explode)

    with pytest.raises(stage_mod.StageError, match="unused"):
        stage_mod.prune_versions(
            versions_dir=versions, keep=0, protected=(), repo=tmp_path
        )
    assert (versions / "old").exists(), "a tree was deleted despite the refusal"


def test_one_blocking_host_still_stops_a_plan_that_also_has_a_deferrable_one(tmp_path):
    """The mixed case, which is what a real machine mid-migration looks like.

    Both halves of the split are exercised elsewhere in isolation. Neither
    proves that a deferral cannot smuggle the swap past a host that genuinely
    needs to be waited for.
    """
    pinned = tmp_path / "runner.py"
    pinned.write_text("# runner\n", encoding="utf-8")
    actions = (
        plan_mod.Action(kind="activate_root", client_id=None, subject=None,
                        payload_class=1, detail=""),
        plan_mod.Action(kind="merge_hooks", client_id="codex", subject=None,
                        payload_class=5, detail="stale"),
        plan_mod.Action(kind="merge_hooks", client_id="claude-code", subject=None,
                        payload_class=5, detail="stale"),
    )
    blocking, deferrable = run_mod._split_outstanding(
        actions,
        root_relative={"codex": False, "claude-code": True},
        pinned={"codex": (pinned,), "claude-code": (pinned,)},
    )
    assert [a.client_id for a in blocking] == ["claude-code"]
    assert [a.client_id for a in deferrable] == ["codex"]


def test_the_pinned_runner_does_not_follow_the_root_symlink():
    """The invariant the whole deferral rests on, asserted against the source.

    Deferring is only sound because the pinned command executes a tree that
    does not move. If `run_aqg_codex_hook.py` ever derived its root from the
    environment or from the live symlink instead of from its own location, a
    deferred host would run OLD code against NEW policy files — mixed, and
    silently. Three auditors raised this as the change's central unverified
    assumption; this is the assertion that makes it verified.
    """
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "run_aqg_codex_hook.py"
    ).read_text(encoding="utf-8")

    assert "aqg_root = Path(__file__).resolve().parent.parent" in source, (
        "the codex runner no longer derives its root from its own location, so "
        "a deferred host may now execute old code against a new tree"
    )
    assert 'os.environ.get("AQG_ROOT")' not in source, (
        "the codex runner now reads AQG_ROOT from the environment, which "
        "follows the root symlink and therefore moves under a deferred host"
    )


def test_a_symlinked_version_entry_is_never_a_prune_candidate(tmp_path):
    """The premise that lets pin protection use one spelling instead of two.

    Four auditors raised the same symlink hazard: `resolve()` follows links, so
    protecting only the resolved tree could leave the NAME a command uses
    unprotected. It cannot, because `_is_version_tree` refuses a symlink and so
    such an entry is never selected for removal — which makes handling the
    unresolved spelling code that can never execute.

    That is a load-bearing premise borrowed from another function, so it is
    asserted here rather than assumed. If it ever stops holding, the pin
    protection must grow the second spelling back.
    """
    from scripts.aqg_update import stage as stage_mod

    versions = tmp_path / "versions"
    real = versions / "real"
    (real / "scripts").mkdir(parents=True)
    (real / "VERSION").write_text("0.0.1\n", encoding="utf-8")
    alias = versions / "alias"
    alias.symlink_to(real, target_is_directory=True)

    assert not stage_mod._is_version_tree(alias), (
        "a symlinked versions entry is now a prune candidate, so pin "
        "protection must keep the unresolved spelling as well"
    )


def test_the_deferral_survives_the_run_and_doctor_prints_it(tmp_path):
    """"Reported, never recorded" must not mean "said once into the void".

    An unattended updater's return value is read by nobody. The deferral is
    kept out of the install state because `build_plan` refuses to plan over a
    non-empty `pending` — but it still has to be durable somewhere that does
    NOT gate planning. `record` is that place: it writes `CheckResult.pending`
    into the last-check file, `report` is what `doctor` prints, and a quiet
    later check carries the notice forward rather than erasing it.
    """
    state_root = tmp_path / "state"
    state_root.mkdir()
    deferral = (
        "codex: still running the previous version's hooks. Its command is "
        "pinned to the tree it was installed from, so it keeps working; "
        "approve the new hook set in that host to move it forward"
    )

    run_mod.record(
        run_mod.CheckResult(outcome="applied", detail="0.14.4", pending=(deferral,)),
        state_root=state_root,
    )
    printed = run_mod.report(state_root=state_root)
    assert any("needs a human" in line and "codex" in line for line in printed), (
        f"doctor would never mention the approval the update is waiting on: "
        f"{printed}"
    )

    # A quiet check afterwards must not erase it.
    run_mod.record(
        run_mod.CheckResult(outcome="current", detail="up to date"),
        state_root=state_root,
    )
    assert any("codex" in line for line in run_mod.report(state_root=state_root)), (
        "the next quiet check erased the only standing notice of the approval"
    )

    # And approving it must clear it, or the notice becomes permanent noise.
    run_mod.record(
        run_mod.CheckResult(outcome="applied", detail="0.14.5"),
        state_root=state_root,
    )
    assert not any("codex" in line for line in run_mod.report(state_root=state_root)), (
        "the notice outlived the approval that resolved it"
    )


def test_the_split_partitions_every_host_touching_action(tmp_path):
    """`must_stop` replaced `bool(outstanding)`; the two must cover the same set.

    `outstanding` counts every host-touching action. `must_stop` counts
    `blocking` plus the planner's own refusals. That is only a safe narrowing
    if `blocking` and `deferrable` PARTITION the host-touching actions — no
    action silently dropped from both, which would let the swap proceed with
    nobody having decided it was safe.
    """
    pinned = tmp_path / "runner.py"
    pinned.write_text("# runner\n", encoding="utf-8")
    actions = tuple(
        plan_mod.Action(kind=kind, client_id=cid, subject=None,
                        payload_class=5, detail="")
        for kind in ("route_skill", "prune_skill", "merge_hooks", "activate_root")
        for cid in ("codex", "claude-code", "never-heard-of-it")
    )
    blocking, deferrable = run_mod._split_outstanding(
        actions,
        root_relative={"codex": False, "claude-code": True},
        pinned={"codex": (pinned,), "claude-code": ()},
    )
    host_touching = [a for a in actions if a.kind in run_mod.HOST_TOUCHING_KINDS]
    assert len(blocking) + len(deferrable) == len(host_touching), (
        "an action fell out of both halves, so nothing decided whether it was "
        "safe to swap past it"
    )
    assert not (set(map(id, blocking)) & set(map(id, deferrable))), "not disjoint"
    assert all(a.kind != "activate_root" for a in blocking + deferrable)
