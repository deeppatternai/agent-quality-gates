"""An update must not stall because nobody wrote down what the installer routed.

Audit `aud_q2r8-0rjhFHUDm-j`. A freshly installed machine could never leave
`pending`, so automatic updates worked for nobody:

1. `plan` diffed the shipped roster against `state["hosts"][id]["routed_skills"]`.
2. No installer writes that key — only `run._record_installed`, inside `_apply`.
3. Empty record minus a full roster = one `route_skill` per skill.
4. `route_skill` is in `run.HOST_TOUCHING_KINDS`, so `_apply` applies **nothing**
   and returns `pending`.
5. Nothing applied means nothing recorded, so the next check repeats step 3.

Reading reality is only half of it. Ownership of a route is an exact link-text
comparison, and the installer wrote links through the *resolved* root — so the
spelling carried a release and stopped matching at the next one.

**Two spellings, two jobs, and conflating them is the root cause.** Executing
out of the checkout wants the PHYSICAL path so one skill invocation cannot tear
across a swap; `scripts/_aqg_context.sh` resolves for exactly that reason and is
right to. Owning a route wants the LOGICAL path. Both were being read out of one
`AQG_ROOT`, which is why the first version of this fix — an adapter helper that
preferred `$AQG_ROOT` — was defeated on its own main path: by the time an update
check runs from a skill, that variable holds the physical path, and the
`versions/<sha>` directory contains a `VERSION` file too, so no validation
caught it. `migrate.logical_root` derives the logical spelling from the layout
instead, and reads no environment at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.aqg_update import migrate, plan as plan_mod, skills_route
from scripts.aqg_update.hosts import base as base_mod
from scripts.aqg_update.hosts.base import AdapterError, Evidence
from scripts.aqg_update.hosts.claude_code import ClaudeCodeAdapter

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def managed(tmp_path, monkeypatch):
    """A real managed layout: `<home>/.deeppattern/agent-quality-gates` is a
    symlink into `versions/<sha>/`, holding a real checkout.

    Built by copying the checkout rather than by hand, because the previous
    round of this work asserted against a fixture shaped like nothing any
    installer produces, and the audit found the production path untested.
    """
    home = tmp_path / "home"
    versions = home / ".deeppattern" / "versions"
    versions.mkdir(parents=True)
    tree = versions / ("e" * 40)
    shutil.copytree(
        REPO, tree,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".aqg", "node_modules"),
        symlinks=True,
    )
    root = home / ".deeppattern" / "agent-quality-gates"
    root.symlink_to(tree)
    (home / ".claude").mkdir()
    # `Path.home()` is load-bearing now: the ownership root is the constant
    # `~/.deeppattern/agent-quality-gates` rather than anything derived from a
    # caller's spelling, which is what finally made the two sides agree. A
    # fixture that leaves it pointing at the real home is not modelling an
    # install.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return {"home": home, "root": root, "tree": tree, "versions": versions,
            "skills": home / ".claude" / "skills"}


def _adapter(managed) -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(
        aqg_root=managed["root"],
        skills_dest=managed["skills"],
        settings_path=managed["home"] / ".claude" / "settings.json",
    )


def _install(managed) -> None:
    subprocess.run(
        ["bash", str(managed["root"] / "agent-packs/claude-code/install.sh"),
         "--scope", "user", "--mode", "link", "--force", "--no-hooks"],
        cwd=str(managed["root"]), check=True, capture_output=True,
        env={**os.environ, "HOME": str(managed["home"]),
             "AQG_ROOT": str(managed["root"])},
    )


# --- R1: the installer writes a spelling that survives a release --------------

def test_a_mismatched_home_stalls_rather_than_claiming_someone_elses_links(tmp_path, monkeypatch):
    """An install made under one `HOME` and checked under another.

    The `HOME` anchor is pre-existing and systemic — the state root, every host
    adapter's defaults, and the published installer all use it — and round 7's
    attempt to route around it adopted any similarly-named symlink in any
    ancestor as an ownership root, which is what decides what prune deletes.
    Keeping the anchor is the call; what has to be true is that a mismatch fails
    in the safe direction.

    An earlier draft of the docstring claimed ownership was simply never reached
    under a divergent `HOME`. A reviewer showed that is false when the second
    `HOME` has a skills directory of its own, and the measurement is here rather
    than the claim.
    """
    installed = tmp_path / "installed"
    tree = installed / ".deeppattern" / "versions" / ("a" * 40)
    (tree / "agent-packs" / "claude-code" / "skills" / "aqg-x").mkdir(parents=True)
    (installed / ".deeppattern" / "agent-quality-gates").symlink_to(tree)

    other = tmp_path / "other"
    dest = other / ".claude" / "skills"
    dest.mkdir(parents=True)
    theirs = dest / "someone-elses"
    theirs.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: other))

    observed = base_mod.observed_routes(
        aqg_root=tree,
        skills_subdir="agent-packs/claude-code/skills",
        dest_root=dest,
    )
    assert observed == (), (
        "a mismatched HOME claimed routes in a directory it does not own; the "
        "safe answer is 'none of these are mine', which stalls at pending"
    )
    assert theirs.is_dir(), "a directory belonging to that host was disturbed"


def test_the_installer_writes_the_logical_spelling(managed):
    """Through the real installer, not by reading it for `.resolve()`.

    The link text is the artifact — `skills_route` compares it byte for byte —
    so nothing short of the string on disk settles this.
    """
    _install(managed)
    link = managed["skills"] / "aqg-code-construction"
    assert os.readlink(link) == str(
        managed["root"] / "agent-packs/claude-code/skills/aqg-code-construction"
    ), (
        f"the link names a version directory, so it stops being recognised as "
        f"ours at the next release: {os.readlink(link)}"
    )


def test_routes_are_still_ours_after_a_version_swap(managed):
    """The property the whole change exists for, exercised end to end: real
    installer, real adapter, real symlink swap."""
    _install(managed)
    before = _adapter(managed)._observed_routes()
    assert before and len(before) >= 16, f"nothing routed: {before}"

    new_tree = managed["versions"] / ("f" * 40)
    shutil.copytree(managed["tree"], new_tree, symlinks=True)
    # One atomic `os.replace`, which is what `stage.swap_root` does. Modelling
    # it as unlink-then-symlink would invent a window in which the root does not
    # exist — and every hook resolves `$AQG_ROOT` on every tool call, so that
    # window is exactly the thing the symlink layout exists to avoid.
    staged = managed["root"].with_name("root.tmp")
    staged.symlink_to(new_tree)
    os.replace(staged, managed["root"])

    assert _adapter(managed)._observed_routes() == before, (
        "the routes stopped being recognised after a swap, so every update "
        "would re-plan every skill and stall at pending"
    )
    assert (managed["skills"] / "aqg-code-construction").resolve().is_relative_to(
        new_tree
    ), "the route did not follow the swap; the host still serves the old tree"


def test_a_pre_existing_version_pinned_route_is_converted_by_reinstalling(managed):
    """The migration every existing machine needs, measured rather than assumed.

    The audit was right to press on this: the previous round asserted 'one
    reinstall is enough' without ever running one, and the first attempt did
    NOT convert — `aqg_skill_install` resolved the source independently of how
    the installer spelled it.
    """
    old = managed["skills"]
    old.mkdir(parents=True)
    (old / "aqg-code-construction").symlink_to(
        managed["tree"] / "agent-packs/claude-code/skills/aqg-code-construction"
    )
    assert _adapter(managed)._observed_routes() == (), "fixture is not the old shape"

    _install(managed)

    observed = _adapter(managed)._observed_routes()
    assert observed and "aqg-code-construction" in observed, (
        "reinstalling did not adopt the version-pinned route, so an existing "
        "machine stays stalled no matter how often the user reinstalls"
    )


# --- R2: the spelling is derived from the layout, never from the environment --

@pytest.mark.parametrize("spelling", ["logical", "physical"])
def test_the_environment_does_not_decide_ownership(managed, monkeypatch, spelling):
    """`AQG_ROOT` holds the PHYSICAL path whenever an update check is started
    from a skill, because `_aqg_context.sh` pins it that way on purpose. An
    ownership rule that reads it is therefore wrong exactly when it matters.

    The adapter is built WITHOUT an explicit root, so the environment is the
    only thing that can supply one — an earlier version passed `aqg_root=`
    here, which meant the variable it was parametrised over was never read and
    the test could not have failed.
    """
    _install(managed)
    monkeypatch.setenv(
        "AQG_ROOT",
        str(managed["root"] if spelling == "logical" else managed["tree"]),
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: managed["home"]))
    adapter = ClaudeCodeAdapter(          # no aqg_root: it must come from $AQG_ROOT
        skills_dest=managed["skills"],
        settings_path=managed["home"] / ".claude" / "settings.json",
    )
    assert adapter._aqg_root is not None, "the adapter did not read $AQG_ROOT at all"
    assert Path(adapter._aqg_root).resolve() == Path(
        os.environ["AQG_ROOT"]
    ).resolve(), (
        f"the adapter's root did not come from $AQG_ROOT ({adapter._aqg_root}), "
        f"so this test would pass even if the environment were ignored"
    )
    assert len(adapter._observed_routes() or ()) >= 16, (
        f"observation changed with $AQG_ROOT set to the {spelling} spelling"
    )


def test_a_tilde_root_produces_the_same_link_text(managed, monkeypatch):
    """`Path.resolve()` does not expand `~` — it turns `~/x` into `<cwd>/~/x`.

    A caller passing an unexpanded tilde therefore missed the root entirely and
    the writer fell back to the physical spelling, silently restoring the stall
    the change exists to remove. Caught by an auditor, not by me: the mutation
    that removes the expansion left every other test green.
    """
    from scripts.aqg_skill_install import _link_text_for

    monkeypatch.setenv("HOME", str(managed["home"]))
    sub = "agent-packs/claude-code/skills/aqg-code-construction"
    real = (managed["root"] / sub).resolve()

    plain = _link_text_for(managed["root"] / sub, real, managed["root"])
    tilde = _link_text_for(
        Path("~/.deeppattern/agent-quality-gates") / sub, real,
        Path("~/.deeppattern/agent-quality-gates"),
    )
    assert tilde == plain, (
        f"a tilde-spelled root produced a different link text: {tilde} vs {plain}"
    )
    assert "versions" not in str(tilde), (
        "the tilde path fell back to the physical spelling, which is the stall"
    )


@pytest.mark.parametrize("ancestor_is_a_symlink", [False, True])
def test_both_sides_agree_however_the_ancestor_was_spelled(tmp_path, monkeypatch, ancestor_is_a_symlink):
    """The contract in one assertion: the installer is handed the root as a
    human spelled it, the update engine resolves its own, and both must produce
    the same ownership string.

    The symlinked-ancestor case is why this is parametrised. A `$HOME` that
    points elsewhere, or `/var` -> `/private/var` on macOS, made the two sides
    diverge above `.deeppattern` — and every route looked unowned again. The
    fixture used everywhere else in this file has no symlinked ancestor, so
    fourteen tests and eight mutations were green while this was broken. Found
    by an auditor asking what the fixture could not express.
    """
    base = tmp_path / "real"
    tree = base / ".deeppattern" / "versions" / ("a" * 40)
    tree.mkdir(parents=True)
    (base / ".deeppattern" / "agent-quality-gates").symlink_to(tree)
    if ancestor_is_a_symlink:
        (tmp_path / "home").symlink_to(base)
        home = tmp_path / "home"
        as_spelled = home / ".deeppattern" / "versions" / ("a" * 40)
    else:
        home = base
        as_spelled = tree
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    assert migrate.logical_root(as_spelled) == migrate.logical_root(tree.resolve()), (
        "the writer's spelling and the reader's resolved one produced different "
        "ownership roots, so every route would look unowned"
    )
    assert migrate.logical_root(as_spelled).name == "agent-quality-gates", (
        "the managed link was followed into the version tree it points at"
    )


@pytest.mark.parametrize("shape", ["plain", "symlinked-home", "versions-elsewhere",
                                   "staged-tree-is-a-link", "unmanaged-checkout"])
def test_both_sides_agree_on_every_layout_review_has_found(tmp_path, monkeypatch, shape):
    """One string for one route, whatever shape the machine is in.

    Five rounds of review produced five ways to make two derivations of "the
    same" string disagree, and each fix produced the next case. They share one
    cause, so they are parametrised together rather than patched one at a time:
    the installer is handed the root as a human spelled it, the update engine
    resolves its own, and anything computed FROM those inputs can differ.

    The managed answer is therefore a constant — `~/.deeppattern/agent-quality-gates`
    is where AQG installs — and the only judgement left is physical identity.
    """
    sha = "a" * 40
    home = tmp_path / "home"
    if shape == "symlinked-home":
        real = tmp_path / "real"
        (real / ".deeppattern" / "versions" / sha).mkdir(parents=True)
        tree = real / ".deeppattern" / "versions" / sha
        home.symlink_to(real)
    elif shape == "versions-elsewhere":
        (home / ".deeppattern").mkdir(parents=True)
        tree = tmp_path / "data" / "versions" / sha
        tree.mkdir(parents=True)
        (home / ".deeppattern" / "versions").symlink_to(tmp_path / "data" / "versions")
        tree = home / ".deeppattern" / "versions" / sha
    elif shape == "staged-tree-is-a-link":
        (home / ".deeppattern" / "versions").mkdir(parents=True)
        (tmp_path / "staged").mkdir()
        tree = home / ".deeppattern" / "versions" / sha
        tree.symlink_to(tmp_path / "staged")
    else:
        (home / ".deeppattern" / "versions" / sha).mkdir(parents=True)
        tree = home / ".deeppattern" / "versions" / sha
    root = home / ".deeppattern" / "agent-quality-gates"
    root.symlink_to(tree)
    if shape == "unmanaged-checkout":
        # A developer checkout: no managed root anywhere. Both sides must still
        # agree, which they do because both canonicalise.
        root = tmp_path / "dev-checkout"
        (root / "agent-packs" / "claude-code" / "skills").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        tree = root
    else:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    # The two PRODUCTION paths, not `logical_root` compared with itself: an
    # earlier version of this test did the latter and so could not have caught a
    # disagreement between the installer's derivation and the planner's.
    sub = "agent-packs/claude-code/skills"
    skill = tree / sub / "aqg-x"
    skill.mkdir(parents=True, exist_ok=True)

    from scripts.aqg_skill_install import _link_text_for
    writer = _link_text_for(root / sub / "aqg-x", skill.resolve(), root)   # installer
    reader = base_mod.observed_routes.__wrapped__ if False else None
    reader_root = migrate.logical_root(root.resolve()) / sub / "aqg-x"     # planner

    assert str(writer) == str(reader_root), (
        f"{shape}: the installer would write {writer} and the planner looks for "
        f"{reader_root}; ownership is an exact text comparison, so every route "
        f"would look unowned and the update would stall — again"
    )
    if shape != "unmanaged-checkout":
        assert "versions" not in str(writer), (
            f"{shape}: the ownership root followed the managed link into the "
            f"version tree it points at, which is what pins routes to a release"
        )


def test_logical_root_normalises_without_resolving(tmp_path):
    """`.absolute()` alone was not enough: it does not collapse `.`, and
    ownership is an exact text comparison, so one directory reached two ways
    produced two spellings.

    Only `.` is pinned. An earlier version of this test also asserted that
    `a/x/../b` collapses to `a/b` — which lexical normalisation does, and which
    is exactly the case that changes meaning when `x` is a symlink. Affirming it
    would have made a hazard into a guarantee.
    """
    d = tmp_path / "a" / "b"
    d.mkdir(parents=True)
    assert migrate.logical_root(tmp_path / "a" / "." / "b") == d


def test_a_link_that_does_not_point_at_this_tree_is_not_adopted(tmp_path, monkeypatch):
    """Ownership decides what the prune loop may delete, so a directory merely
    SHAPED like the managed layout must not be adopted.

    The discriminator moved with the mechanism. It used to be structural — the
    path had to be spelled `.deeppattern/versions/...` — and that spelling is
    exactly what five rounds of review showed cannot be relied on. It is now
    physical identity: a symlink is the managed root only if it resolves to the
    very tree being asked about. A lookalike pointing anywhere else is not ours,
    whatever it is called and wherever it sits.
    """
    home = tmp_path / "home"
    (home / ".deeppattern").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    ours = tmp_path / "elsewhere" / "versions" / ("a" * 40)
    ours.mkdir(parents=True)
    decoy_target = tmp_path / "elsewhere" / "versions" / ("b" * 40)
    decoy_target.mkdir()
    (tmp_path / "elsewhere" / "agent-quality-gates").symlink_to(decoy_target)

    assert migrate.logical_root(ours) == ours.resolve(), (
        "a symlink that points at a DIFFERENT tree was adopted as the ownership "
        "root for this one; prune would then be aimed by a link we do not own"
    )

    # And one that DOES point at this tree, but sits somewhere AQG does not
    # install. Round 7 searched the tree's ancestors so a divergent `HOME` could
    # still find the managed root; measured, it did not fix that case and it made
    # any symlink of this name in any ancestor into an ownership root — which is
    # what decides what the prune loop may delete. The search is gone; this pins
    # its absence.
    (tmp_path / "elsewhere" / "versions" / "agent-quality-gates").symlink_to(ours)
    assert migrate.logical_root(ours) == ours.resolve(), (
        "a symlink planted in an ancestor was adopted as the ownership root"
    )


def test_a_pre_existing_version_pinned_route_is_converted_by_reinstalling(managed):
    """The migration every existing machine needs, measured rather than assumed.

    The audit was right to press on this: the previous round asserted 'one
    reinstall is enough' without ever running one, and the first attempt did
    NOT convert — `aqg_skill_install` resolved the source independently of how
    the installer spelled it.
    """
    old = managed["skills"]
    old.mkdir(parents=True)
    (old / "aqg-code-construction").symlink_to(
        managed["tree"] / "agent-packs/claude-code/skills/aqg-code-construction"
    )
    assert _adapter(managed)._observed_routes() == (), "fixture is not the old shape"

    _install(managed)

    observed = _adapter(managed)._observed_routes()
    assert observed and "aqg-code-construction" in observed, (
        "reinstalling did not adopt the version-pinned route, so an existing "
        "machine stays stalled no matter how often the user reinstalls"
    )


# --- R2: the spelling is derived from the layout, never from the environment --

@pytest.mark.parametrize("spelling", ["logical", "physical"])
def test_the_environment_does_not_decide_ownership(managed, monkeypatch, spelling):
    """`AQG_ROOT` holds the PHYSICAL path whenever an update check is started
    from a skill, because `_aqg_context.sh` pins it that way on purpose. An
    ownership rule that reads it is therefore wrong exactly when it matters.

    The adapter is built WITHOUT an explicit root, so the environment is the
    only thing that can supply one — an earlier version passed `aqg_root=`
    here, which meant the variable it was parametrised over was never read and
    the test could not have failed.
    """
    _install(managed)
    monkeypatch.setenv(
        "AQG_ROOT",
        str(managed["root"] if spelling == "logical" else managed["tree"]),
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: managed["home"]))
    adapter = ClaudeCodeAdapter(          # no aqg_root: it must come from $AQG_ROOT
        skills_dest=managed["skills"],
        settings_path=managed["home"] / ".claude" / "settings.json",
    )
    assert adapter._aqg_root is not None, "the adapter did not read $AQG_ROOT at all"
    assert Path(adapter._aqg_root).resolve() == Path(
        os.environ["AQG_ROOT"]
    ).resolve(), (
        f"the adapter's root did not come from $AQG_ROOT ({adapter._aqg_root}), "
        f"so this test would pass even if the environment were ignored"
    )
    assert len(adapter._observed_routes() or ()) >= 16, (
        f"observation changed with $AQG_ROOT set to the {spelling} spelling"
    )


def test_a_tilde_root_produces_the_same_link_text(managed, monkeypatch):
    """`Path.resolve()` does not expand `~` — it turns `~/x` into `<cwd>/~/x`.

    A caller passing an unexpanded tilde therefore missed the root entirely and
    the writer fell back to the physical spelling, silently restoring the stall
    the change exists to remove. Caught by an auditor, not by me: the mutation
    that removes the expansion left every other test green.
    """
    from scripts.aqg_skill_install import _link_text_for

    monkeypatch.setenv("HOME", str(managed["home"]))
    sub = "agent-packs/claude-code/skills/aqg-code-construction"
    real = (managed["root"] / sub).resolve()

    plain = _link_text_for(managed["root"] / sub, real, managed["root"])
    tilde = _link_text_for(
        Path("~/.deeppattern/agent-quality-gates") / sub, real,
        Path("~/.deeppattern/agent-quality-gates"),
    )
    assert tilde == plain, (
        f"a tilde-spelled root produced a different link text: {tilde} vs {plain}"
    )
    assert "versions" not in str(tilde), (
        "the tilde path fell back to the physical spelling, which is the stall"
    )


@pytest.mark.parametrize("ancestor_is_a_symlink", [False, True])
def test_both_sides_agree_however_the_ancestor_was_spelled(tmp_path, monkeypatch, ancestor_is_a_symlink):
    """The contract in one assertion: the installer is handed the root as a
    human spelled it, the update engine resolves its own, and both must produce
    the same ownership string.

    The symlinked-ancestor case is why this is parametrised. A `$HOME` that
    points elsewhere, or `/var` -> `/private/var` on macOS, made the two sides
    diverge above `.deeppattern` — and every route looked unowned again. The
    fixture used everywhere else in this file has no symlinked ancestor, so
    fourteen tests and eight mutations were green while this was broken. Found
    by an auditor asking what the fixture could not express.
    """
    base = tmp_path / "real"
    tree = base / ".deeppattern" / "versions" / ("a" * 40)
    tree.mkdir(parents=True)
    (base / ".deeppattern" / "agent-quality-gates").symlink_to(tree)
    if ancestor_is_a_symlink:
        (tmp_path / "home").symlink_to(base)
        home = tmp_path / "home"
        as_spelled = home / ".deeppattern" / "versions" / ("a" * 40)
    else:
        home = base
        as_spelled = tree
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    assert migrate.logical_root(as_spelled) == migrate.logical_root(tree.resolve()), (
        "the writer's spelling and the reader's resolved one produced different "
        "ownership roots, so every route would look unowned"
    )
    assert migrate.logical_root(as_spelled).name == "agent-quality-gates", (
        "the managed link was followed into the version tree it points at"
    )


@pytest.mark.parametrize("shape", ["plain", "symlinked-home", "versions-elsewhere",
                                   "staged-tree-is-a-link", "unmanaged-checkout"])
def test_both_sides_agree_on_every_layout_review_has_found(tmp_path, monkeypatch, shape):
    """One string for one route, whatever shape the machine is in.

    Five rounds of review produced five ways to make two derivations of "the
    same" string disagree, and each fix produced the next case. They share one
    cause, so they are parametrised together rather than patched one at a time:
    the installer is handed the root as a human spelled it, the update engine
    resolves its own, and anything computed FROM those inputs can differ.

    The managed answer is therefore a constant — `~/.deeppattern/agent-quality-gates`
    is where AQG installs — and the only judgement left is physical identity.
    """
    sha = "a" * 40
    home = tmp_path / "home"
    if shape == "symlinked-home":
        real = tmp_path / "real"
        (real / ".deeppattern" / "versions" / sha).mkdir(parents=True)
        tree = real / ".deeppattern" / "versions" / sha
        home.symlink_to(real)
    elif shape == "versions-elsewhere":
        (home / ".deeppattern").mkdir(parents=True)
        tree = tmp_path / "data" / "versions" / sha
        tree.mkdir(parents=True)
        (home / ".deeppattern" / "versions").symlink_to(tmp_path / "data" / "versions")
        tree = home / ".deeppattern" / "versions" / sha
    elif shape == "staged-tree-is-a-link":
        (home / ".deeppattern" / "versions").mkdir(parents=True)
        (tmp_path / "staged").mkdir()
        tree = home / ".deeppattern" / "versions" / sha
        tree.symlink_to(tmp_path / "staged")
    else:
        (home / ".deeppattern" / "versions" / sha).mkdir(parents=True)
        tree = home / ".deeppattern" / "versions" / sha
    root = home / ".deeppattern" / "agent-quality-gates"
    root.symlink_to(tree)
    if shape == "unmanaged-checkout":
        # A developer checkout: no managed root anywhere. Both sides must still
        # agree, which they do because both canonicalise.
        root = tmp_path / "dev-checkout"
        (root / "agent-packs" / "claude-code" / "skills").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        tree = root
    else:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    # The two PRODUCTION paths, not `logical_root` compared with itself: an
    # earlier version of this test did the latter and so could not have caught a
    # disagreement between the installer's derivation and the planner's.
    sub = "agent-packs/claude-code/skills"
    skill = tree / sub / "aqg-x"
    skill.mkdir(parents=True, exist_ok=True)

    from scripts.aqg_skill_install import _link_text_for
    writer = _link_text_for(root / sub / "aqg-x", skill.resolve(), root)   # installer
    reader = base_mod.observed_routes.__wrapped__ if False else None
    reader_root = migrate.logical_root(root.resolve()) / sub / "aqg-x"     # planner

    assert str(writer) == str(reader_root), (
        f"{shape}: the installer would write {writer} and the planner looks for "
        f"{reader_root}; ownership is an exact text comparison, so every route "
        f"would look unowned and the update would stall — again"
    )
    if shape != "unmanaged-checkout":
        assert "versions" not in str(writer), (
            f"{shape}: the ownership root followed the managed link into the "
            f"version tree it points at, which is what pins routes to a release"
        )


def test_logical_root_normalises_without_resolving(tmp_path):
    """`.absolute()` alone was not enough: it does not collapse `.`, and
    ownership is an exact text comparison, so one directory reached two ways
    produced two spellings.

    Only `.` is pinned. An earlier version of this test also asserted that
    `a/x/../b` collapses to `a/b` — which lexical normalisation does, and which
    is exactly the case that changes meaning when `x` is a symlink. Affirming it
    would have made a hazard into a guarantee.
    """
    d = tmp_path / "a" / "b"
    d.mkdir(parents=True)
    assert migrate.logical_root(tmp_path / "a" / "." / "b") == d


def test_an_already_routed_host_plans_no_route(managed):
    """The stall itself, with a state that records nothing — which is what
    every installer leaves behind."""
    _install(managed)
    built = plan_mod.build_plan(
        state=None,
        target=managed["tree"],
        evidence={"claude-code": Evidence(
            client_id="claude-code", hooks_status="complete", hooks_detail="",
            recorded_version=None,
            routed_skills=_adapter(managed)._observed_routes(),
        )},
        target_commit="a" * 40,
    )
    assert not [a for a in built.actions if a.kind == "route_skill"], (
        "an install that is already correct was planned again, which is the "
        "stall: route_skill is host-touching, so nothing would be applied"
    )


# --- R4: one host's bad data must not stop the others -------------------------

def test_an_unsafe_name_cannot_reach_a_planner_at_all():
    """The guard lives on the contract, so there is no downstream case to handle.

    It used to live in the planner and produce a deferral — and a deferral
    becomes an outstanding item, which holds back the whole machine's apply.
    One unusable name in one shared directory would have stopped a
    signature-verified security update channel with no bound and no escape.
    Refusing the evidence at construction keeps the guard and removes the
    consequence.

    It cannot arise from `owned_routes` either — a directory entry name can
    hold no separator — so this is the boundary for an adapter that builds
    evidence some other way.
    """
    for name in ("..", "a/b", "/abs", ".", ""):
        with pytest.raises(AdapterError, match="routed_skills"):
            Evidence(
                client_id="claude-code", hooks_status="complete", hooks_detail="",
                recorded_version=None, routed_skills=(name,),
            )


def test_a_name_forced_past_the_contract_skips_that_host_only(managed):
    """`Evidence` refuses these at construction, but a frozen dataclass is not
    sealed — `object.__setattr__` writes through one, and this repo's own tests
    do exactly that. So the layer that hands names to a filesystem keeps its own
    check, and its answer is *skip*, not *defer*: a deferral becomes an
    outstanding item and holds the whole machine's apply, which is how a guard
    becomes a denial of service.
    """
    _install(managed)
    good = _adapter(managed)._observed_routes()
    forced = Evidence(
        client_id="claude-code", hooks_status="complete", hooks_detail="",
        recorded_version=None, routed_skills=("aqg-ok",))
    object.__setattr__(forced, "routed_skills", ("a/b",))

    built = plan_mod.build_plan(
        state=None, target=managed["tree"], target_commit="a" * 40,
        evidence={"claude-code": forced,
                  "codex": Evidence(client_id="codex", hooks_status="complete",
                                    hooks_detail="", recorded_version=None,
                                    routed_skills=good)})
    assert not [a for a in built.actions if a.subject == "a/b"], (
        "a name that is not one path component was planned into an action"
    )
    assert not [d for d in built.deferred if d.client_id == "claude-code"], (
        "the guard deferred the host, which holds back the whole apply"
    )
    assert not [a for a in built.actions
                if a.client_id == "codex" and a.kind == "route_skill"], (
        "one host's forced-through name stopped planning for a host that was fine"
    )


def test_one_hosts_absent_skills_directory_does_not_stop_the_others(managed):
    """`None` for one host must not become work, or a stall, for another."""
    _install(managed)
    good = _adapter(managed)._observed_routes()
    built = plan_mod.build_plan(
        state=None, target=managed["tree"], target_commit="a" * 40,
        evidence={
            "claude-code": Evidence(
                client_id="claude-code", hooks_status="complete", hooks_detail="",
                recorded_version=None, routed_skills=None),
            "codex": Evidence(
                client_id="codex", hooks_status="complete", hooks_detail="",
                recorded_version=None, routed_skills=good),
        },
    )
    assert not [a for a in built.actions
                if a.kind in {"route_skill", "prune_skill"}], (
        "a host with no skills destination produced skill work for someone"
    )
    assert not [d for d in built.deferred if d.client_id == "codex"], (
        "one host's absence held back another"
    )


def test_a_malformed_roster_is_refused_by_the_contract():
    """A bare string is iterable, so `"nope"` would plan four routes named
    `n`, `o`, `p`, `e`. Caught on `Evidence`, before any planner sees it."""
    for bad in ("nope", ("",), ["aqg-x"]):
        with pytest.raises(AdapterError, match="routed_skills"):
            Evidence(client_id="claude-code", hooks_status="complete",
                     hooks_detail="", recorded_version=None, routed_skills=bad)


# --- R5: the destructive path ------------------------------------------------

def test_a_dropped_skill_is_pruned_and_a_foreign_entry_is_not(managed):
    """Pruning is the only destructive action in the system, and this change
    alters the very input that decides what gets pruned — so it needs a test
    that watches something actually get deleted, and something else survive.

    The survivor is the point. That directory is shared with the user's own
    skills and other tools' skills, and `skills_route` only ever removes a link
    whose text is exactly what it would have written.
    """
    _install(managed)
    source = managed["root"] / "agent-packs/claude-code/skills"
    dest = managed["skills"]

    # something of ours, and two things that are not
    mine = "aqg-code-construction"
    (dest / "user-own-skill").mkdir()
    # Deliberately a link a PREFIX test would claim and an exact test would not:
    # its text starts with `source_root` but names something one level deeper.
    # A `startswith` ownership check deletes this; the real one must not.
    (dest / "aqg-lookalike").symlink_to(source / "aqg-security-review" / "scripts")

    owned = skills_route.owned_routes(source_root=source, dest_root=dest)
    assert mine in owned, "our own route was not recognised"
    assert "aqg-lookalike" not in owned, (
        "a link this layer never wrote was claimed as ours. Ownership is an "
        "exact text match for a reason: anything looser reaches links the user "
        "or another tool created, and the next prune deletes them."
    )
    assert "user-own-skill" not in owned

    removed = skills_route.prune(name=mine, source_root=source, dest_root=dest)
    assert removed and not (dest / mine).exists(), "our own route was not pruned"

    assert not skills_route.prune(
        name="aqg-lookalike", source_root=source, dest_root=dest
    ), "prune agreed to remove a link it never created"
    assert (dest / "user-own-skill").is_dir(), "a user's own skill was deleted"
    assert (dest / "aqg-lookalike").is_symlink(), "a foreign link was deleted"


# --- R6: the checked object is the used object --------------------------------

def test_copy_mode_reads_the_path_it_validated(managed, monkeypatch):
    """`install_skill` validates `SKILL.md` on the resolved path, so the copy
    must read that same path.

    Splitting the two spellings introduced this gap: for a moment the copy read
    the LOGICAL path, whose middle segment is a symlink that can be re-pointed
    between the check and the read. Symlink mode is unaffected — writing a
    symlink is writing text, and the text is resolved by whoever follows it.

    Asserted at the internal boundary because the defect IS which path crosses
    it; the race that exploits it cannot be made deterministic, and a test that
    cannot fail is not evidence.
    """
    from scripts import aqg_skill_install as installer

    seen = {}
    monkeypatch.setattr(
        installer, "_copy_skill",
        lambda source, target, root: seen.update(source=Path(source)),
    )
    logical = managed["root"] / "agent-packs/claude-code/skills/aqg-code-construction"
    installer.install_skill(
        logical, managed["home"] / "copied" / "aqg-code-construction",
        requested_mode="copy", force=True, aqg_root=managed["root"],
    )
    assert seen["source"] == logical.resolve(), (
        f"the copy read {seen['source']}, which is not the path validated "
        f"({logical.resolve()}); a swap of the root link between the two is "
        f"enough to copy a directory that was never checked"
    )
