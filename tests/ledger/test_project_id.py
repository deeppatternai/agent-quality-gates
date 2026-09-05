"""project_id normalization tests — the ledger aggregation key MUST be stable
(same repo → same id) and identical on both the AQG and EAF sides."""
from __future__ import annotations

import subprocess

import pytest

from ledger.project_id import (
    normalize_remote_url,
    project_id_from_remote_or_path,
    project_id_from_repo,
)


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/Example-Org/Example-Repo.git", "example-org/example-repo"),
    ("https://github.com/Example-Org/Example-Repo", "example-org/example-repo"),
    ("git@github.com:Example-Org/Example-Repo.git", "example-org/example-repo"),
    ("git@github.com:Example-Org/Example-Repo", "example-org/example-repo"),
    ("ssh://git@github.com/Example-Org/Example-Repo.git", "example-org/example-repo"),
    ("https://gitlab.com/group/proj.git", "group/proj"),
    ("HTTPS://GitHub.com/A/B", "a/b"),
    ("https://github.com/Example-Org/Example-Repo/", "example-org/example-repo"),
])
def test_normalize_remote_url_ok(url, expected):
    assert normalize_remote_url(url) == expected


@pytest.mark.parametrize("bad", ["", "   ", "not-a-url", None, 42,
                                 "https://github.com/onlyowner"])
def test_normalize_remote_url_invalid(bad):
    assert normalize_remote_url(bad) is None


def test_case_variant_clones_collapse():
    """A repo cloned with different casing must map to ONE project_id."""
    a = normalize_remote_url("git@github.com:Example-Org/Example-Repo.git")
    b = normalize_remote_url("https://github.com/example-org/example-repo")
    assert a == b == "example-org/example-repo"


def test_remote_preferred_over_path():
    assert project_id_from_remote_or_path("git@github.com:A/B.git", "/x/y") == "a/b"


def test_path_hash_stable_and_sensitive():
    a = project_id_from_remote_or_path(None, "/Users/x/proj")
    assert a == project_id_from_remote_or_path(None, "/Users/x/proj")  # stable
    assert a.startswith("local:")
    assert project_id_from_remote_or_path(None, "/Users/x/other") != a  # path-sensitive
    assert "/Users/x" not in a  # path not leaked, only hashed


def test_from_repo_with_remote(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "git@github.com:Foo/Bar.git"], check=True)
    assert project_id_from_repo(str(tmp_path)) == "foo/bar"


def test_from_repo_no_remote_stable(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    pid = project_id_from_repo(str(tmp_path))
    assert pid.startswith("local:")
    assert pid == project_id_from_repo(str(tmp_path))  # stable across calls


def test_from_repo_not_a_repo_falls_back(tmp_path):
    # A non-repo dir: no remote, rev-parse fails → falls back to path hash.
    pid = project_id_from_repo(str(tmp_path / "nope"))
    assert pid.startswith("local:")


# --- audit fd14014a regressions ---


@pytest.mark.parametrize("url,expected", [
    ("http://host:8080/owner/repo.git", "owner/repo"),       # port ignored
    ("ssh://git@github.com:22/o/r.git", "o/r"),              # ssh port ignored
    ("git@gitlab.com:org/sub/repo.git", "org/sub/repo"),     # subgroup → full path
    ("https://gitlab.com/org/sub/repo", "org/sub/repo"),
])
def test_url_port_and_subgroup(url, expected):
    """gemini-f1: ports are ignored; subgroups keep their full path (no collision)."""
    assert normalize_remote_url(url) == expected


def test_distinct_non_git_dirs_distinct_ids(tmp_path, monkeypatch):
    """gpt-f4: the '.' fallback must resolve to an abspath, not collapse unrelated
    cwds to one local:<hash>."""
    d1 = tmp_path / "p1"
    d2 = tmp_path / "p2"
    d1.mkdir()
    d2.mkdir()
    monkeypatch.chdir(d1)
    id1 = project_id_from_repo(".")
    monkeypatch.chdir(d2)
    id2 = project_id_from_repo(".")
    assert id1.startswith("local:") and id2.startswith("local:")
    assert id1 != id2
