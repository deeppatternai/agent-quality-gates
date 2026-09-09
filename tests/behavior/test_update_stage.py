"""Behavior contracts for materializing a version and switching to it.

docs/UPDATE_ARCHITECTURE.md §5.1. The update is **additive**: a new version is
materialized beside the live one and a symlink is re-pointed, so nothing is ever
overwritten in place. That is what makes an update safe while sessions are
running — a hook that already opened a script keeps reading the tree it opened,
because that tree is still there.

The refusal this file cares about most is the migration case. Every machine
installed today has `~/.deeppattern/agent-quality-gates` as a **real git
checkout**, not a symlink. Swapping must refuse to touch it rather than replace
it: that directory is the user's entire install, and clobbering it is
unrecoverable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.aqg_update import stage as stage_mod


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny real repository with two commits."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    (root / "VERSION").write_text("0.15.0\n", encoding="utf-8")
    (root / "marker.txt").write_text("first", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "first", cwd=root)
    (root / "marker.txt").write_text("second", encoding="utf-8")
    _git("commit", "-qam", "second", cwd=root)
    return root


def _commit(repo: Path, rev: str) -> str:
    return _git("rev-parse", rev, cwd=repo)


# --- materializing a version ---------------------------------------------------


@pytest.mark.parametrize('kind', ['partial', 'dangling', 'modified'])
def test_retry_name_never_reuses_occupied_content(tmp_path, repo, kind):
    versions = tmp_path / 'versions'
    versions.mkdir()
    commit = _commit(repo, 'HEAD')
    old = versions / '0.15.0'
    if kind == 'dangling':
        old.symlink_to(tmp_path / 'missing', target_is_directory=True)
    elif kind == 'partial':
        old.mkdir()
    else:
        stage_mod.stage_version(repo=repo, commit=commit, versions_dir=versions, name=old.name)
        (old / 'marker.txt').write_text('tampered')
    name = stage_mod.version_name('0.15.0', commit, versions)
    assert name != old.name and len(name) <= 40
    fresh = stage_mod.stage_version(repo=repo, commit=commit, versions_dir=versions, name=name)
    assert (fresh / 'marker.txt').read_text() == 'second'
    assert old.exists() or old.is_symlink()


def test_unremovable_retries_are_bounded_and_resume_after_space_is_freed(tmp_path, repo):
    versions = tmp_path / 'versions'
    versions.mkdir()
    commit = _commit(repo, 'HEAD')
    attempts = []
    for _ in range(4):
        name = stage_mod.version_name('0.15.0', commit, versions)
        attempts.append(stage_mod.stage_version(repo=repo, commit=commit, versions_dir=versions, name=name))
    with pytest.raises(stage_mod.StageError, match='retained retry'):
        stage_mod.version_name('0.15.0', commit, versions)
    assert len(list(versions.iterdir())) == 4
    stage_mod.discard_version(repo=repo, target=attempts[-1])
    name = stage_mod.version_name('0.15.0', commit, versions)
    assert not (versions / name).exists()


def test_a_staged_version_has_that_commit_content(tmp_path, repo):
    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD~1"), versions_dir=versions, name="v1"
    )
    assert (tree / "marker.txt").read_text(encoding="utf-8") == "first"


def test_a_staged_version_is_blob_exact_under_a_converting_config(tmp_path, repo):
    """Staging must write the blob bytes, whatever the ambient git config says.

    `acquire` verifies a COMMIT; everything downstream trusts that the staged
    tree IS that commit. Checkout conversion breaks that silently: with
    `core.autocrlf=true` — the Git for Windows installer default — `worktree
    add` writes LF blobs out as CRLF, so the bytes that run are not the bytes
    that were verified. A shell script is the sharp case, because a CR before
    the newline rides into the interpreter line.

    A tracked `*.sh text eol=lf` used to mask this for shell scripts only, and
    nothing covered `.py`, which is most of what the engine installs. The
    attribute is gone (WS-8 forbids a tracked .gitattributes), so the engine
    pins conversion off at its own checkout instead — which covers every file,
    not just the ones an attribute happened to name.
    """
    _git("config", "core.autocrlf", "true", cwd=repo)
    _git("config", "core.eol", "crlf", cwd=repo)
    (repo / "hook.sh").write_bytes(b"#!/usr/bin/env bash\necho hi\n")
    (repo / "mod.py").write_bytes(b"x = 1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "a shell script and a module", cwd=repo)
    commit = _commit(repo, "HEAD")

    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=commit, versions_dir=versions, name="v1"
    )

    for name in ("hook.sh", "mod.py"):
        blob = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{name}"],
            capture_output=True, check=True,
        ).stdout
        assert b"\r" not in blob, f"{name}: the fixture's own blob is not LF"
        assert (tree / name).read_bytes() == blob, (
            f"{name}: the staged bytes are not the blob's"
        )


def test_two_versions_coexist(tmp_path, repo):
    """The whole design: the old tree stays readable while the new one lands."""
    versions = tmp_path / "versions"
    old = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD~1"), versions_dir=versions, name="v1"
    )
    new = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v2"
    )
    assert (old / "marker.txt").read_text(encoding="utf-8") == "first"
    assert (new / "marker.txt").read_text(encoding="utf-8") == "second"


def test_staging_over_an_existing_name_is_refused(tmp_path, repo):
    """Silently reusing a directory would hand back a tree whose contents nobody
    verified — the staged version might be from an earlier, failed attempt."""
    versions = tmp_path / "versions"
    stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v1"
    )
    with pytest.raises(stage_mod.StageError, match="exists"):
        stage_mod.stage_version(
            repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v1"
        )


def test_an_unknown_commit_fails_closed_and_leaves_nothing_behind(tmp_path, repo):
    versions = tmp_path / "versions"
    with pytest.raises(stage_mod.StageError):
        stage_mod.stage_version(
            repo=repo, commit="0" * 40, versions_dir=versions, name="v1"
        )
    assert not (versions / "v1").exists()


# --- switching --------------------------------------------------------------


def test_the_root_points_at_the_version_after_a_swap(tmp_path, repo):
    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v2"
    )
    root = tmp_path / "agent-quality-gates"
    stage_mod.swap_root(root=root, target=tree)
    assert root.is_symlink()
    assert (root / "marker.txt").read_text(encoding="utf-8") == "second"


def test_a_swap_reports_the_version_it_replaced(tmp_path, repo):
    """Rollback needs to know where to go back to, and asking afterwards is too
    late — the link has already moved."""
    versions = tmp_path / "versions"
    first = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD~1"), versions_dir=versions, name="v1"
    )
    second = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v2"
    )
    root = tmp_path / "agent-quality-gates"
    assert stage_mod.swap_root(root=root, target=first) is None
    assert stage_mod.swap_root(root=root, target=second) == first


def test_swapping_back_restores_the_previous_version(tmp_path, repo):
    versions = tmp_path / "versions"
    first = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD~1"), versions_dir=versions, name="v1"
    )
    second = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v2"
    )
    root = tmp_path / "agent-quality-gates"
    stage_mod.swap_root(root=root, target=first)
    stage_mod.swap_root(root=root, target=second)
    stage_mod.swap_root(root=root, target=first)
    assert (root / "marker.txt").read_text(encoding="utf-8") == "first"


def test_the_replaced_tree_stays_readable_after_a_swap(tmp_path, repo):
    """This is what makes an update safe while sessions run: a hook that already
    resolved the old tree keeps reading it. Nothing was overwritten."""
    versions = tmp_path / "versions"
    first = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD~1"), versions_dir=versions, name="v1"
    )
    second = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v2"
    )
    root = tmp_path / "agent-quality-gates"
    stage_mod.swap_root(root=root, target=first)
    handle = open(first / "marker.txt", encoding="utf-8")
    try:
        stage_mod.swap_root(root=root, target=second)
        assert handle.read() == "first"
        assert (first / "marker.txt").read_text(encoding="utf-8") == "first"
    finally:
        handle.close()


def test_current_target_reports_where_the_root_points(tmp_path, repo):
    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v2"
    )
    root = tmp_path / "agent-quality-gates"
    assert stage_mod.current_target(root) is None
    stage_mod.swap_root(root=root, target=tree)
    assert stage_mod.current_target(root) == tree


# --- the migration refusal ------------------------------------------------------


def test_a_real_directory_at_the_root_is_never_replaced(tmp_path, repo):
    """Every machine installed today has a real git checkout there. Replacing it
    would destroy the user's entire install, unrecoverably."""
    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v2"
    )
    root = tmp_path / "agent-quality-gates"
    root.mkdir()
    (root / "VERSION").write_text("0.14.0\n", encoding="utf-8")
    with pytest.raises(stage_mod.StageError, match="not a symlink"):
        stage_mod.swap_root(root=root, target=tree)
    assert (root / "VERSION").read_text(encoding="utf-8") == "0.14.0\n"


def test_a_regular_file_at_the_root_is_never_replaced(tmp_path, repo):
    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v2"
    )
    root = tmp_path / "agent-quality-gates"
    root.write_text("not an install", encoding="utf-8")
    with pytest.raises(stage_mod.StageError, match="not a symlink"):
        stage_mod.swap_root(root=root, target=tree)
    assert root.read_text(encoding="utf-8") == "not an install"


def test_swapping_to_something_that_is_not_a_version_tree_is_refused(tmp_path, repo):
    """A root pointing anywhere but a materialized version is how an install
    silently becomes unreproducible."""
    root = tmp_path / "agent-quality-gates"
    stray = tmp_path / "stray"
    stray.mkdir()
    with pytest.raises(stage_mod.StageError):
        stage_mod.swap_root(root=root, target=stray)


# --- retention ------------------------------------------------------------------


def test_pruning_keeps_the_requested_number_of_newest_versions(tmp_path, repo):
    versions = tmp_path / "versions"
    made = [
        stage_mod.stage_version(
            repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name=f"v{i}"
        )
        for i in range(4)
    ]
    for index, tree in enumerate(made):
        os.utime(tree, (index, index))
    removed = stage_mod.prune_versions(versions_dir=versions, keep=2, protected=(), repo=repo)
    assert set(removed) == {made[0], made[1]}
    assert made[2].exists() and made[3].exists()


def test_pruning_never_removes_a_protected_version(tmp_path, repo):
    """The tree the root points at, and any the journal still references, must
    survive whatever the retention count says."""
    versions = tmp_path / "versions"
    made = [
        stage_mod.stage_version(
            repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name=f"v{i}"
        )
        for i in range(3)
    ]
    for index, tree in enumerate(made):
        os.utime(tree, (index, index))
    removed = stage_mod.prune_versions(
        versions_dir=versions, keep=1, protected=(made[0],), repo=repo
    )
    assert made[0].exists()
    assert made[0] not in removed


def test_pruning_touches_nothing_outside_the_versions_directory(tmp_path, repo):
    versions = tmp_path / "versions"
    versions.mkdir()
    outsider = tmp_path / "not-a-version"
    outsider.mkdir()
    (outsider / "keep").write_text("x", encoding="utf-8")
    stage_mod.prune_versions(versions_dir=versions, keep=0, protected=(), repo=repo)
    assert (outsider / "keep").exists()


def test_pruning_an_absent_versions_directory_is_not_an_error(tmp_path, repo):
    assert stage_mod.prune_versions(
        versions_dir=tmp_path / "nope", keep=2, protected=(), repo=repo
    ) == ()


# --- fixes from audit aud_JNQoWlajKzLYGIst -------------------------------------


@pytest.mark.parametrize("name", ["../escape", "a/b", "/tmp/absolute", "..", ""])
def test_an_unsafe_version_name_is_refused(tmp_path, repo, name):
    """`versions_dir / name` escapes with `..` and is replaced outright by an
    absolute name, and the failure path then rmtrees at wherever that landed."""
    with pytest.raises(stage_mod.StageError, match="name"):
        stage_mod.stage_version(
            repo=repo,
            commit=_commit(repo, "HEAD"),
            versions_dir=tmp_path / "versions",
            name=name,
        )


def test_a_symlinked_versions_directory_is_refused_by_pruning(tmp_path, repo):
    """`iterdir()` on a symlink lists the TARGET's children, so pruning would
    delete outside the directory it claims to confine itself to."""
    real = tmp_path / "real"
    real.mkdir()
    victim = real / "not-ours"
    victim.mkdir()
    (victim / "keep").write_text("x", encoding="utf-8")
    link = tmp_path / "versions"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(stage_mod.StageError, match="symlink"):
        stage_mod.prune_versions(versions_dir=link, keep=0, protected=(), repo=repo)
    assert (victim / "keep").exists()


def test_a_relative_target_is_refused_by_the_swap(tmp_path, repo):
    """A relative target passes both content checks and then writes relative
    link text, which resolves against root.parent — a dangling root right after
    a "successful" swap."""
    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v1"
    )
    root = tmp_path / "agent-quality-gates"
    relative = Path(os.path.relpath(tree, Path.cwd()))
    with pytest.raises(stage_mod.StageError, match="absolute"):
        stage_mod.swap_root(root=root, target=relative)
    assert not root.exists()


def test_current_target_absolutizes_a_relative_link(tmp_path, repo):
    """A hand-made relative root link must not be reported as a path the caller
    would resolve against its own cwd."""
    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v1"
    )
    root = tmp_path / "agent-quality-gates"
    os.symlink(os.path.relpath(tree, tmp_path), root)
    assert stage_mod.current_target(root) == tree


def test_pruning_never_removes_the_live_version(tmp_path, repo):
    """The docstring used to guarantee this while the function had no idea which
    tree was live."""
    versions = tmp_path / "versions"
    made = [
        stage_mod.stage_version(
            repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name=f"v{i}"
        )
        for i in range(3)
    ]
    for index, tree in enumerate(made):
        os.utime(tree, (index, index))
    root = tmp_path / "agent-quality-gates"
    stage_mod.swap_root(root=root, target=made[0])
    removed = stage_mod.prune_versions(
        versions_dir=versions, keep=1, protected=(), repo=repo, root=root
    )
    assert made[0].exists()
    assert made[0] not in removed


def test_pruning_refuses_to_remove_a_directory_that_is_not_a_version_tree(
    tmp_path, repo
):
    """The sentinel was checked before the reversible operation and not before
    the irreversible one. That was backwards."""
    versions = tmp_path / "versions"
    versions.mkdir()
    stranger = versions / "someone-elses-data"
    stranger.mkdir()
    (stranger / "important") .write_text("x", encoding="utf-8")
    removed = stage_mod.prune_versions(
        versions_dir=versions, keep=0, protected=(), repo=repo
    )
    assert (stranger / "important").exists()
    assert stranger not in removed


def test_pruning_removes_the_git_registration_too(tmp_path, repo):
    """An rmtree'd worktree leaves a stale entry in .git/worktrees, and the next
    `git worktree add` for that name then refuses."""
    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v1"
    )
    stage_mod.prune_versions(versions_dir=versions, keep=0, protected=(), repo=repo)
    assert not tree.exists()
    assert "v1" not in _git("worktree", "list", cwd=repo)
    stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v1"
    )


def test_a_surviving_corpse_is_named_in_the_error(tmp_path, repo, monkeypatch):
    """Cleanup is best-effort; when it fails the operator must learn that here,
    not from a bare "already exists" on the next attempt."""
    versions = tmp_path / "versions"

    def _leave_it(*_args, **_kwargs):
        (versions / "v1").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(stage_mod, "_remove_worktree", _leave_it)
    with pytest.raises(stage_mod.StageError, match="manual"):
        stage_mod.stage_version(
            repo=repo, commit="0" * 40, versions_dir=versions, name="v1"
        )


def test_a_root_symlink_pointing_outside_the_versions_directory_is_refused(
    tmp_path, repo
):
    """A user symlink to a real checkout — the relocate-to-another-disk pattern —
    is not an install this layer created, and "is a symlink" was standing in for
    "was made by us"."""
    versions = tmp_path / "versions"
    tree = stage_mod.stage_version(
        repo=repo, commit=_commit(repo, "HEAD"), versions_dir=versions, name="v1"
    )
    elsewhere = tmp_path / "relocated-checkout"
    elsewhere.mkdir()
    (elsewhere / "VERSION").write_text("0.14.0\n", encoding="utf-8")
    root = tmp_path / "agent-quality-gates"
    root.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(stage_mod.StageError, match="not a version tree"):
        stage_mod.swap_root(root=root, target=tree)
    assert stage_mod.current_target(root) == elsewhere


def test_the_tree_holding_the_object_store_is_never_pruned(tmp_path):
    """The migrated tree carries the real `.git`; every other tree is a worktree
    of it and dies with it.

    The sentinel that marks a prunable version tree is `VERSION`, which every
    AQG checkout has — so the migrated tree looked exactly like a version, and
    `keep=0` removed it, after which `git` failed in the live tree. Retention is
    "current plus previous", so on any install this happens on the second update.
    """
    import subprocess
    from scripts.aqg_update import migrate as migrate_mod

    inst = tmp_path / "install"
    (inst / "scripts").mkdir(parents=True)
    (inst / "scripts" / "_aqg_context.sh").write_text("#\n", encoding="utf-8")
    (inst / "VERSION").write_text("1\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(inst), *args], capture_output=True, check=True
        )

    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "T"), ("add", "-A"), ("commit", "-qm", "v1")):
        git(*args)
    (inst / "VERSION").write_text("2\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "v2")
    second = git("rev-parse", "HEAD").stdout.decode().strip()
    git("checkout", "-q", "--detach", "HEAD~1")

    moved = migrate_mod.migrate(inst)
    staged = stage_mod.stage_version(
        repo=inst, commit=second, versions_dir=moved.versions_dir, name=second
    )

    removed = stage_mod.prune_versions(
        versions_dir=moved.versions_dir, keep=0, protected=(), repo=inst, root=None
    )
    assert moved.target.exists(), "the tree holding .git was pruned"
    assert moved.target not in removed
    # And the proof that it mattered: the repository is still a repository. The
    # staged tree is legitimately gone at keep=0 — what must survive is the one
    # every other tree reads through.
    alive = subprocess.run(
        ["git", "-C", str(moved.target), "rev-parse", "HEAD"], capture_output=True
    )
    assert alive.returncode == 0, alive.stderr.decode()[:200]
    assert staged.name in {p.name for p in removed}, "the staged tree was not pruned"


def test_prunability_is_decided_by_where_the_object_store_actually_is(tmp_path):
    """`.git`-is-a-directory was a proxy, and the wrong one in both directions.

    The property that matters is: does deleting this tree destroy the repository
    the other trees read through? git answers it directly —
    `rev-parse --git-common-dir`. A `--separate-git-dir` clone has `.git` as a
    FILE and is safe to remove; a plain clone has it as a directory and is not.
    """
    import subprocess

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "VERSION").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(plain), "init", "-q"], capture_output=True, check=True)
    assert stage_mod._holds_the_object_store(plain) is True

    outside = tmp_path / "elsewhere.git"
    separate = tmp_path / "separate"
    separate.mkdir()
    (separate / "VERSION").write_text("1\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", str(outside), str(separate)],
        capture_output=True, check=True,
    )
    assert (separate / ".git").is_file(), "fixture did not produce the shape under test"
    assert stage_mod._holds_the_object_store(separate) is False, (
        "a tree whose object store lives outside it was treated as the repository"
    )

    not_a_repo = tmp_path / "plainold"
    not_a_repo.mkdir()
    (not_a_repo / "VERSION").write_text("1\n", encoding="utf-8")
    assert stage_mod._holds_the_object_store(not_a_repo) is False
