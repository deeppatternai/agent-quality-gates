"""project_id normalization — one target_repo = one project (DesignSpec §5.5).

Shared by the AQG inbox/hook side AND the EAF exporter so the SAME repo always
maps to the SAME project_id (the ledger aggregation primary key). Pure-stdlib.

Resolution: prefer the git remote origin slug (lowercased path, so case-variant
clones collapse to one id; subgroups kept as full path so they don't collide);
fall back to a stable hash of the repo's RESOLVED absolute top-level path
('local:<hash>') when there is no remote. The pure helpers
(normalize_remote_url / project_id_from_remote_or_path) are split out from the
git-subprocess wrapper so both sides unit-test the algorithm identically.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

# A usable lowercased slug: >=2 '/'-separated [a-z0-9._-] segments. normalize_
# remote_url validates its output against this so a remote with unsafe chars
# (space / control / backslash) falls back to the local-path hash rather than
# producing an invalid project_id (the on-disk partition key). Mirrors
# conformance._PROJECT_SLUG_RE (case-folded here; the field check allows both).
_SLUG_RE = re.compile(r"^[a-z0-9._-]+(?:/[a-z0-9._-]+)+$")


def normalize_remote_url(url) -> "str | None":
    """git remote URL → lowercased 'owner/repo' (or full 'group/sub/repo' for
    subgroups), or None if unparseable.

    Handles url-form (https/ssh/git, with optional user@ and :port) via urlparse
    and scp-form (git@host:path) by splitting on the first ':'. A port in the
    netloc is ignored (audit gemini-f1); subgroups keep their full path so two
    distinct repos never collide.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if "://" in url:
        path = urlparse(url).path
    elif ":" in url:
        # scp-like: [user@]host:path — path is everything after the first ':'.
        path = url.split(":", 1)[1]
    else:
        return None  # not a recognizable git remote (no scheme, no scp ':')

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [seg for seg in path.split("/") if seg]
    if len(parts) < 2:
        return None  # need at least owner/repo
    # A '.'/'..' segment would make the derived project_id a path-traversal slug
    # (project_id is the on-disk partition key) — refuse it (audit b5381a7d
    # gpt-f1 + o3-f2; workflow A2).
    if any(seg in (".", "..") for seg in parts):
        return None
    slug = "/".join(parts).lower()
    # defense-in-depth: a stray unsafe char (space / control / backslash) → fall
    # back to the local-path hash rather than an invalid/escaping slug.
    if not _SLUG_RE.match(slug):
        return None
    return slug


def project_id_from_remote_or_path(remote_url, toplevel_path) -> str:
    """project_id from a remote URL (preferred) or a stable hash of the repo
    top-level path. The path is RESOLVED to an absolute path before hashing so a
    relative arg like '.' can't collapse unrelated dirs to one id (audit gpt-f4).
    Same inputs → same id (idempotent)."""
    slug = normalize_remote_url(remote_url) if remote_url else None
    if slug:
        return slug
    abspath = str(Path(str(toplevel_path)).expanduser().resolve())
    digest = hashlib.sha256(abspath.encode("utf-8")).hexdigest()[:16]
    return f"local:{digest}"


def _git(args, repo_path) -> "str | None":
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def project_id_from_repo(repo_path: str = ".") -> str:
    """Resolve project_id for a working dir: read remote.origin.url +
    rev-parse --show-toplevel via git; fall back to the resolved path's hash if
    it is not a git repo or has no remote."""
    remote = _git(["config", "--get", "remote.origin.url"], repo_path)
    toplevel = _git(["rev-parse", "--show-toplevel"], repo_path) or str(repo_path)
    return project_id_from_remote_or_path(remote, toplevel)
