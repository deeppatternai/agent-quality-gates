#!/usr/bin/env python3
"""Self-computing behavior-test fixture gate.

Replaces the old hardcoded case counts (`len(cases)!=55 / desc!=42 / exp!=13`) — a forgettable,
CI-only touchpoint — with invariants computed from the skill roster. Adding a
skill needs NO edit here; only a deliberate skill REMOVAL lowers the I6 floor.

Invariants (all fail-closed; a malformed fixture yields a clean ::error::, never
a traceback — audit bc37c580 D2):
  shape : cases is a non-empty list; each case is a dict with a non-empty str
          `expected_skill` + a recognized `style`
  I1    : every roster skill has >= MIN_CASES_PER_SKILL cases
  I2    : every fixture expected_skill is in the roster (no orphan)
  I3    : desc / total ratio within [DESC_RATIO_MIN, DESC_RATIO_MAX]
  I4    : every case style is recognized (covered by shape)
  I5    : fixture schema_version present + equals extractor.py SCHEMA_VERSION
  I6    : declared ⊆ roster — every skill named in the version-controlled
          skills.list manifest must still have a live skills/<name>/ dir (guards
          whole-skill deletion; removing a skill must also delete its skills.list
          line per GUIDE §4.4). Fail-closed if skills.list is absent/empty.
  I7    : managed ⟺ M2-null — a skill whose sidecar is_managed_skill (SKILL.md-
          from-source overlay) must have both M2 hash fields
          (description_sha256_first8 / trigger_section_sha256_first8) null on
          every case; a legacy skill must keep them present. Same shared
          is_managed_skill helper as regen / validator (spec 2026-05-31 §6/§8).

Roster = `skills/aqg-*/` directories that contain a SKILL.md (excludes stray /
partial dirs; templates/example-skill is outside skills/).

Exit codes:
  0 : all invariants hold
  1 : one or more invariant violations
  2 : usage error (repo root / fixture / extractor not found)
  3 : reserved (not emitted)
  70: reserved (not emitted)

stdlib + PyYAML (already a behavior-test dependency).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Shared migration predicate (overlay design, spec 2026-05-31 §4/§6). The SAME
# helper drives regen / validator / this gate, so "managed" can never be defined
# three different ways. A managed skill ⟺ its M2 hash fields are null (I7 below).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _aqg_wrapper_overlay import is_managed_skill  # noqa: E402

SIDECAR_FILENAME = "skill.template.json"

# I6 deletion guard — the version-controlled skills.list roster manifest, an
# INDEPENDENT committed reference (NOT a live `ls skills/aqg-*`): every declared
# name must still have a dir, so a whole-skill deletion cannot pass unnoticed.
# Adding a skill needs NO manifest edit (a new dir not yet declared is fine).
# REMOVING a skill is an Owner-approved major change (SKILL_AUTHORING_GUIDE §4.4)
# and MUST also delete its skills.list line — that reviewed diff authorizes it.
SKILLS_MANIFEST = "skills.list"

MIN_CASES_PER_SKILL = 5
DESC_RATIO_MIN = 0.70
DESC_RATIO_MAX = 0.85
VALID_STYLES = frozenset({"description_based", "explicit_invocation"})

_SCHEMA_VERSION_RE = re.compile(
    r"^\s*SCHEMA_VERSION\s*=\s*[\"']?(\d+)[\"']?\s*(?:#.*)?$", re.MULTILINE
)


def _find_repo_root(start: Path | None = None) -> Path | None:
    p = (start or Path(__file__).resolve().parent).resolve()
    for _ in range(10):
        if (p / "VERSION").is_file() and (p / "skills").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _roster(repo_root: Path) -> set[str]:
    """Registered skills = skills/aqg-*/ dirs that contain a SKILL.md."""
    skills_dir = repo_root / "skills"
    if not skills_dir.is_dir():
        return set()
    return {
        p.name
        for p in skills_dir.glob("aqg-*")
        if p.is_dir() and (p / "SKILL.md").is_file()
    }


def _declared_skills(repo_root: Path) -> set[str]:
    """Skill names declared in the version-controlled roster manifest
    (skills.list): one name per line, '#' comments and blank lines ignored.
    The I6 deletion guard anchors here — an INDEPENDENT committed reference —
    not the live skills/ dir it guards (see SKILLS_MANIFEST)."""
    manifest = repo_root / SKILLS_MANIFEST
    if not manifest.is_file():
        return set()
    declared: set[str] = set()
    # utf-8-sig: strip a stray BOM so a leading "# comment" line 1 is still seen
    # as a comment (else it parses as a phantom declared name — audit ae59c0a9 f2).
    for line in manifest.read_text(encoding="utf-8-sig").splitlines():
        name = line.strip()
        if name and not name.startswith("#"):
            declared.add(name)
    return declared


def _load_skill_sidecar(repo_root: Path, skill: str):
    """Load skills/<skill>/skill.template.json → (sidecar_dict, error_msg).

    Returns:
      - (None, None)  — sidecar absent: treated as LEGACY. A pre-overlay skill
        has no sidecar; the validator separately requires one, so a genuinely
        missing sidecar on a real roster skill is caught there, not here. (This
        also keeps the test helper's no-sidecar legacy skills valid.)
      - (None, error) — sidecar PRESENT but unparseable / not a JSON object:
        fail-closed VIOLATION (audit 9c540100 f3). A corrupt sidecar could be
        hiding `wrapper_generated: true`, which would otherwise be read as legacy
        and let a managed skill keep stale non-null M2 hashes undetected.
      - (dict, None)  — a valid sidecar object.

    Never raises (the module's fail-closed contract: a clean ::error::, not a
    traceback). `is_managed_skill(None)` is False, so the caller can pass the
    first element straight through.
    """
    sidecar_path = repo_root / "skills" / skill / SIDECAR_FILENAME
    if not sidecar_path.is_file():
        return None, None  # absent → legacy
    try:
        loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"sidecar unparseable (skills/{skill}/{SIDECAR_FILENAME}): {exc}"
    if not isinstance(loaded, dict):
        return None, (
            f"sidecar root must be a JSON object: skills/{skill}/{SIDECAR_FILENAME}"
        )
    return loaded, None


def _m2_is_null(case: dict, fieldname: str) -> bool:
    """True if an M2 hash field is null/absent on a case (the 'skipped' shape).

    `null` in YAML loads as None; an absent key is also treated as null. Any
    other value (a hash string, etc.) counts as present (non-null).
    """
    return case.get(fieldname) is None


def check_fixture_mix(repo_root: Path) -> list[str]:
    """Return a list of invariant-violation messages (empty = all hold).

    Fail-closed: malformed shape is reported as a violation, never raised.
    """
    import yaml  # local import: keeps usage error (exit 2) distinct if missing

    violations: list[str] = []
    fixture_path = repo_root / "tests" / "behavior" / "fixtures" / "triggers.yaml"
    extractor_path = repo_root / "tests" / "behavior" / "extractor.py"

    # ---- load + shape (fail-closed) ----
    try:
        fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [f"cannot parse {fixture_path}: {exc}"]
    if not isinstance(fixture, dict):
        return [f"{fixture_path}: top-level must be a mapping"]

    # Strict integer (audit 41965f2c P2): reject None / bool / float / str so a
    # malformed value cannot int()-coerce into a false-pass (int(1.9)==1,
    # int(True)==1). `type(x) is int` excludes bool (a subclass of int).
    fsv = fixture.get("schema_version")
    if type(fsv) is not int:
        violations.append(
            f"fixture schema_version must be an integer (got {fsv!r})"
        )

    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        violations.append("fixture 'cases' must be a non-empty list")
        return violations  # nothing else is safe to compute

    skills_in_cases: list[str] = []
    for i, c in enumerate(cases):
        if not isinstance(c, dict):
            violations.append(f"cases[{i}] must be a mapping")
            continue
        es = c.get("expected_skill")
        if not isinstance(es, str) or not es:
            violations.append(f"cases[{i}].expected_skill must be a non-empty string")
        else:
            skills_in_cases.append(es)
        # isinstance guard BEFORE set membership: a non-scalar style (list/dict)
        # is unhashable and would raise TypeError on `in` (audit 41965f2c P1).
        style = c.get("style")
        if not isinstance(style, str) or style not in VALID_STYLES:
            violations.append(
                f"cases[{i}].style {style!r} unrecognized "
                f"(one of {sorted(VALID_STYLES)})"
            )
    if violations:
        return violations  # shape broken — counts below would be unreliable

    by_skill = Counter(skills_in_cases)
    roster = _roster(repo_root)
    declared = _declared_skills(repo_root)

    # ---- I6: every declared skill must still exist (whole-skill deletion guard) ----
    # Name-subset (declared ⊆ roster), NOT a bare count floor: a count-only floor
    # leaks deletion slack once a skill is added zero-touch (roster > declared, so a
    # later deletion still clears the count), is blind to a typo'd manifest name, and
    # `set` dedup of a duplicated line would quietly lower it. Checking each declared
    # NAME still has a dir catches all three (audit ae59c0a9, convergent 4/4).
    if not declared:
        # fail-closed: no manifest ⇒ nothing anchors the guard; silently skipping
        # it is the exact self-defeat WS-3 prevents.
        violations.append(
            f"skills.list roster manifest missing or empty at "
            f"{repo_root / SKILLS_MANIFEST} — required to anchor the I6 guard"
        )
    else:
        missing = sorted(declared - roster)
        if missing:
            violations.append(
                f"skills.list declares skill(s) with no skills/<name>/ dir: "
                f"{missing} (skill removed? delete its skills.list line too — an "
                f"Owner-approved major change per GUIDE §4.4; typo? fix the name)"
            )

    # ---- I1: every roster skill has >= MIN_CASES_PER_SKILL cases ----
    under = sorted(s for s in roster if by_skill.get(s, 0) < MIN_CASES_PER_SKILL)
    if under:
        violations.append(
            f"skills with < {MIN_CASES_PER_SKILL} fixture cases: "
            + ", ".join(f"{s}({by_skill.get(s, 0)})" for s in under)
        )

    # ---- I2: no orphan fixture skills ----
    orphan = sorted(set(by_skill) - roster)
    if orphan:
        violations.append(f"fixture cases for unregistered skill(s): {orphan}")

    # ---- I3: distribution ratio (total > 0 guaranteed above) ----
    total = len(cases)
    desc = sum(1 for c in cases if c.get("style") == "description_based")
    ratio = desc / total
    if not (DESC_RATIO_MIN <= ratio <= DESC_RATIO_MAX):
        violations.append(
            f"description_based ratio {ratio:.3f} outside "
            f"[{DESC_RATIO_MIN}, {DESC_RATIO_MAX}] ({desc}/{total})"
        )

    # ---- I5: schema_version consistency ----
    try:
        extractor_text = extractor_path.read_text(encoding="utf-8")
    except OSError as exc:
        violations.append(f"cannot read extractor {extractor_path}: {exc}")
        return violations
    m = _SCHEMA_VERSION_RE.search(extractor_text)
    if not m:
        violations.append("cannot find SCHEMA_VERSION integer in extractor.py")
    elif type(fsv) is int and fsv != int(m.group(1)):
        # non-int fsv already flagged above; only compare when it is a real int
        violations.append(
            f"schema_version mismatch: extractor={m.group(1)} vs fixture={fsv}"
        )

    # ---- I7: managed ⟺ M2-null (spec 2026-05-31 §6/§8) ----
    # For each roster skill: if it is managed (SKILL.md-from-source overlay) then
    # EVERY one of its triggers.yaml cases must have BOTH M2 hash fields null /
    # absent (its wrapper is regen-generated + git-status-gated, so the per-case
    # hash baseline no longer applies). If it is legacy, both fields must be
    # present (non-null) on every case until it migrates. Uses the one shared
    # is_managed_skill helper so this can never disagree with regen / validator.
    _M2_FIELDS = ("description_sha256_first8", "trigger_section_sha256_first8")
    cases_by_skill: dict = {}
    for c in cases:
        if isinstance(c, dict):
            es = c.get("expected_skill")
            if isinstance(es, str):
                cases_by_skill.setdefault(es, []).append(c)
    for skill in sorted(roster):
        skill_cases = cases_by_skill.get(skill, [])
        if not skill_cases:
            continue  # I1 already flags a roster skill with no cases
        sidecar, sidecar_err = _load_skill_sidecar(repo_root, skill)
        if sidecar_err is not None:
            violations.append(f"I7: {sidecar_err}")
            continue
        managed = is_managed_skill(sidecar)
        for c in skill_cases:
            nulls = [_m2_is_null(c, f) for f in _M2_FIELDS]
            cid = c.get("id", "<no-id>")
            if managed and not all(nulls):
                present = [f for f, n in zip(_M2_FIELDS, nulls) if not n]
                violations.append(
                    f"managed skill {skill!r} case {cid!r} must have M2 hash "
                    f"fields null (it is generated + git-status-gated), but "
                    f"these are non-null: {present}"
                )
            elif not managed and any(nulls):
                missing = [f for f, n in zip(_M2_FIELDS, nulls) if n]
                violations.append(
                    f"legacy skill {skill!r} case {cid!r} must keep M2 hash "
                    f"fields populated until it migrates, but these are "
                    f"null/absent: {missing}"
                )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_fixture_mix",
        description="Self-computing behavior-test fixture gate (replaces hardcoded counts).",
    )
    parser.add_argument(
        "--repo", default=None,
        help="repo root (default: walk up from this script to find VERSION + skills/)",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve() if args.repo else _find_repo_root()
    if repo_root is None or not (repo_root / "skills").is_dir():
        print("::error::cannot resolve AQG repo root (need VERSION + skills/)", file=sys.stderr)
        return 2

    # Usage-error (exit 2) preflight: a missing fixture / extractor is an infra
    # setup problem, distinct from an invariant violation (exit 1) — audit
    # 41965f2c C1. Keeps CI from reporting a setup failure as a fixture failure.
    for rel in ("tests/behavior/fixtures/triggers.yaml", "tests/behavior/extractor.py"):
        if not (repo_root / rel).is_file():
            print(f"::error::missing required file: {repo_root / rel}", file=sys.stderr)
            return 2

    violations = check_fixture_mix(repo_root)
    if violations:
        for v in violations:
            print(f"::error::{v}")
        return 1

    roster = _roster(repo_root)
    print(f"OK: {len(roster)} skills, fixture invariants hold (self-computed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
