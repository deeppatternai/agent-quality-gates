"""Behavior contracts for the manual upgrade path on the version-tree layout.

docs/UPDATE_ARCHITECTURE.md §9 and §13. `upgrade.sh` is the path a human
invokes, and §9 is explicit that manual invocation may follow `main` — the
signature requirement is on the *automatic* channel. So what moving this onto
the update engine buys is not verification, it is **transaction**: an update
that either lands or does not, with the previous version still on disk, instead
of an in-place `git checkout` that can leave a half-updated tree every running
session is reading.

That means this introduces a path which applies an **unsigned** commit. It is
reachable only from an explicit human command, never from the session-start
trigger, and it enforces the same host-consistency rule the automatic path does:
safety there was never about signatures.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.aqg_update import migrate as migrate_mod
from scripts.aqg_update import run as run_mod
from scripts.aqg_update import stage as stage_mod

UPGRADE = Path(__file__).resolve().parents[2] / "scripts" / "upgrade.sh"
FIRST_VERSION = "0.16.0"
SECOND_VERSION = "0.17.0"


def _origin_and_install(tmp_path: Path):
    """A remote with two commits, and an install migrated onto the layout."""
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
    (origin / "skills" / "aqg-demo").mkdir(parents=True)
    (origin / "skills" / "aqg-demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (origin / "VERSION").write_text(FIRST_VERSION + "\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "v1")
    first = git(origin, "rev-parse", "HEAD").stdout.decode().strip()
    (origin / "VERSION").write_text(SECOND_VERSION + "\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "v2")
    second = git(origin, "rev-parse", "HEAD").stdout.decode().strip()

    install = tmp_path / "aqg"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(install)], capture_output=True, check=True
    )
    git(install, "checkout", "-q", "--detach", first)
    migrate_mod.migrate(install)
    return origin, install, first, second


def _clean_plan(monkeypatch):
    """A plan that touches no host configuration; the planner has its own suite."""
    def only_activate(*, target_commit, **kwargs):
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


# --- the transactional manual update ------------------------------------------------


def test_applying_a_commit_swaps_the_root_and_keeps_the_old_version(
    tmp_path, monkeypatch
):
    """What the transaction buys over `git checkout`: the previous version is
    still on disk, so the swap is reversible."""
    origin, install, first, second = _origin_and_install(tmp_path)
    _clean_plan(monkeypatch)
    subprocess.run(
        ["git", "-C", str(install), "fetch", "-q", "origin"], capture_output=True, check=True
    )
    result = run_mod.apply_commit(root=install, commit=second, state_root=tmp_path / "state")
    assert result.outcome == "applied", result.detail
    live = tmp_path / "versions" / SECOND_VERSION
    assert os.path.realpath(install) == str(live.resolve())
    assert stage_mod.version_commit(live) == second
    assert (tmp_path / "versions" / FIRST_VERSION).is_dir(), "the previous version was destroyed"


def test_applying_a_commit_refuses_a_plan_that_touches_a_host(tmp_path, monkeypatch):
    """The manual path is exempt from the SIGNATURE requirement, not from the
    consistency one. Swapping the root while a route waits leaves a host
    describing the old tree and serving the new one, whoever asked for it."""
    origin, install, first, second = _origin_and_install(tmp_path)
    subprocess.run(
        ["git", "-C", str(install), "fetch", "-q", "origin"], capture_output=True, check=True
    )

    def with_a_route(*, target_commit, **kwargs):
        return run_mod.plan_mod.Plan(
            actions=(
                run_mod.plan_mod.Action(
                    kind="route_skill", client_id="claude-code", subject="aqg-demo",
                    payload_class=3, detail="a skill was added upstream",
                ),
            ),
            deferred=(),
        )

    monkeypatch.setattr(run_mod.plan_mod, "build_plan", with_a_route)
    result = run_mod.apply_commit(root=install, commit=second, state_root=tmp_path / "state")
    assert result.outcome == "pending"
    assert os.path.realpath(install) == str((tmp_path / "versions" / FIRST_VERSION).resolve())


def test_the_automatic_path_never_applies_an_unverified_commit():
    """`apply_commit` skips `acquire`, so nothing on the session-start path may
    reach it. This is a design boundary, not a security control — anything that
    can run code can call anything — but the boundary is worth asserting so it
    is not crossed by accident."""
    import inspect

    source = inspect.getsource(run_mod.main) + inspect.getsource(run_mod.check)
    assert "apply_commit" not in source


# --- upgrade.sh on the new layout ------------------------------------------------------


def test_upgrade_recognises_a_migrated_install_as_a_checkout(tmp_path):
    """After the first transactional update the root points at a git WORKTREE,
    whose `.git` is a file. A `-d "$repo_root/.git"` test calls that "not a git
    checkout" and skips every future update, silently and permanently."""
    # Executable lines only: the comment explaining why that test is wrong
    # contains the string, and a test tripped by its own documentation is the
    # second time in this feature that has happened.
    code = "\n".join(
        line for line in UPGRADE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert '-d "$repo_root/.git"' not in code, (
        "a worktree's .git is a file; this test would skip updates forever"
    )
    assert "is-inside-work-tree" in code


def test_upgrade_uses_the_engine_when_the_layout_supports_it():
    body = UPGRADE.read_text(encoding="utf-8")
    assert "apply_commit" in body


def test_upgrade_tells_a_plain_checkout_how_to_get_the_transaction():
    """An install that has not migrated keeps today's behaviour — but silently
    keeping the old path is how nobody ever migrates."""
    body = UPGRADE.read_text(encoding="utf-8")
    assert "--migrate" in body.split("usage()")[1]


def test_a_caller_that_will_reconcile_may_take_a_host_touching_update(
    tmp_path, monkeypatch
):
    """`upgrade.sh` steps 2-4 reinstall skills and refresh hooks — which is
    exactly the work the pending list describes, in the same command.

    So the refusal that is right for the trigger is wrong for a human running
    the whole script: nothing follows the trigger, and the routing DOES follow
    here. The exemption is explicit and the pending list still comes back, so
    the caller can report what it is about to fix.
    """
    origin, install, first, second = _origin_and_install(tmp_path)
    subprocess.run(
        ["git", "-C", str(install), "fetch", "-q", "origin"], capture_output=True, check=True
    )

    def with_a_route(*, target_commit, **kwargs):
        return run_mod.plan_mod.Plan(
            actions=(
                run_mod.plan_mod.Action(
                    kind="activate_root", client_id=None, subject=None,
                    payload_class=0, detail="activate",
                ),
                run_mod.plan_mod.Action(
                    kind="route_skill", client_id="claude-code", subject="aqg-demo",
                    payload_class=3, detail="a skill was added upstream",
                ),
            ),
            deferred=(),
        )

    monkeypatch.setattr(run_mod.plan_mod, "build_plan", with_a_route)
    result = run_mod.apply_commit(
        root=install, commit=second, state_root=tmp_path / "state",
        host_reconciliation=True,
    )
    assert result.outcome == "applied", result.detail
    assert any("aqg-demo" in item for item in result.pending), (
        "the caller was given no list of what it now has to reconcile"
    )
    assert os.path.realpath(install) == str((tmp_path / "versions" / SECOND_VERSION).resolve())


def test_the_exemption_is_not_available_to_the_automatic_path():
    """`check` must never pass it: nothing follows the trigger."""
    import inspect

    assert "host_reconciliation" not in inspect.getsource(run_mod._check_locked)
    assert "host_reconciliation" not in inspect.getsource(run_mod.check)


def test_upgrade_only_claims_reconciliation_when_it_will_actually_do_it():
    """`--no-hooks` and the per-client skips turn off the very steps that
    reconcile. Claiming reconciliation anyway would swap the root and leave the
    hosts stale — the silent-guardrail hazard, arrived at from the other side.
    """
    body = UPGRADE.read_text(encoding="utf-8")
    assert "reconcile" in body
    assert 'hooks_mode' in body.split("target_commit=")[0] or "no_hooks" in body


def test_upgrade_puts_the_root_back_if_reconciliation_fails():
    """The gap the exemption opens, closed.

    The swap happens first and the reconciliation follows, so a failure in
    between leaves the root on the NEW tree with the hosts still describing the
    OLD one — exactly what the automatic path's refusal exists to prevent, and
    `set -e` would abort and leave it that way.

    Rolling the root back is not a perfect state either: some skill routes may
    already point at the new tree. But it is the safe direction, because the old
    tree still contains every hook script the host's config references, and the
    failure mode being avoided is a guardrail whose script vanished.
    """
    body = UPGRADE.read_text(encoding="utf-8")
    assert "trap" in body, "nothing restores the root if a later step fails"
    assert "aqg_rollback_root" in body


def test_the_rollback_trap_covers_every_reconciliation_step():
    """It has to be cleared after step 5, not before it.

    An earlier version cleared it between the Codex and the Claude hook steps —
    and the Claude hook step IS reconciliation, so a failure there would have
    left the root on the new tree with that host still describing the old one.
    """
    body = UPGRADE.read_text(encoding="utf-8")
    clear_at = body.index("trap - EXIT")
    last_reconciliation = body.index("# --- 5.")
    verify_at = body.index("# --- 6.")
    assert last_reconciliation < clear_at < verify_at, (
        "the trap is cleared before the last step that reconciles a host"
    )


def test_reconciliation_is_verified_before_the_rollback_trap_is_cleared():
    """Captured hook failures must still roll the root back."""
    body = UPGRADE.read_text(encoding="utf-8")
    closing = body[body.index("# Every reconciliation step"):body.index("# --- 6.")]

    assert "codex_hooks_rc" in closing and "hooks_rc" in closing
    assert "finalize_reconciliation" in closing
    assert "rollback_reconciliation" in body
    assert "AQG_RECONCILIATION_LOCK_HELD" in body
    assert body.index("RECONCILIATION_LOCK_FILENAME") < body.index("run.apply_commit")
    assert closing.index("codex_hooks_rc") < closing.index("finalize_reconciliation")
    assert closing.index("hooks_rc") < closing.index("finalize_reconciliation")
    assert closing.index("finalize_reconciliation") < closing.index("trap - EXIT")


def test_skipping_one_client_withdraws_the_reconciliation_claim():
    """`--no-codex` skips the step that reconciles Codex.

    The gate was `do_codex OR do_claude`, so skipping one host still claimed
    reconciliation and swapped the root — leaving that host describing the old
    tree, which is the exact state the claim exists to avoid. It has to be AND:
    a run that will not reconcile every host must not claim to reconcile any.
    """
    body = UPGRADE.read_text(encoding="utf-8")
    helper = body[body.index("reconciliation_enabled()") : body.index("# --- 0.")]
    gate = body[body.index('reconcile="0"'):body.index('echo "applying')]
    outer_lock = body[body.index("# Hold one OS lock") : body.index("# Read VERSION")]

    assert 'for enabled in "$do_codex" "$do_claude"' in helper, helper
    assert '[[ "$enabled" == "1" ]] || return 1' in helper, helper
    assert "reconciliation_enabled" in gate, gate
    assert "reconciliation_enabled" in outer_lock, outer_lock
    assert '"$do_codex" == "1" && "$do_claude" == "1"' not in body


def test_applying_the_commit_already_live_reports_current(tmp_path, monkeypatch):
    """Running the upgrade twice must not look like a problem.

    `_apply` staged unconditionally, so the second run hit "a staged tree
    already exists" and reported `pending` — turning the most ordinary thing a
    user does into a warning about work that needs a human.
    """
    origin, install, first, second = _origin_and_install(tmp_path)
    result = run_mod.apply_commit(root=install, commit=first, state_root=tmp_path / "s")
    assert result.outcome == "current", result.detail


def test_a_refused_update_does_not_abort_the_rest_of_the_upgrade():
    """`--no-hooks` with a host-touching plan used to `exit 1`, so the skills
    steps that used to run stopped running. Not updating the tree is a reason to
    say so, not a reason to abandon the rest of the command."""
    body = UPGRADE.read_text(encoding="utf-8")
    # Only the REFUSED branch. A genuine transaction failure still aborts, and
    # should: the first version of this test forbade `exit 1` anywhere in the
    # block and so demanded that too.
    refused = body[body.index('if [[ "$rc" == "2" ]]'):body.index('elif [[ "$rc" == "3" ]]')]
    assert "exit" not in refused, refused


def test_the_failure_message_does_not_promise_a_rollback_that_may_not_have_happened():
    """`repair-required` means precisely that the previous state could NOT be
    restored, so printing "the previous version is still live" for it is false
    at the moment it matters most."""
    body = UPGRADE.read_text(encoding="utf-8")
    assert body.count("the previous version is still live") <= 1
    assert "repair-required" in body


def test_a_refusal_does_not_leave_a_staged_tree_blocking_the_next_attempt(
    tmp_path, monkeypatch
):
    """Found by running it, not by a test.

    The refusal path stages the tree and then declines to apply it, so
    `versions/<version>` was left behind, so every later attempt at that same
    version hit "a staged
    version already exists" and reported pending forever. One `--no-hooks` run
    permanently wedged the upgrade.
    """
    origin, install, first, second = _origin_and_install(tmp_path)
    subprocess.run(
        ["git", "-C", str(install), "fetch", "-q", "origin"], capture_output=True, check=True
    )

    def with_a_route(*, target_commit, **kwargs):
        return run_mod.plan_mod.Plan(
            actions=(
                run_mod.plan_mod.Action(
                    kind="route_skill", client_id="claude-code", subject="aqg-demo",
                    payload_class=3, detail="added upstream",
                ),
            ),
            deferred=(),
        )

    monkeypatch.setattr(run_mod.plan_mod, "build_plan", with_a_route)
    first_try = run_mod.apply_commit(root=install, commit=second, state_root=tmp_path / "s")
    assert first_try.outcome == "pending"
    assert not (tmp_path / "versions" / SECOND_VERSION).exists(), (
        "the refusal left a staged tree behind"
    )

    # And the proof it mattered: the same version can be attempted again.
    monkeypatch.setattr(run_mod.plan_mod, "build_plan", with_a_route)
    again = run_mod.apply_commit(root=install, commit=second, state_root=tmp_path / "s")
    assert "already exists" not in again.detail, again.detail


def test_the_record_says_when_a_version_was_applied_without_verification(
    tmp_path, monkeypatch
):
    """Nothing else distinguishes a signature-verified version from one a human
    applied by hand, and `doctor` is the only place anyone would look."""
    origin, install, first, second = _origin_and_install(tmp_path)
    _clean_plan(monkeypatch)
    subprocess.run(
        ["git", "-C", str(install), "fetch", "-q", "origin"], capture_output=True, check=True
    )
    result = run_mod.apply_commit(root=install, commit=second, state_root=tmp_path / "s")
    assert result.outcome == "applied"
    assert "unsigned" in result.detail, result.detail


def test_main_cannot_be_made_to_apply_an_unverified_commit(tmp_path, monkeypatch):
    """Behavioural, not a string search.

    The earlier test asserted that the literal 'apply_commit' does not appear in
    two functions' source, which an indirection would defeat. This makes the
    function itself explode and runs the automatic entry point: if anything on
    that path reached it, the record would say so.
    """
    def explode(*args, **kwargs):
        raise AssertionError("the automatic path reached apply_commit")

    monkeypatch.setattr(run_mod, "apply_commit", explode)
    monkeypatch.setenv("AQG_STATE_ROOT", str(tmp_path))
    assert run_mod.main([]) == 0
    last = tmp_path / run_mod.LAST_CHECK_FILENAME
    if last.exists():
        import json
        assert "reached apply_commit" not in json.loads(last.read_text())["detail"]
