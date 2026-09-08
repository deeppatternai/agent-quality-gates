"""Hook evidence must describe the tree the update is going TO.

The roster fix (#674) took `route_skill` out of every plan, and the update still
never applied. `merge_hooks` was left, and its condition is:

    stale_for_target = install_moved or _recorded_version(...) != target_version
    if status in _HOOKS_NEED_WORK or stale_for_target: -> merge_hooks

`install_moved` is true for **every** real update — that is what an update is —
so `merge_hooks` was planned every time, it is in `run.HOST_TOUCHING_KINDS`, and
`_apply` applies nothing when a plan contains one. Measured on a machine with a
complete install state, `hooks_status="complete"` and every skill routed:
`{'merge_hooks': 1, 'activate_root': 1, 'record_state': 1}` — pending.

The rule was not paranoid, it was answering the wrong question. `hooks_status`
was computed against the tree that is live NOW, so "complete" said nothing about
whether the host's settings would still be complete once the root swaps. Rather
than assume it will not be, ask about the tree the swap is going to — it is
staged and on disk before evidence is collected.

So `Evidence` now records WHICH tree its hook status describes, and the planner
requires that to be the target before trusting it. An adapter that cannot answer
about the target leaves the field unset and the conservative merge returns —
fail-closed, not fail-quiet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.aqg_update import plan as plan_mod
from scripts.aqg_update.hosts.base import Evidence


def _tree(root: Path, *, version: str, skills=("aqg-a", "aqg-b")) -> Path:
    (root / "skills").mkdir(parents=True)
    for name in skills:
        (root / "skills" / name).mkdir()
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


def _state(version: str, commit: str, skills=("aqg-a", "aqg-b")) -> dict:
    return {
        "schema": 1, "channel": "stable", "installed_version": version,
        "installed_commit": commit, "release_sequence": 4,
        "installed_at": "2026-09-07T00:00:00Z", "applied_by": "stable-2026-09",
        "hosts": {"claude-code": {"last_applied_version": version,
                                  "routed_skills": list(skills)}},
        "pending": [],
    }


def test_a_host_already_correct_for_the_target_is_not_re_merged(tmp_path):
    """The whole stall, after the roster half was fixed.

    Nothing about this machine needs doing: its state is recorded, its hooks are
    complete *for the tree being installed*, and every shipped skill is routed.
    A plan for it must contain no host-touching action, or `_apply` applies
    nothing and the update stalls exactly as it did before #674.
    """
    target = _tree(tmp_path / "v", version="0.14.4")
    evidence = {
        "claude-code": Evidence(
            client_id="claude-code",
            hooks_status="complete",
            hooks_detail="15 managed scripts across 16 hook definitions",
            recorded_version="0.14.3",
            routed_skills=("aqg-a", "aqg-b"),
            hooks_checked_against=str(target),
        )
    }
    built = plan_mod.build_plan(
        state=_state("0.14.3", "a" * 40), target=target,
        evidence=evidence, target_commit="b" * 40,
    )
    touching = [a for a in built.actions
                if a.kind in {"route_skill", "prune_skill", "merge_hooks"}]
    assert not touching, (
        "a host that is already correct for the target was planned host-touching "
        f"work, so the update applies nothing: {[(a.kind, a.detail) for a in touching]}"
    )


def test_evidence_about_the_wrong_tree_still_merges(tmp_path):
    """Fail-closed, and the reason the field exists rather than a bare flag.

    An adapter that inspected the LIVE tree cannot say whether the target's hook
    set differs. Trusting `complete` from it would skip a merge that a changed
    hook set genuinely needs — and a hook whose script vanished from under the
    root fails into `|| true`, so the guardrail stops running and says nothing.
    """
    target = _tree(tmp_path / "v", version="0.14.4")
    evidence = {
        "claude-code": Evidence(
            client_id="claude-code", hooks_status="complete", hooks_detail="",
            recorded_version="0.14.3", routed_skills=("aqg-a", "aqg-b"),
            hooks_checked_against=str(tmp_path / "some-other-tree"),
        )
    }
    built = plan_mod.build_plan(
        state=_state("0.14.3", "a" * 40), target=target,
        evidence=evidence, target_commit="b" * 40,
    )
    assert [a for a in built.actions if a.kind == "merge_hooks"], (
        "hook evidence gathered against a different tree was trusted"
    )


def test_evidence_that_names_no_tree_still_merges(tmp_path):
    """The default. An adapter that does not implement the target-relative
    inspection leaves the field unset and gets the old, conservative answer."""
    target = _tree(tmp_path / "v", version="0.14.4")
    evidence = {
        "claude-code": Evidence(
            client_id="claude-code", hooks_status="complete", hooks_detail="",
            recorded_version="0.14.3", routed_skills=("aqg-a", "aqg-b"),
        )
    }
    built = plan_mod.build_plan(
        state=_state("0.14.3", "a" * 40), target=target,
        evidence=evidence, target_commit="b" * 40,
    )
    assert [a for a in built.actions if a.kind == "merge_hooks"], (
        "evidence that names no tree was treated as target-relative"
    )


@pytest.mark.parametrize("status", ["missing", "stale"])
def test_a_target_whose_hooks_really_differ_is_still_merged(tmp_path, status):
    """Target-relative evidence is not a way to stop merging — it is a way to
    stop merging when nothing changed. A target whose hook set genuinely differs
    reports `stale` against it, and that still plans the merge."""
    target = _tree(tmp_path / "v", version="0.14.4")
    evidence = {
        "claude-code": Evidence(
            client_id="claude-code", hooks_status=status, hooks_detail="",
            recorded_version="0.14.3", routed_skills=("aqg-a", "aqg-b"),
            hooks_checked_against=str(target),
        )
    }
    built = plan_mod.build_plan(
        state=_state("0.14.3", "a" * 40), target=target,
        evidence=evidence, target_commit="b" * 40,
    )
    assert [a for a in built.actions if a.kind == "merge_hooks"], (
        f"a host reporting {status!r} against the target was not merged"
    )


# --- the wiring, not just the rule -------------------------------------------

def test_verify_inspects_against_the_target_it_was_given(tmp_path):
    """The planner's rule is worth nothing if the root never reaches `_inspect`.

    Asserted on the argument the seam actually receives, because the first
    version of this suite tested only the rule — building `Evidence` by hand —
    and a mutation that made `verify` pass `None` left every test green.
    """
    from scripts.aqg_update.hosts.base import Evidence, HostAdapter

    seen = []

    class _Probe(HostAdapter):
        client_id = "probe"
        hook_command_is_root_relative = True

        def _inspect(self, root=None):
            seen.append(root)
            return "complete", "probe"

    target = tmp_path / "staged"
    target.mkdir()
    evidence = _Probe().verify(state=None, target_root=target)

    assert seen == [target], f"the seam was called with {seen}, not the target"
    assert evidence.hooks_checked_against == str(target.resolve()), (
        "evidence did not record the tree it was gathered against, so the "
        "planner cannot tell target-relative evidence from any other kind"
    )


def test_verify_records_no_tree_when_it_was_given_none():
    """The default path, and what every caller outside an update takes."""
    from scripts.aqg_update.hosts.base import HostAdapter

    class _Probe(HostAdapter):
        client_id = "probe"

        def _inspect(self, root=None):
            return "complete", "probe"

    assert _Probe().verify(state=None).hooks_checked_against is None


def test_an_adapter_that_does_not_declare_root_relative_hooks_is_not_stamped(tmp_path):
    """Fail closed: provenance is a declaration, never a caller assumption.

    `verify` stamps the root it PASSED; it cannot see which root the adapter
    USED. `generic._inspect` takes `root` and ignores it, and so may a future
    or third-party adapter. Without the opt-in, such an adapter returning
    `complete` would have the target stamped on an answer that was never
    checked there, the planner would skip the merge, and a hook whose script
    vanished from under the root fails into `|| true` — the guardrail stops
    running and says nothing.
    """
    from scripts.aqg_update.hosts.base import HostAdapter

    seen = []

    class _Ignores(HostAdapter):
        client_id = "ignores"
        # hook_command_is_root_relative left at its default

        def _inspect(self, root=None):
            seen.append(root)
            return "complete", "ignored the root"

    target = tmp_path / "staged"
    target.mkdir()
    evidence = _Ignores().verify(state=None, target_root=target)

    assert seen == [None], "an undeclared adapter was handed the target anyway"
    assert evidence.hooks_checked_against is None, (
        "an adapter that never promised to honour the root was recorded as "
        "having checked the target"
    )


def test_codex_does_not_claim_target_relative_hooks():
    """Its installed command embeds an absolute versioned path and a digest.

    Pinned so the day someone splits the codex root they must come here and
    say so, rather than the planner silently starting to trust an answer that
    is ill-posed for this host.
    """
    from scripts.aqg_update.hosts.codex import CodexAdapter
    from scripts.aqg_update.hosts.claude_code import ClaudeCodeAdapter

    assert CodexAdapter.hook_command_is_root_relative is False
    assert ClaudeCodeAdapter.hook_command_is_root_relative is True


def test_two_spellings_of_one_tree_are_one_tree(tmp_path):
    """The producer resolves; the consumer must resolve too, or it is text.

    `_evidence` records `str(migrate._canonical(root))` — the RESOLVED
    spelling. If the planner compares that to the literal `target` it was
    handed, then `/tmp/x` against `/private/tmp/x`, a symlinked `~`, or a
    trailing `..` makes one directory compare as two, `about_the_target`
    collapses, and the conservative `merge_hooks` returns — the exact stall
    this rule exists to end, wearing a reason string that blames the version.
    """
    from scripts.aqg_update.hosts.base import Evidence
    from scripts.aqg_update import plan as plan_mod

    real = tmp_path / "real"
    (real / "skills").mkdir(parents=True)
    (real / "VERSION").write_text("9.9.9\n")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    def merges(target, recorded):
        evidence = {"claude-code": Evidence(
            client_id="claude-code", hooks_status="complete", hooks_detail="",
            recorded_version="0.0.1", routed_skills=(),
            hooks_checked_against=recorded)}
        built = plan_mod.build_plan(
            state=_state("9.9.9", "a" * 40, skills=()), target=target,
            evidence=evidence,
            target_commit="b" * 40)
        return [a for a in built.actions if a.kind == "merge_hooks"]

    assert not merges(alias, str(real)), (
        "the same tree named two ways was treated as two trees"
    )
    assert not merges(real / "skills" / "..", str(real)), (
        "a non-normalised spelling of the target was treated as another tree"
    )
    assert merges(alias, str(tmp_path / "elsewhere")), (
        "evidence about a genuinely different tree stopped forcing the merge"
    )
    assert merges(alias, "has a \x00 in it"), (
        "an unreadable recorded root must not be trusted, and must not raise"
    )


def test_apply_asks_for_evidence_about_the_staged_target(monkeypatch, tmp_path):
    """The one line that turns this whole change on in production.

    Everything else here tests a rule or a seam below it. `_apply` passing
    `target_root=` is the wire; delete it and every host reverts to
    live-relative evidence, the version fallback fires for all of them, and
    the fleet-wide stall returns — with the entire suite green. That is the
    shape of hole this workstream has now shipped three times, so it is
    pinned at the layer above, not one below.
    """
    from scripts.aqg_update import run as run_mod

    seen = {}

    def _spy(installed, dropped=None, target_root=None):
        seen["target_root"] = target_root
        raise _Stop()

    class _Stop(Exception):
        pass

    from scripts.aqg_update import stage as stage_mod

    monkeypatch.setattr(run_mod, "_collect_evidence", _spy)

    # The managed layout `_apply` requires: the root is a SYMLINK into
    # `versions/<sha>`, which is how it derives where to stage.
    versions = tmp_path / "versions"
    live = versions / ("a" * 40)
    live.mkdir(parents=True)
    root = tmp_path / "agent-quality-gates"
    root.symlink_to(live, target_is_directory=True)

    staged = versions / ("c" * 40)
    staged.mkdir()
    monkeypatch.setattr(
        stage_mod, "stage_version",
        lambda **kwargs: staged,
    )

    with pytest.raises(_Stop):
        run_mod._apply(
            root=root, commit="c" * 40, version="9.9.9",
            installed=_state("9.9.9", "a" * 40), state_root=tmp_path,
        )

    assert seen["target_root"] == staged, (
        f"_apply asked for evidence about {seen['target_root']}, not the "
        f"staged target {staged}: every host's hook status would then "
        f"describe the tree being replaced, the version fallback fires for "
        f"all of them, and the fleet-wide stall returns"
    )


def test_a_real_adapter_answers_the_planner_without_forcing_a_merge(tmp_path):
    """End to end across the producer/consumer seam, with no hand-built Evidence.

    Every other planner test here constructs `Evidence` itself, which means
    they agree with each other about the spelling of `hooks_checked_against`
    rather than with `_evidence`. That is precisely how a one-sided
    canonicalisation survived a green suite. This one takes what a REAL
    adapter's `verify` produces and hands it to the real `build_plan`.
    """
    import shutil
    from scripts.aqg_update import plan as plan_mod
    from scripts.aqg_update.hosts.claude_code import ClaudeCodeAdapter

    repo = Path(__file__).resolve().parents[2]
    target = tmp_path / "staged"
    target.mkdir()
    (target / "VERSION").write_text("9.9.9\n")
    shutil.copytree(repo / "agent-packs" / "claude-code" / "hooks",
                    target / "agent-packs" / "claude-code" / "hooks")
    (target / "scripts").mkdir(exist_ok=True)

    settings = tmp_path / "settings.json"
    adapter = ClaudeCodeAdapter(settings_path=settings, aqg_root=target)
    evidence = {"claude-code": adapter.verify(state=None, target_root=target)}

    # Whatever the status is, the provenance must name the staged target —
    # not the live tree, and not a spelling the planner will fail to match.
    recorded = evidence["claude-code"].hooks_checked_against
    assert recorded is not None, "a declared adapter recorded no tree"
    assert plan_mod._same_tree(recorded, target), (
        f"the adapter recorded {recorded!r}, which the planner does not "
        f"recognise as the target {target}"
    )


def test_an_unreadable_target_degrades_instead_of_dropping_the_host(tmp_path):
    """A staged tree that cannot answer must not become an adapter FAILURE.

    A raised error lands in `_collect_evidence`'s `dropped`, which is an
    outstanding item, which holds the apply for every host on the machine —
    a worse stall than the one this whole change exists to remove, reached by
    a stricter check. `require_aqg_root` raises `AdapterError` today, but the
    contract is about unusable targets, not about one exception type: catching
    only that type enforces the rule for one shape of failure and lets every
    other shape take the machine down.
    """
    from scripts.aqg_update.hosts.base import HostAdapter

    class _TargetExplodes(HostAdapter):
        client_id = "explodes"
        hook_command_is_root_relative = True

        def _inspect(self, root=None):
            if root is not None:
                raise RuntimeError("staged payload is truncated")
            return "complete", "live tree is fine"

    target = tmp_path / "staged"
    target.mkdir()
    evidence = _TargetExplodes().verify(state=None, target_root=target)

    assert evidence.hooks_status == "complete"
    assert evidence.hooks_checked_against is None, (
        "the live-relative answer was recorded as if it described the target"
    )


def test_an_unreadable_target_leaves_a_trace(tmp_path):
    """Do not launder a corrupt signed payload into an ordinary re-merge.

    The fallback discards the exception. If it also discards the fact that a
    fallback happened, the planner — and `doctor`, the only place anyone
    looks — cannot tell "this adapter never implemented target inspection"
    from "the signature-verified staged tree is missing its hook sources".
    Both present as `hooks_checked_against is None`. On a channel whose
    defining failure mode is a stall nobody could see for three rounds,
    suppressing that signal is the expensive kind of silence.
    """
    from scripts.aqg_update.hosts.base import HostAdapter

    class _TargetExplodes(HostAdapter):
        client_id = "explodes"
        hook_command_is_root_relative = True

        def _inspect(self, root=None):
            if root is not None:
                raise RuntimeError("staged payload is truncated")
            return "complete", "live tree is fine"

    target = tmp_path / "staged"
    target.mkdir()
    detail = _TargetExplodes().verify(state=None, target_root=target).hooks_detail

    assert "unreadable" in detail, (
        f"nothing in {detail!r} says the staged target could not be read"
    )
    assert "live tree is fine" in detail, "the live answer itself was lost"
    # And WHICH failure. A bare `except Exception` that records only
    # "unreadable" makes a programming error in `_inspect` — a parser bug, a
    # typo in a new adapter — indistinguishable from a genuinely corrupt
    # payload. Both then present as an ordinary conservative re-merge.
    assert "RuntimeError" in detail, (
        f"the exception type was discarded: {detail!r}"
    )
    assert "truncated" in detail, (
        f"the exception message was discarded: {detail!r}"
    )


def test_a_live_tree_that_cannot_answer_either_is_still_a_failure(tmp_path):
    """Only the TARGET is retried away.

    Widening the fallback must not turn a genuinely broken host into a
    silently 'fine' one: if the live tree cannot answer, the second call
    raises, the host is dropped, and that is both correct and the behaviour
    that existed before this parameter.
    """
    import pytest as _pytest
    from scripts.aqg_update.hosts.base import HostAdapter

    class _BothExplode(HostAdapter):
        client_id = "both"
        hook_command_is_root_relative = True

        def _inspect(self, root=None):
            raise RuntimeError("nothing here can be read")

    target = tmp_path / "staged"
    target.mkdir()
    with _pytest.raises(RuntimeError):
        _BothExplode().verify(state=None, target_root=target)


def test_the_root_relative_declaration_is_true_of_the_command_we_actually_write():
    """The whole rule is load-bearing on one boolean that nothing verifies.

    `hook_command_is_root_relative = True` is a CLAIM about how a host's hook
    command is spelled, and `verify` cannot observe it. If claude-code's
    command ever stopped naming `$AQG_ROOT` — someone bakes in a resolved path
    "for speed", the way codex already does — the attribute would keep saying
    True, the planner would keep trusting target-relative evidence, and the
    merge that a version-pinned command genuinely needs would be skipped. A
    hook pointing into a swapped-away root fails into `|| true`: the guardrail
    stops running and says nothing.

    So check the claim against the thing it is a claim ABOUT: the command text
    the installer actually writes. This cannot prove the attribute is right in
    general, but it makes the specific lie that matters falsifiable.
    """
    from scripts.aqg_update.hosts.claude_code import ClaudeCodeAdapter
    from scripts.aqg_update.hosts.codex import CodexAdapter
    from scripts import install_aqg_hooks

    repo = Path(__file__).resolve().parents[2]
    specs = install_aqg_hooks._aqg_hook_specs(repo)
    commands = _commands_in(specs)
    assert len(commands) >= 10, (
        f"only {len(commands)} hook commands found; the extractor stopped "
        f"seeing them, so this test would pass vacuously"
    )

    for command in commands:
        assert "$AQG_ROOT" in command, (
            f"ClaudeCodeAdapter declares hook_command_is_root_relative="
            f"{ClaudeCodeAdapter.hook_command_is_root_relative}, but the "
            f"command the installer writes does not name $AQG_ROOT: {command!r}"
        )
        assert str(repo) not in command, (
            f"the command embeds a resolved checkout path, which is exactly "
            f"what makes {CodexAdapter.client_id}'s declaration False: {command!r}"
        )


def _commands_in(spec):
    """Every `"command"` string in whatever shape `_aqg_hook_specs` returns.

    Keyed on the field name rather than "any string", because matchers and
    event names are strings too and none of them names a root — collecting
    those made the check fail on `"PreToolUse"` and say nothing true.
    """
    if isinstance(spec, dict):
        out = []
        for key, value in spec.items():
            if key == "command" and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_commands_in(value))
        return out
    if isinstance(spec, (list, tuple)):
        return [c for value in spec for c in _commands_in(value)]
    return []
