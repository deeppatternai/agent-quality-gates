"""Behavior contracts for routing skills into a host's skills directory.

docs/UPDATE_ARCHITECTURE.md §8. A skill is delivered as a symlink from the
host's skills directory into the AQG checkout, so editing a skill's content is
live immediately and only the roster ever needs work.

Every case here is about **ownership**. The host's skills directory is shared:
it holds the user's own skills, other tools' skills, and ours. Removing
something we did not create is unrecoverable, so the rule is narrow — we touch a
symlink only when it points at exactly `<source>/<one component>`, which is the
only shape we ever create. That rule is ported from the shell installer, whose
comment records the case that forced it: `<source>/../scripts` matches a text
prefix and resolves outside.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.aqg_update import skills_route as route_mod


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """An AQG checkout's skills directory holding two skills."""
    root = tmp_path / "checkout" / "skills"
    for name in ("aqg-code-construction", "aqg-security-review"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(name, encoding="utf-8")
    return root


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    d = tmp_path / "host" / "skills"
    d.mkdir(parents=True)
    return d


# --- routing --------------------------------------------------------------------


def test_a_routed_skill_reads_through_to_the_checkout(source, dest):
    route_mod.route(name="aqg-code-construction", source_root=source, dest_root=dest)
    link = dest / "aqg-code-construction"
    assert link.is_symlink()
    assert (link / "SKILL.md").read_text(encoding="utf-8") == "aqg-code-construction"


def test_routing_an_already_correct_route_leaves_it_alone(source, dest):
    """Re-creating a correct link on every run would churn the filesystem and
    race a concurrent session reading through it, for no gain."""
    route_mod.route(name="aqg-code-construction", source_root=source, dest_root=dest)
    link = dest / "aqg-code-construction"
    before = os.lstat(link).st_ino
    created = route_mod.route(
        name="aqg-code-construction", source_root=source, dest_root=dest
    )
    assert created is False
    assert os.lstat(link).st_ino == before


def test_routing_never_replaces_a_real_directory(source, dest):
    """A directory of that name is the user's own skill. Replacing it is
    unrecoverable, so it is refused rather than backed up and clobbered."""
    theirs = dest / "aqg-code-construction"
    theirs.mkdir()
    (theirs / "SKILL.md").write_text("mine", encoding="utf-8")
    with pytest.raises(route_mod.RouteError, match="not a route"):
        route_mod.route(name="aqg-code-construction", source_root=source, dest_root=dest)
    assert (theirs / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_routing_never_replaces_a_link_we_did_not_create(source, dest, tmp_path):
    elsewhere = tmp_path / "someone-elses-skill"
    elsewhere.mkdir()
    link = dest / "aqg-code-construction"
    link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(route_mod.RouteError, match="not a route"):
        route_mod.route(name="aqg-code-construction", source_root=source, dest_root=dest)
    assert Path(os.readlink(link)) == elsewhere


def test_routing_a_skill_the_checkout_does_not_ship_is_refused(source, dest):
    """A dangling route is worse than no route: the host lists a skill that
    cannot be read."""
    with pytest.raises(route_mod.RouteError, match="does not ship"):
        route_mod.route(name="aqg-not-a-skill", source_root=source, dest_root=dest)
    assert not (dest / "aqg-not-a-skill").exists()


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "/abs"])
def test_routing_an_unsafe_name_is_refused(source, dest, name):
    with pytest.raises(route_mod.RouteError, match="name"):
        route_mod.route(name=name, source_root=source, dest_root=dest)


# --- pruning ----------------------------------------------------------------------


def test_pruning_removes_our_own_route(source, dest):
    route_mod.route(name="aqg-code-construction", source_root=source, dest_root=dest)
    removed = route_mod.prune(
        name="aqg-code-construction", source_root=source, dest_root=dest
    )
    assert removed is True
    assert not (dest / "aqg-code-construction").exists()


def test_pruning_leaves_a_real_directory_alone(source, dest):
    theirs = dest / "aqg-code-construction"
    theirs.mkdir()
    (theirs / "SKILL.md").write_text("mine", encoding="utf-8")
    assert (
        route_mod.prune(
            name="aqg-code-construction", source_root=source, dest_root=dest
        )
        is False
    )
    assert (theirs / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_pruning_leaves_a_foreign_symlink_alone(source, dest, tmp_path):
    elsewhere = tmp_path / "someone-elses-skill"
    elsewhere.mkdir()
    link = dest / "aqg-code-construction"
    link.symlink_to(elsewhere, target_is_directory=True)
    assert (
        route_mod.prune(
            name="aqg-code-construction", source_root=source, dest_root=dest
        )
        is False
    )
    assert link.is_symlink()


def test_pruning_refuses_a_link_that_only_looks_like_ours(source, dest):
    """`<source>/../scripts` matches a text prefix and resolves OUTSIDE the
    skills directory. The shell installer's comment records this case as the one
    that forced the rule to be a shape check rather than a prefix match."""
    escaping = source / ".." / "scripts"
    escaping.mkdir(parents=True, exist_ok=True)
    (escaping / "sentinel").write_text("do not delete", encoding="utf-8")
    link = dest / "aqg-code-construction"
    link.symlink_to(escaping, target_is_directory=True)
    assert (
        route_mod.prune(
            name="aqg-code-construction", source_root=source, dest_root=dest
        )
        is False
    )
    assert link.is_symlink()
    assert (source.parent / "scripts" / "sentinel").exists()


def test_pruning_a_deeper_link_is_refused(source, dest):
    """We only ever create `<source>/<one component>`. Anything deeper was made
    by someone else."""
    deeper = source / "aqg-code-construction" / "nested"
    deeper.mkdir()
    link = dest / "aqg-code-construction"
    link.symlink_to(deeper, target_is_directory=True)
    assert (
        route_mod.prune(
            name="aqg-code-construction", source_root=source, dest_root=dest
        )
        is False
    )
    assert link.is_symlink()


def test_pruning_something_absent_is_not_an_error(source, dest):
    """A prune planned from recorded state may name a route a user already
    removed by hand. That is the desired end state, not a failure."""
    assert (
        route_mod.prune(name="aqg-security-review", source_root=source, dest_root=dest)
        is False
    )


def test_pruning_removes_a_dangling_route_of_ours(source, dest):
    """The tree it pointed into is gone, but the link shape is still ours and a
    dangling entry makes the host list a skill it cannot read."""
    route_mod.route(name="aqg-code-construction", source_root=source, dest_root=dest)
    import shutil

    shutil.rmtree(source / "aqg-code-construction")
    assert (
        route_mod.prune(
            name="aqg-code-construction", source_root=source, dest_root=dest
        )
        is True
    )


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "/abs"])
def test_pruning_an_unsafe_name_is_refused(source, dest, name):
    with pytest.raises(route_mod.RouteError, match="name"):
        route_mod.prune(name=name, source_root=source, dest_root=dest)


# --- what we own ------------------------------------------------------------------


def test_owned_routes_lists_only_ours(source, dest, tmp_path):
    route_mod.route(name="aqg-code-construction", source_root=source, dest_root=dest)
    (dest / "their-skill").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (dest / "their-link").symlink_to(elsewhere, target_is_directory=True)
    assert route_mod.owned_routes(source_root=source, dest_root=dest) == (
        "aqg-code-construction",
    )


def test_owned_routes_on_an_absent_destination_is_empty(source, tmp_path):
    assert (
        route_mod.owned_routes(source_root=source, dest_root=tmp_path / "nope") == ()
    )


# --- fixes from audit aud_Ge4_6C5o1NZuYlj7 ------------------------------------
#
# P8: the earlier ownership tests each probed ONE adversarial shape and passed,
# while the neighbours of that shape failed. These are parametrized over the
# class instead of a representative of it.


@pytest.mark.parametrize(
    "make_target",
    [
        pytest.param(lambda src, n: f"{src}/other/../{n}", id="mid-path-dotdot"),
        pytest.param(lambda src, n: f"{src}/aqg-security-review", id="name-mismatch"),
        pytest.param(lambda src, n: n, id="relative-text"),
        pytest.param(lambda src, n: f"{src}/{n}/", id="trailing-slash"),
        pytest.param(lambda src, n: f"{src}//{n}", id="doubled-slash"),
        pytest.param(lambda src, n: f"{src}/./{n}", id="dot-component"),
    ],
)
def test_an_adversarial_link_shape_is_never_claimed_as_ours(source, dest, make_target):
    """Every one of these resolves at or into the source directory, and none is
    the text this module writes. Claiming one means deleting a link we did not
    create.

    The targets are built as RAW STRINGS on purpose: `Path` normalises a
    trailing slash, a doubled slash and a `.` component away before the symlink
    is created, so a `Path`-built fixture would silently test our own shape and
    pass for the wrong reason.
    """
    name = "aqg-code-construction"
    (source / "other").mkdir(exist_ok=True)
    link = dest / name
    os.symlink(make_target(source, name), link)
    assert route_mod.prune(name=name, source_root=source, dest_root=dest) is False
    assert link.is_symlink()
    assert route_mod.owned_routes(source_root=source, dest_root=dest) == ()


def test_an_adversarial_shape_also_blocks_routing_rather_than_passing_as_correct(
    source, dest
):
    """`route` used to read a mismatched link as "already correct" and return
    False, leaving the host serving one skill's content under another's name."""
    name = "aqg-code-construction"
    os.symlink(source / "aqg-security-review", dest / name)
    with pytest.raises(route_mod.RouteError, match="not a route"):
        route_mod.route(name=name, source_root=source, dest_root=dest)


def test_a_symlink_loop_in_the_target_is_not_claimed_and_does_not_crash(source, dest):
    a = dest / "loop-a"
    b = dest / "loop-b"
    a.symlink_to(b)
    b.symlink_to(a)
    assert route_mod.owned_routes(source_root=source, dest_root=dest) == ()


def test_an_entry_swapped_for_a_regular_file_before_the_unlink_is_not_deleted(
    source, dest, monkeypatch
):
    """`os.unlink` on a path that stopped being a symlink deletes that file."""
    name = "aqg-code-construction"
    route_mod.route(name=name, source_root=source, dest_root=dest)
    link = dest / name
    original_is_our = route_mod._is_our_route

    def _swap_then_answer(candidate, src):
        answer = original_is_our(candidate, src)
        if answer and candidate == link:
            link.unlink()
            link.write_text("someone else's file", encoding="utf-8")
        return answer

    monkeypatch.setattr(route_mod, "_is_our_route", _swap_then_answer)
    assert route_mod.prune(name=name, source_root=source, dest_root=dest) is False
    assert link.read_text(encoding="utf-8") == "someone else's file"


@pytest.mark.parametrize("call", ["route", "prune", "owned_routes"])
def test_a_relative_source_root_is_refused(source, dest, call):
    """Recognition depends on a canonical spelling: a relative root makes the
    module refuse to recognize its own past work, so `route` raises on links it
    created and `prune` silently leaves them."""
    relative = Path(os.path.relpath(source, Path.cwd()))
    with pytest.raises(route_mod.RouteError, match="absolute"):
        if call == "owned_routes":
            route_mod.owned_routes(source_root=relative, dest_root=dest)
        else:
            getattr(route_mod, call)(
                name="aqg-code-construction", source_root=relative, dest_root=dest
            )
