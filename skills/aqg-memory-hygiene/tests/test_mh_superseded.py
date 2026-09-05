"""Core-level deterministic tests for superseded_by resolution (audit round-1
fixes). Exercises resolve_superseded_by / classify_slug directly so the repo:
hardening and the active-node requirement are pinned without YAML round-trip
ambiguity.

- gemini-f1: a bare slug must point to another ACTIVE node (ADR §0 ②).
- gpt-f2: repo: rejects empty / absolute / `..` traversal / drive-letter / backslash.
- gpt-f3: classify_slug consults the dir index first (name/stem collision).
"""

from __future__ import annotations

import _mh_core as core
from _mh_fixtures import compliant_meta, write_node


def _superseded_meta(**over) -> dict:
    meta = compliant_meta(status="superseded")
    meta.update(
        {"superseded_by": "null", "superseded_reason": "moved", "superseded_date": "2020-02-01"}
    )
    meta.update(over)
    return meta


def _idx_and(tmp_path, target_filename):
    nodes = [core.parse_node(p) for p in core.iter_node_files(tmp_path)]
    idx = core.build_dir_index(nodes, core.index_files(tmp_path))
    target = next(n for n in nodes if n.path.name == target_filename)
    return idx, target


def _lone_node(tmp_path):
    """A single node + its (self-only) index — enough for the repo: branch which
    never consults siblings."""
    write_node(tmp_path, "self.md", metadata=_superseded_meta())
    return _idx_and(tmp_path, "self.md")


# ---- repo: hardening (gpt-f2) --------------------------------------------


def test_repo_pointer_valid_relative_warns(tmp_path):
    idx, node = _lone_node(tmp_path)
    assert core.resolve_superseded_by("repo:docs/foo.md", node, idx).kind == "warn"


def test_repo_pointer_empty_rejected(tmp_path):
    idx, node = _lone_node(tmp_path)
    res = core.resolve_superseded_by("repo:", node, idx)
    assert res.kind == "reject" and "empty" in res.detail.lower()


def test_repo_pointer_absolute_rejected(tmp_path):
    idx, node = _lone_node(tmp_path)
    assert core.resolve_superseded_by("repo:/etc/passwd", node, idx).kind == "reject"


def test_repo_pointer_dotdot_traversal_rejected(tmp_path):
    idx, node = _lone_node(tmp_path)
    assert core.resolve_superseded_by("repo:../../etc/passwd", node, idx).kind == "reject"


def test_repo_pointer_backslash_traversal_rejected(tmp_path):
    idx, node = _lone_node(tmp_path)
    # POSIX hosts treat `\` as a literal char; normalize before the traversal check
    assert core.resolve_superseded_by("repo:..\\secret", node, idx).kind == "reject"


def test_repo_pointer_drive_letter_rejected(tmp_path):
    idx, node = _lone_node(tmp_path)
    assert core.resolve_superseded_by("repo:C:/secret", node, idx).kind == "reject"


def test_repo_pointer_internal_dotdot_rejected(tmp_path):
    # gpt-f3: an internal `..` that normalizes back inside the dir (foo/../bar →
    # bar) used to be warn-accepted. The contract rejects ANY `..` segment, not
    # just net escapes — so these must reject, not warn.
    idx, node = _lone_node(tmp_path)
    assert core.resolve_superseded_by("repo:foo/../bar.md", node, idx).kind == "reject"
    assert core.resolve_superseded_by("repo:a/b/../../c.md", node, idx).kind == "reject"


def test_repo_pointer_single_dot_still_warns(tmp_path):
    # a `.` segment is a harmless no-op (not traversal) — must stay warn-only,
    # confirming the hardening rejects `..` specifically, not all dotted segments.
    idx, node = _lone_node(tmp_path)
    assert core.resolve_superseded_by("repo:a/./b.md", node, idx).kind == "warn"


def test_repo_pointer_backslash_internal_traversal_rejected(tmp_path):
    # gpt-f2 + gpt-f3: backslashes are normalized to `/` BEFORE the segment check,
    # so a Windows-style internal `..` is rejected on POSIX hosts too.
    idx, node = _lone_node(tmp_path)
    assert core.resolve_superseded_by("repo:foo\\..\\bar.md", node, idx).kind == "reject"


def test_repo_pointer_empty_segment_warns(tmp_path):
    # an empty path segment (`a//b`) is not a traversal — documented warn-only
    # (repo: existence is not verified; only `..`/absolute/drive forms hard-fail).
    idx, node = _lone_node(tmp_path)
    assert core.resolve_superseded_by("repo:a//b.md", node, idx).kind == "warn"


# ---- slug must point to ANOTHER ACTIVE node (gemini-f1) -------------------


def test_slug_to_active_node_ok(tmp_path):
    write_node(tmp_path, "feedback_new.md", metadata=compliant_meta())  # active
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_by="feedback_new"))
    idx, node = _idx_and(tmp_path, "feedback_old.md")
    assert core.resolve_superseded_by("feedback_new", node, idx).kind == "ok"


def test_slug_to_superseded_node_rejected(tmp_path):
    # target is itself retired → cannot be a supersede target (must be ACTIVE)
    write_node(tmp_path, "feedback_mid.md", metadata=_superseded_meta())  # superseded
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_by="feedback_mid"))
    idx, node = _idx_and(tmp_path, "feedback_old.md")
    res = core.resolve_superseded_by("feedback_mid", node, idx)
    assert res.kind == "reject" and "active" in res.detail.lower()


def test_slug_self_rejected(tmp_path):
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_by="feedback_old"))
    idx, node = _idx_and(tmp_path, "feedback_old.md")
    assert core.resolve_superseded_by("feedback_old", node, idx).kind == "reject"


def test_slug_dangling_rejected(tmp_path):
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_by="ghost"))
    idx, node = _idx_and(tmp_path, "feedback_old.md")
    assert core.resolve_superseded_by("ghost", node, idx).kind == "reject"


# ---- classify_slug index-first precedence on name/stem collision (gpt-f3) -


def test_classify_slug_resolves_to_stem_owner_on_name_collision(tmp_path):
    # B owns stem `feedback_new`; A aliases it via name:. A slug `feedback_new`
    # from A must resolve to B (the stem owner), NOT be misread as A-self.
    write_node(tmp_path, "feedback_new.md", metadata=compliant_meta())  # B, active
    write_node(
        tmp_path,
        "feedback_old.md",
        name="feedback_new",
        metadata=_superseded_meta(superseded_by="feedback_new"),
    )  # A
    idx, node_a = _idx_and(tmp_path, "feedback_old.md")
    entry, kind = core.classify_slug("feedback_new", node_a, idx)
    assert kind == "node", "slug must resolve to the sibling stem-owner, not self"
    assert entry is not None and entry.path.name == "feedback_new.md"
    # end-to-end: resolves to B (active) → ok
    assert core.resolve_superseded_by("feedback_new", node_a, idx).kind == "ok"
