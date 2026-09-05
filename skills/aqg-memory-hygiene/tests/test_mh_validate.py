"""Behavior tests for `aqg_memory_hygiene.py validate` (ADR §4.3 schema gate,
§4.5 exit semantics, §6 acceptance #1).

Each test asserts a real behavioral outcome (exit code + the SPECIFIC violation
text), not the shape of a data structure. CLI-level tests use extreme dates
(2020 = safely past, 2099 = safely future) so they are deterministic on any run
date; boundary/threshold edges live in test_mh_staleness.py / core unit tests.
"""

from __future__ import annotations

from _mh_fixtures import compliant_meta, run_cli, write_index, write_node


def _validate(d) -> tuple[int, str]:
    return run_cli(["validate", "--memory-dir", str(d)])


# ---- compliant + index exclusion ----------------------------------------


def test_validate_compliant_node_exits_zero(tmp_path):
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta())
    rc, text = _validate(tmp_path)
    assert rc == 0, f"compliant node should pass; got rc={rc}\n{text}"


def test_validate_excludes_index_files(tmp_path):
    # round2 convergent finding: a standard dir always has MEMORY.md (no node
    # frontmatter) — the strict gate must skip it or it fatal-fails everywhere.
    write_index(tmp_path, "MEMORY.md")
    write_index(tmp_path, "README.md")
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta())
    rc, text = _validate(tmp_path)
    assert rc == 0, f"index files must be excluded; got rc={rc}\n{text}"
    assert "MEMORY.md" not in text or "skip" in text.lower()


def test_validate_empty_dir_exits_zero(tmp_path):
    rc, text = _validate(tmp_path)
    assert rc == 0, f"empty dir is not a violation; got rc={rc}\n{text}"


def test_validate_missing_dir_exits_zero_with_note(tmp_path):
    missing = tmp_path / "does-not-exist"
    rc, text = _validate(missing)
    assert rc == 0, f"missing dir → 0 + note (no target); got rc={rc}\n{text}"
    assert "no memory dir" in text.lower()


# ---- required fields / enums / flat-vs-nested ----------------------------


def test_validate_missing_required_field_rejected(tmp_path):
    meta = compliant_meta()
    del meta["status"]
    write_node(tmp_path, "feedback_a.md", metadata=meta)
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "status" in text


def test_validate_bad_type_enum_rejected(tmp_path):
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(type="bogus"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "type" in text


def test_validate_bad_status_enum_rejected(tmp_path):
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(status="bogus"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "status" in text


def test_validate_bad_volatility_enum_rejected(tmp_path):
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(volatility="kinda"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "volatility" in text


def test_validate_flat_type_no_metadata_rejected(tmp_path):
    # flat `type:` at top level instead of nested under metadata: → reject (rule ②)
    raw = (
        "---\n"
        "name: feedback_flat\n"
        "type: feedback\n"
        "status: active\n"
        "volatility: durable\n"
        "last_verified: 2020-01-01\n"
        "---\n\nbody\n"
    )
    write_node(tmp_path, "feedback_flat.md", raw=raw)
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "metadata" in text.lower()


def test_validate_node_type_residue_rejected(tmp_path):
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(node_type="memory"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "node_type" in text


def test_validate_node_type_nested_under_metadata_rejected(tmp_path):
    # gpt-f2 / gemini-f2: rule ⑤ forbids node_type ANYWHERE; one nested inside a
    # deeper mapping under metadata slipped past the old two-level check.
    raw = (
        "---\nname: nm\ndescription: d\nmetadata:\n  type: feedback\n"
        "  status: active\n  volatility: durable\n  last_verified: 2020-01-01\n"
        "  tags:\n    node_type: legacy\n---\n\nbody\n"
    )
    write_node(tmp_path, "nm.md", raw=raw)
    rc, text = _validate(tmp_path)
    assert rc != 0, "node_type nested under metadata must be rejected"
    assert "node_type" in text


def test_validate_node_type_nested_top_level_rejected(tmp_path):
    raw = (
        "---\nname: nt\ndescription: d\npayload:\n  node_type: legacy\n"
        "metadata:\n  type: feedback\n  status: active\n  volatility: durable\n"
        "  last_verified: 2020-01-01\n---\n\nbody\n"
    )
    write_node(tmp_path, "nt.md", raw=raw)
    rc, text = _validate(tmp_path)
    assert rc != 0, "node_type nested in a top-level mapping must be rejected"
    assert "node_type" in text


def test_contains_key_deep_walks_mappings_and_sequences():
    import _mh_core as core
    # a sequence holding a mapping with the forbidden key → found (walks lists)
    assert core._contains_key_deep({"a": [{"node_type": "x"}]}, "node_type") is True
    # absent at every depth → not found
    assert core._contains_key_deep({"a": {"b": {"c": 1}}}, "node_type") is False


def test_validate_cyclic_frontmatter_does_not_crash(tmp_path):
    # audit commit-gate f1: PyYAML anchors/aliases build a recursive structure
    # (metadata: &m { ... self: *m }); the recursive node_type walk must NOT
    # RecursionError out to exit 70 — never-crash contract (ADR §4.5).
    raw = (
        "---\nname: cyc\ndescription: d\nmetadata: &m\n  type: feedback\n"
        "  status: active\n  volatility: durable\n  last_verified: 2020-01-01\n"
        "  self: *m\n---\n\nbody\n"
    )
    write_node(tmp_path, "cyc.md", raw=raw)
    rc, _ = _validate(tmp_path)
    assert rc != 70, "cyclic frontmatter must not crash validate (exit 70)"
    assert rc == 0  # cyclic but schema-compliant (the extra `self` key is not forbidden)


def test_validate_node_type_reachable_in_cycle_still_rejected(tmp_path):
    # the cycle-safe walk must still flag a node_type reachable before the back-edge
    raw = (
        "---\nname: cyc2\ndescription: d\nmetadata: &m\n  type: feedback\n"
        "  status: active\n  volatility: durable\n  last_verified: 2020-01-01\n"
        "  node_type: legacy\n  self: *m\n---\n\nbody\n"
    )
    write_node(tmp_path, "cyc2.md", raw=raw)
    rc, text = _validate(tmp_path)
    assert rc == 1 and "node_type" in text


def test_contains_key_deep_cycle_safe():
    import _mh_core as core
    # a directly self-referential dict must terminate without RecursionError
    d: dict = {"a": 1}
    d["self"] = d
    assert core._contains_key_deep(d, "node_type") is False
    d["node_type"] = "x"
    assert core._contains_key_deep(d, "node_type") is True


# ---- frontmatter presence / parseability ---------------------------------


def test_validate_no_frontmatter_rejected(tmp_path):
    write_node(tmp_path, "feedback_a.md", raw="# just a heading\n\nno frontmatter here\n")
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "frontmatter" in text.lower()


def test_validate_unparseable_frontmatter_rejected(tmp_path):
    # malformed YAML inside the fence (bad indentation / stray colon structure)
    raw = "---\nname: x\nmetadata:\n  type: feedback\n :::broken: [\n---\n\nbody\n"
    write_node(tmp_path, "feedback_a.md", raw=raw)
    rc, text = _validate(tmp_path)
    assert rc != 0


def test_validate_invalid_calendar_date_reported_not_crash(tmp_path):
    # audit round2 gpt-f1: PyYAML raises ValueError (NOT yaml.YAMLError) when
    # constructing an invalid-but-timestamp-shaped scalar like 2026-02-30. It must
    # surface as a parse-error violation (rc 1), never an exit-70 crash.
    raw = (
        "---\nname: bad\ndescription: d\nmetadata:\n  type: feedback\n"
        "  status: active\n  volatility: durable\n  last_verified: 2026-02-30\n---\n\nbody\n"
    )
    write_node(tmp_path, "bad.md", raw=raw)
    rc, text = _validate(tmp_path)
    assert rc == 1, f"invalid calendar date must be a violation, not a crash (rc={rc})"
    assert "bad.md" in text


def test_validate_invalid_month_date_reported_not_crash(tmp_path):
    raw = (
        "---\nname: bad\ndescription: d\nmetadata:\n  type: feedback\n"
        "  status: active\n  volatility: durable\n  last_verified: 2026-13-01\n---\n\nbody\n"
    )
    write_node(tmp_path, "bad.md", raw=raw)
    rc, _ = _validate(tmp_path)
    assert rc == 1


# ---- last_verified date hygiene ------------------------------------------


def test_validate_future_last_verified_rejected(tmp_path):
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(last_verified="2099-12-31"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "last_verified" in text


def test_validate_malformed_last_verified_rejected(tmp_path):
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(last_verified="not-a-date"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "last_verified" in text


def test_validate_last_verified_timestamp_rejected(tmp_path):
    # audit gpt-f1: YAML parses an unquoted timestamp to datetime; the schema is
    # date-only, so a timestamp must be rejected (not silently truncated).
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(last_verified="2026-01-01T12:00:00"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "last_verified" in text


# ---- superseded integrity ------------------------------------------------


def _superseded_meta(**over) -> dict:
    meta = compliant_meta(status="superseded")
    meta.update(
        {
            "superseded_by": "null",
            "superseded_reason": "moved to repo",
            "superseded_date": "2020-02-01",
        }
    )
    meta.update(over)
    return meta


def test_validate_superseded_null_pointer_ok(tmp_path):
    # pure retirement: status superseded + all 3 fields + superseded_by null
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta())
    rc, text = _validate(tmp_path)
    assert rc == 0, f"clean superseded(null) should pass; got rc={rc}\n{text}"


def test_validate_superseded_missing_fields_rejected(tmp_path):
    meta = compliant_meta(status="superseded")  # no superseded_* trio
    write_node(tmp_path, "feedback_old.md", metadata=meta)
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "superseded" in text.lower()


def test_validate_active_with_superseded_field_rejected(tmp_path):
    # status active but carries a superseded_* field → reject (rule ⑦)
    meta = compliant_meta(status="active", superseded_reason="oops")
    write_node(tmp_path, "feedback_a.md", metadata=meta)
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "superseded" in text.lower() and "active" in text.lower()


def test_validate_superseded_date_future_rejected(tmp_path):
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_date="2099-01-01"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "superseded_date" in text


def test_validate_superseded_date_timestamp_rejected(tmp_path):
    # audit gpt-f1: a timestamp scalar is not the date-only schema form
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_date="2020-02-01T08:00:00"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "superseded_date" in text


def test_validate_superseded_by_superseded_node_rejected(tmp_path):
    # gemini-f1 end-to-end: superseded_by slug must point to an ACTIVE node; a
    # pointer to an already-superseded node is rejected (no retired-node chains).
    write_node(tmp_path, "feedback_mid.md", metadata=_superseded_meta(superseded_by="null"))
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_by="feedback_mid"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "active" in text.lower()


# ---- superseded_by slug resolution ---------------------------------------


def test_validate_superseded_by_slug_to_active_node_ok(tmp_path):
    write_node(tmp_path, "feedback_new.md", metadata=compliant_meta())
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_by="feedback_new"))
    rc, text = _validate(tmp_path)
    assert rc == 0, f"slug → existing sibling node should pass; got rc={rc}\n{text}"


def test_validate_superseded_by_slug_self_rejected(tmp_path):
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_by="feedback_old"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "self" in text.lower()


def test_validate_superseded_by_slug_to_index_rejected(tmp_path):
    write_index(tmp_path, "MEMORY.md")
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_by="MEMORY"))
    rc, text = _validate(tmp_path)
    assert rc != 0


def test_validate_superseded_by_slug_dangling_rejected(tmp_path):
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta(superseded_by="no_such_node"))
    rc, text = _validate(tmp_path)
    assert rc != 0
    assert "dangling" in text.lower() or "no_such_node" in text


# ---- superseded_by repo: pointer (warn-only dangling, §9 Decision 3) ------


def test_validate_repo_pointer_dangling_warns_not_fails(tmp_path):
    write_node(
        tmp_path,
        "feedback_old.md",
        metadata=_superseded_meta(superseded_by="repo:docs/foo.md"),
    )
    rc, text = _validate(tmp_path)
    assert rc == 0, f"repo: dangling is warn-only, not hard-fail; got rc={rc}\n{text}"
    assert "warn" in text.lower() or "repo:" in text


def test_validate_repo_pointer_traversal_rejected(tmp_path):
    write_node(
        tmp_path,
        "feedback_old.md",
        metadata=_superseded_meta(superseded_by="repo:../../etc/passwd"),
    )
    rc, text = _validate(tmp_path)
    assert rc != 0, "repo: with .. traversal must be rejected"
    assert "traversal" in text.lower() or ".." in text


def test_validate_repo_pointer_internal_dotdot_rejected(tmp_path):
    # gpt-f3 end-to-end: an internal `..` segment (foo/../bar) is a hard violation,
    # not a warn — validate must exit non-zero.
    write_node(
        tmp_path,
        "feedback_old.md",
        metadata=_superseded_meta(superseded_by="repo:foo/../bar.md"),
    )
    rc, text = _validate(tmp_path)
    assert rc != 0, "repo: internal .. segment must be rejected (gpt-f3)"
    assert "traversal" in text.lower() or ".." in text
