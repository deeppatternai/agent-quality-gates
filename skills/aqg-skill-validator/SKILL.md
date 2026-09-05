---
name: aqg-skill-validator
description: Validate an AQG skill's sidecar manifest, SKILL.md frontmatter, boundary section, and cross-cutting registration before commit; surface violations + advisory warnings. Use when authoring a new skill, backfilling a sidecar for a shipped skill, or checking spec drift.
---

# AQG Skill Validator

Use this skill before committing a new or modified AQG skill, to catch sidecar schema gaps, frontmatter drift, missing boundary section, exit-code contract gaps, and unregistered cross-cutting touchpoints (install scripts, doctor tuples, drift test, triggers fixture).

## Workflow

1. Identify the skill name (e.g. `aqg-foo`).
2. Run the validator (see How To Run).
3. Adjudicate output:
   - **PASS**: skill is shippable; advisory warnings (e.g. `audit_mode_required` advisory v1) are non-blocking but worth reviewing.
   - **FAIL with sidecar_violations**: schema problems in `skills/<name>/skill.template.json`; fix the sidecar and re-run.
   - **FAIL with skill_dir_violations**: SKILL.md frontmatter has extra keys, a missing boundary H2 on a non-read-only skill, or entry script docstring is missing the `0/1/2/3/70` exit-code contract. (A missing boundary H2 on a `read-only` skill is an advisory **warning**, not a hard violation — it is surfaced only by the final `--strict` re-run in step 4.)
   - **FAIL with cross_cutting_violations**: skill name not in install scripts × 2 / `aqg_doctor.py` skill-name tuples / `triggers.yaml` (≥5 cases), or the sidecar lacks `wrapper_generated: true` (the wrapper-generation opt-in that replaced the retired drift-hash baseline). Apply the items in `skills/<name>/GENERATED.md` (if present) or hand-patch.
4. After all violations resolved, re-run validator with `--strict` to ensure even advisory warnings are clean before claiming the skill is shippable.

## How To Run

```bash
# Resolve AQG_ROOT via shared helper.
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
python3 "$aqg_root/scripts/aqg_skill_validator.py" <skill-name>
# Or with strict mode (advisory warnings fail too):
python3 "$aqg_root/scripts/aqg_skill_validator.py" <skill-name> --strict
# Or JSON output for tooling:
python3 "$aqg_root/scripts/aqg_skill_validator.py" <skill-name> --json
```

## Boundaries

- **Read-only**: validator never modifies any file under `skills/`, `agent-packs/`, or repo-level `scripts/`. Only reads sidecar + SKILL.md + entry script + cross-cutting files for diagnostics.
- **Forbidden touch**: production paths, secrets, raw private data — none accessed.
- **Owner edges**: do not auto-fix violations. Validator reports; author + Owner adjudicate. Sidecar schema or boundary policy changes require ADR.

## Reporting Shape

Validator emits 3 violation categories + 1 warning list:

- `sidecar_violations`: schema-level (missing fields, wrong enum, cross-field invariants)
- `skill_dir_violations`: SKILL.md / entry script disk-shape problems
- `cross_cutting_violations`: registration gaps (install / doctor / drift / triggers)
- `warnings`: advisory only (`audit_mode_required` v1, `_aqg_context.sh` source pre-PR-3b3, **wrapper out-of-sync** with source — `--strict` fails to match the CI regen→git-status gate)

Exit code: `0` clean / `1` violations (or warnings under `--strict`) / `2` usage error / `3` config error (reserved) / `70` internal error (reserved). Codes `3` and `70` are reserved for future use and not currently emitted, but the validator enforces this full `0/1/2/3/70` contract on the skills it checks.

## When NOT to use

- **During edit-time** of an in-progress skill that has not yet been wired up — wait until skeleton + sidecar exist.
- **For non-AQG markdown files** with `aqg-*` prefix in the name; this skill expects the AQG sidecar manifest contract.
- **As a replacement for behavior tests** — validator checks disk shape + schema; runtime trigger discrimination still needs the nightly real-claude harness.
