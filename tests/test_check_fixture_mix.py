"""Negative-test battery for scripts/check_fixture_mix.py (the self-computing gate).

Per ADR 2026-05-26-self-computing-fixture-gate-a1.md + audit bc37c580 D3: build a
synthetic repo in tmp_path and assert each invariant fires cleanly (no traceback)
and the happy path passes. Covers the edges the old hardcoded gate missed:
whole-skill deletion (I6), orphan (I2), forgot-fixture (I1), malformed shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from check_fixture_mix import check_fixture_mix, _roster, main  # noqa: E402

_OMIT = object()


def make_repo(tmp_path: Path, skills: dict[str, tuple[int, int]], *,
              schema: object = 1, extractor_schema: int = 1,
              skill_md: bool = True, extra_cases=(),
              manifest: "list[str] | None" = None,
              write_manifest: bool = True) -> Path:
    """Write a synthetic AQG repo. skills = {name: (n_desc, n_explicit)}.
    schema=_OMIT drops the schema_version key entirely.

    NB: every skill here is LEGACY (no skill.template.json written → not
    managed), so each generated case carries non-null M2 hash fields to satisfy
    the I7 `managed ⟺ M2-null` invariant (a legacy skill must keep M2 hashes
    populated). The managed/null shape is exercised by make_repo_m2 below.
    """
    (tmp_path / "VERSION").write_text("0.0.0\n")
    sk = tmp_path / "skills"
    for name in skills:
        d = sk / name
        d.mkdir(parents=True)
        if skill_md:
            (d / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n")
    cases: list = []
    for name, (nd, ne) in skills.items():
        for style, n in (("description_based", nd), ("explicit_invocation", ne)):
            for _ in range(n):
                cases.append({
                    "style": style,
                    "expected_skill": name,
                    "description_sha256_first8": "deadbeef",
                    "trigger_section_sha256_first8": "cafef00d",
                })
    cases += list(extra_cases)
    fixture: dict = {"cases": cases}
    if schema is not _OMIT:
        fixture["schema_version"] = schema
    fx = tmp_path / "tests" / "behavior" / "fixtures"
    fx.mkdir(parents=True)
    (fx / "triggers.yaml").write_text(yaml.safe_dump(fixture))
    (tmp_path / "tests" / "behavior" / "extractor.py").write_text(
        f"SCHEMA_VERSION = {extractor_schema}\n"
    )
    # skills.list = the version-controlled roster manifest the I6 floor anchors
    # to. Default: mirror the on-disk skills (happy path, floor == roster). Pass
    # `manifest=[...]` to declare a different baseline (e.g. a skill whose dir was
    # deleted but whose line remains), or write_manifest=False to omit it.
    if write_manifest:
        names = sorted(skills) if manifest is None else manifest
        (tmp_path / "skills.list").write_text(
            "".join(f"{n}\n" for n in names), encoding="utf-8"
        )
    return tmp_path


# 11 skills @ (4 desc + 1 explicit) → 55 cases, ratio 0.80 (within [0.70,0.85])
VALID = {f"aqg-skill-{i:02d}": (4, 1) for i in range(11)}


def test_valid_repo_passes(tmp_path):
    root = make_repo(tmp_path, VALID)
    assert check_fixture_mix(root) == []


def test_adding_12th_skill_needs_no_gate_edit(tmp_path):
    skills = dict(VALID)
    skills["aqg-skill-11"] = (4, 1)  # roster 12
    # manifest still declares only the 11-skill baseline → adding is zero-touch
    root = make_repo(tmp_path, skills, manifest=sorted(VALID))
    assert check_fixture_mix(root) == []


# ---- I1: per-skill >= 5 -----------------------------------------------------


def test_i1_skill_under_five_cases(tmp_path):
    skills = dict(VALID)
    skills["aqg-skill-00"] = (3, 1)  # 4 cases < 5
    root = make_repo(tmp_path, skills)
    v = check_fixture_mix(root)
    assert any("< 5 fixture cases" in m and "aqg-skill-00" in m for m in v)


# ---- I2: orphan -------------------------------------------------------------


def test_i2_orphan_fixture_skill(tmp_path):
    root = make_repo(tmp_path, VALID, extra_cases=[
        {"style": "description_based", "expected_skill": "aqg-ghost"},
    ])
    v = check_fixture_mix(root)
    assert any("unregistered skill" in m and "aqg-ghost" in m for m in v)


# ---- I6: roster floor (whole-skill deletion) -------------------------------


def test_i6_deletion_of_declared_skill(tmp_path):
    # manifest declares aqg-skill-10 but its dir was removed with its cases — the
    # deletion the dynamic roster alone cannot see; name-subset names it (audit D1)
    skills = {f"aqg-skill-{i:02d}": (4, 1) for i in range(10)}
    manifest = [f"aqg-skill-{i:02d}" for i in range(11)]
    root = make_repo(tmp_path, skills, manifest=manifest)
    v = check_fixture_mix(root)
    assert any("no skills/<name>/ dir" in m and "aqg-skill-10" in m for m in v)


def test_i6_zero_touch_add_leaves_no_deletion_slack(tmp_path):
    # a skill added zero-touch (roster > declared) must NOT create room to delete a
    # declared skill unnoticed — the count-floor blind spot name-subset closes
    # (audit ae59c0a9 f-A). declared = 00..10 (11); roster = 01..11 (skill-00 gone,
    # skill-11 added) → count 11 == 11 but skill-00 is missing → must still fire.
    skills = {f"aqg-skill-{i:02d}": (4, 1) for i in range(1, 12)}
    manifest = [f"aqg-skill-{i:02d}" for i in range(11)]
    root = make_repo(tmp_path, skills, manifest=manifest)
    v = check_fixture_mix(root)
    assert any("no skills/<name>/ dir" in m and "aqg-skill-00" in m for m in v)


def test_i6_manifest_missing_fails_closed(tmp_path):
    # skills.list absent → nothing anchors the guard → must fire (fail-closed);
    # a silently-disabled deletion guard is the exact self-defeat WS-3 prevents
    root = make_repo(tmp_path, VALID, write_manifest=False)
    v = check_fixture_mix(root)
    assert any("skills.list" in m and "missing" in m for m in v)


def test_i6_duplicate_manifest_line_harmless(tmp_path):
    # a duplicated skills.list line used to silently lower a count floor; under
    # name-subset it is harmless — the name still must exist (audit ae59c0a9 f-A)
    root = make_repo(tmp_path, VALID, manifest=sorted(VALID) + ["aqg-skill-00"])
    assert check_fixture_mix(root) == []


# ---- shape (fail-closed, no traceback) -------------------------------------


def test_shape_empty_cases_no_crash(tmp_path):
    root = make_repo(tmp_path, {})  # zero skills, zero cases
    v = check_fixture_mix(root)
    assert any("non-empty list" in m for m in v)  # clean, not ZeroDivisionError


def test_shape_missing_expected_skill_no_keyerror(tmp_path):
    root = make_repo(tmp_path, VALID, extra_cases=[{"style": "description_based"}])
    v = check_fixture_mix(root)
    assert any("expected_skill must be a non-empty string" in m for m in v)


def test_shape_non_dict_case(tmp_path):
    root = make_repo(tmp_path, VALID, extra_cases=["not-a-mapping"])
    v = check_fixture_mix(root)
    assert any("must be a mapping" in m for m in v)


def test_shape_bad_style(tmp_path):
    root = make_repo(tmp_path, VALID, extra_cases=[
        {"style": "weird", "expected_skill": "aqg-skill-00"},
    ])
    v = check_fixture_mix(root)
    assert any("unrecognized" in m for m in v)


# ---- I3: distribution ratio ------------------------------------------------


def test_i3_ratio_skew_all_explicit(tmp_path):
    skills = {f"aqg-skill-{i:02d}": (0, 5) for i in range(11)}  # ratio 0.0
    root = make_repo(tmp_path, skills)
    v = check_fixture_mix(root)
    assert any("ratio" in m and "outside" in m for m in v)


# ---- I5: schema_version ----------------------------------------------------


def test_i5_schema_mismatch(tmp_path):
    root = make_repo(tmp_path, VALID, schema=1, extractor_schema=2)
    v = check_fixture_mix(root)
    assert any("schema_version mismatch" in m for m in v)


# ---- roster: SKILL.md gating -----------------------------------------------


def test_roster_excludes_dir_without_skill_md(tmp_path):
    root = make_repo(tmp_path, VALID)
    (root / "skills" / "aqg-partial").mkdir()  # no SKILL.md
    roster = _roster(root)
    assert "aqg-partial" not in roster
    assert len(roster) == 11  # partial dir does not inflate the roster


def test_partial_dir_does_not_satisfy_floor(tmp_path):
    # 10 real skills + 1 SKILL.md-less dir → roster still 10; manifest declares
    # aqg-skill-10 → I6 must fire (the partial dir must not paper over a deletion)
    skills = {f"aqg-skill-{i:02d}": (4, 1) for i in range(10)}
    manifest = [f"aqg-skill-{i:02d}" for i in range(11)]
    root = make_repo(tmp_path, skills, manifest=manifest)
    (root / "skills" / "aqg-partial").mkdir()
    v = check_fixture_mix(root)
    assert any("no skills/<name>/ dir" in m and "aqg-skill-10" in m for m in v)


# ===== code-audit 41965f2c regression tests =====


# ---- P1: non-scalar style must not traceback (fail-closed) -----------------


def test_p1_unhashable_style_no_traceback(tmp_path):
    root = make_repo(tmp_path, VALID, extra_cases=[
        {"style": [], "expected_skill": "aqg-skill-00"},  # unhashable
    ])
    v = check_fixture_mix(root)  # must NOT raise TypeError
    assert any("unrecognized" in m for m in v)


def test_p1_dict_style_no_traceback(tmp_path):
    root = make_repo(tmp_path, VALID, extra_cases=[
        {"style": {"k": "v"}, "expected_skill": "aqg-skill-00"},
    ])
    v = check_fixture_mix(root)
    assert any("unrecognized" in m for m in v)


# ---- P2: schema_version strict (no int() coercion false-pass) --------------


def test_p2_schema_bool_rejected(tmp_path):
    # extractor SCHEMA_VERSION=1; fixture `true` → int(True)==1 would false-pass
    root = make_repo(tmp_path, VALID, schema=True, extractor_schema=1)
    v = check_fixture_mix(root)
    assert any("integer" in m for m in v)


def test_p2_schema_float_rejected(tmp_path):
    # int(1.9)==1 would false-pass against extractor 1
    root = make_repo(tmp_path, VALID, schema=1.9, extractor_schema=1)
    v = check_fixture_mix(root)
    assert any("integer" in m for m in v)


def test_p2_schema_missing_rejected(tmp_path):
    root = make_repo(tmp_path, VALID, schema=_OMIT)
    v = check_fixture_mix(root)
    assert any("integer" in m or "schema_version" in m for m in v)


# ---- P3: I3 upper bound (all description_based) -----------------------------


def test_p3_ratio_skew_all_desc(tmp_path):
    skills = {f"aqg-skill-{i:02d}": (5, 0) for i in range(11)}  # ratio 1.0
    root = make_repo(tmp_path, skills)
    v = check_fixture_mix(root)
    assert any("ratio" in m and "outside" in m for m in v)


# ---- C1 + C2: main() exit-code contract ------------------------------------


def test_main_exit_0_valid(tmp_path):
    root = make_repo(tmp_path, VALID)
    assert main(["--repo", str(root)]) == 0


def test_main_exit_1_violation(tmp_path):
    skills = dict(VALID)
    skills["aqg-skill-00"] = (3, 1)  # under 5 → invariant violation
    root = make_repo(tmp_path, skills)
    assert main(["--repo", str(root)]) == 1


def test_main_exit_2_missing_fixture(tmp_path):
    root = make_repo(tmp_path, VALID)
    (root / "tests" / "behavior" / "fixtures" / "triggers.yaml").unlink()
    assert main(["--repo", str(root)]) == 2


def test_main_exit_2_missing_extractor(tmp_path):
    root = make_repo(tmp_path, VALID)
    (root / "tests" / "behavior" / "extractor.py").unlink()
    assert main(["--repo", str(root)]) == 2


# ===== managed ⟺ M2-null invariant (spec 2026-05-31 §6/§8, Task 7) =====
#
# A skill that migrated to the SKILL.md-from-source overlay (is_managed_skill)
# must have BOTH M2 hash fields (description_sha256_first8 /
# trigger_section_sha256_first8) null/absent on every one of its triggers.yaml
# cases — the wrapper is regen-generated + git-status-gated, so the per-case
# hash baseline no longer applies. A legacy skill must keep them present
# (non-null). Both sides use the ONE shared is_managed_skill helper, so this
# auto-detects a skill that left M1 (test_drift.py) but not M2 (or vice versa).


def make_repo_m2(
    tmp_path: Path,
    skills: dict,
    *,
    managed: dict | None = None,
    m2_present: dict | None = None,
) -> Path:
    """Synthetic repo with per-skill sidecars + M2 hash fields on the cases.

    skills    = {name: (n_desc, n_explicit)}
    managed   = {name: bool}  — write wrapper_generated:true when True
    m2_present= {name: bool}  — when True, cases carry non-null M2 hashes;
                                when False, cases carry M2 null fields.
    Defaults: not managed, M2 present (the legacy shape).
    """
    managed = managed or {}
    m2_present = m2_present or {}
    (tmp_path / "VERSION").write_text("0.0.0\n")
    sk = tmp_path / "skills"
    for name in skills:
        d = sk / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n")
        sidecar = {"name": name}
        if managed.get(name, False):
            sidecar["wrapper_generated"] = True
        (d / "skill.template.json").write_text(json.dumps(sidecar))
    cases: list = []
    for name, (nd, ne) in skills.items():
        present = m2_present.get(name, True)
        for style, n in (("description_based", nd), ("explicit_invocation", ne)):
            for _ in range(n):
                case = {"style": style, "expected_skill": name}
                if present:
                    case["description_sha256_first8"] = "deadbeef"
                    case["trigger_section_sha256_first8"] = "cafef00d"
                else:
                    case["description_sha256_first8"] = None
                    case["trigger_section_sha256_first8"] = None
                cases.append(case)
    fixture = {"schema_version": 1, "cases": cases}
    fx = tmp_path / "tests" / "behavior" / "fixtures"
    fx.mkdir(parents=True)
    (fx / "triggers.yaml").write_text(yaml.safe_dump(fixture))
    (tmp_path / "tests" / "behavior" / "extractor.py").write_text("SCHEMA_VERSION = 1\n")
    # skills.list manifest mirrors the roster (I6 floor anchor — see make_repo)
    (tmp_path / "skills.list").write_text(
        "".join(f"{n}\n" for n in sorted(skills)), encoding="utf-8"
    )
    return tmp_path


# 11 legacy skills with M2 present → the established happy path.
LEGACY11 = {f"aqg-skill-{i:02d}": (4, 1) for i in range(11)}


def test_m2_legacy_with_hashes_passes(tmp_path):
    root = make_repo_m2(tmp_path, LEGACY11)  # all legacy, M2 present
    assert check_fixture_mix(root) == []


def test_m2_managed_with_null_hashes_passes(tmp_path):
    skills = dict(LEGACY11)
    skills["aqg-skill-11"] = (4, 1)  # 12th skill, managed, M2 null
    root = make_repo_m2(
        tmp_path, skills,
        managed={"aqg-skill-11": True},
        m2_present={"aqg-skill-11": False},
    )
    assert check_fixture_mix(root) == []


def test_m2_managed_with_nonnull_hashes_violates(tmp_path):
    """A managed skill whose cases still carry non-null M2 hashes → violation."""
    skills = dict(LEGACY11)
    skills["aqg-skill-11"] = (4, 1)
    root = make_repo_m2(
        tmp_path, skills,
        managed={"aqg-skill-11": True},
        m2_present={"aqg-skill-11": True},  # WRONG: managed but hashes present
    )
    v = check_fixture_mix(root)
    assert any("aqg-skill-11" in m and "managed" in m.lower() for m in v), v


def test_m2_legacy_with_null_hashes_violates(tmp_path):
    """A legacy skill whose cases have null M2 hashes → violation (must keep
    them until it migrates)."""
    root = make_repo_m2(
        tmp_path, LEGACY11,
        m2_present={"aqg-skill-00": False},  # WRONG: legacy but nulled
    )
    v = check_fixture_mix(root)
    assert any("aqg-skill-00" in m and "legacy" in m.lower() for m in v), v


def test_m2_managed_partial_null_violates(tmp_path):
    """Managed skill with ONE hash field nulled but the other non-null → still a
    violation (both must be null for a managed skill)."""
    skills = dict(LEGACY11)
    skills["aqg-skill-11"] = (5, 0)
    root = make_repo_m2(
        tmp_path, skills,
        managed={"aqg-skill-11": True},
        m2_present={"aqg-skill-11": False},
    )
    # Corrupt: set ONE case's description hash back to non-null.
    fx = root / "tests" / "behavior" / "fixtures" / "triggers.yaml"
    data = yaml.safe_load(fx.read_text())
    for c in data["cases"]:
        if c["expected_skill"] == "aqg-skill-11":
            c["description_sha256_first8"] = "deadbeef"  # one field non-null
            break
    fx.write_text(yaml.safe_dump(data))
    v = check_fixture_mix(root)
    assert any("aqg-skill-11" in m for m in v), v


def test_m2_malformed_sidecar_fails_closed(tmp_path):
    """A roster skill whose sidecar is PRESENT but malformed JSON must fail
    CLOSED: the gate cannot tell whether the skill is managed, so it must emit a
    violation (not silently read it as legacy, which could mask a managed skill's
    stale non-null M2 hashes) — and must not raise (audit 9c540100 f3)."""
    root = make_repo_m2(tmp_path, LEGACY11)
    (root / "skills" / "aqg-skill-00" / "skill.template.json").write_text("{ not json")
    v = check_fixture_mix(root)  # must NOT raise
    assert any("aqg-skill-00" in m and "unparseable" in m.lower() for m in v), v


def test_m2_missing_sidecar_reads_as_legacy(tmp_path):
    """An ABSENT sidecar (no file) is treated as legacy, not a violation: a
    pre-overlay skill has no sidecar, and the validator separately requires one.
    With M2 hashes present (the legacy shape), the gate stays clean."""
    root = make_repo_m2(tmp_path, LEGACY11)
    sc = root / "skills" / "aqg-skill-00" / "skill.template.json"
    if sc.is_file():
        sc.unlink()
    v = check_fixture_mix(root)  # must NOT raise
    assert not any("aqg-skill-00" in m for m in v), v
