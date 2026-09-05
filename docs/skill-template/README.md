# AQG Skill Template — Sidecar Manifest + Validator

**Status**: B-1 (validator-first); B-2 generator + B-3 standalone skill follow.


## What this is

A **machine-readable companion** to `docs/SKILL_AUTHORING_GUIDE.md`. The GUIDE is human-facing prose; this directory documents the **sidecar manifest** that lives at `skills/<name>/skill.template.json` and is validated by `scripts/aqg_skill_validator.py`.

SKILL.md frontmatter remains canonical-fixed `name` + `description` only per GUIDE §1; the sidecar holds everything else (boundary class, audit mode, fixture metadata, exit-code contract, etc.) so an AI agent can generate or validate skills from a structured input rather than walking through 430 lines of GUIDE prose.

## Sample

`templates/skill_template_example.json` — a complete annotated example. Copy it to `skills/<your-skill>/skill.template.json` and fill in values.

## Schema (v1)

| field | type | required | notes |
|---|---|---|---|
| `skill_template_schema` | int | yes | accepted: `{1}` (Q4 default — contract version field) |
| `name` | str | yes | must match `^aqg-[a-z0-9][a-z0-9-]{0,40}$`; mirrors SKILL.md frontmatter |
| `description` | str | yes | mirrors SKILL.md frontmatter; no length cap (per a1 audit #6) |
| `version` | str | optional | sidecar-only; skill-internal evolution |
| `primary_trigger_keywords` | list[str] | optional | cross-checked against fixture prompts (keyword bridge defense) |
| `boundary_class` | str enum | yes | `read-only` / `writes-evidence` / `writes-code` |
| `audit_mode_required` | str enum | optional (advisory) | `fast` / `standard` / `deep` / `none`; **Q6 advisory v1** — emits warn, doesn't block |
| `trigger_grammar` | obj | yes | `{ keywords: [str], sentence_patterns: [str] }` |
| `entry_script` | str | yes | project-relative path under `scripts/` or `skills/<name>/scripts/` |
| `cli_contract` | obj | yes | exit-code map; must include `0/1/2/3/70` per GUIDE §1.4 |
| `output_shape` | str enum | yes | `markdown_table` / `json` / `ledger_block` / `multi_section_report` |
| `reads_paths` | list[str] | yes | project-relative POSIX paths (no `/`, `~`, `..`, `\\`) |
| `writes_paths` | list[str] | yes | default empty for `read-only` class |
| `forbidden_paths` | list[str] | yes | production / secrets / raw private data |
| `aqg_agent_gating` | bool | yes | true = fail-closed under `AQG_AGENT={codex,claude,human-opt-in,ci-rerun}` |
| `owner_only_actions` | list[str] | yes | destructive / irreversible actions needing Owner confirmation |
| `cases` | list[obj] | yes | ≥5; `id` / `style` / `prompt` / `expected_skill` / `skill_file_ref`; ≥3 description-based + ≥1 explicit |
| `self_test_entrypoint` | str | yes | typically `skills/<name>/scripts/self_test.py` |
| `fixed_before_next_task_required_when_findings` | bool | yes | echoes handoff manifest invariant 4 |
| `numeric_values_quoted` | bool | yes | default true (defends YAML auto-typing per PR-5fg/5h) |

`_doc` is reserved for sample manifests (mirrors handoff manifest pattern); validator ignores it.

## Validator

```bash
# Validate a shipped skill
python3 scripts/aqg_skill_validator.py aqg-startup-preflight

# Strict mode (warnings fail too)
python3 scripts/aqg_skill_validator.py aqg-startup-preflight --strict

# JSON output for tooling
python3 scripts/aqg_skill_validator.py aqg-startup-preflight --json
```

Exit codes:
- `0` valid (advisory warnings allowed unless `--strict`)
- `1` violations found
- `2` usage error / sidecar missing / skill dir missing

### Always-on checks

- Sidecar parses + schema valid (delegates to `_skill_template_schema.check_skill_template`)
- `sidecar.name` matches skill directory name
- SKILL.md frontmatter has **only** `name` + `description` (no extras)
- SKILL.md frontmatter values match sidecar
- SKILL.md body contains a boundary-style H2 (loose match: `boundary` / `stop` / `scope` / `owner`)
- Skill name registered in `scripts/aqg_doctor.py` skill-name tuples
- Skill name registered in `scripts/install.sh` AND `agent-packs/claude-code/install.sh`
- Sidecar `wrapper_generated: true` + a regenerated wrapper (`aqg_skill_gen.py regen`) — the committed wrapper is regen→git-status gated (replaced the retired drift-hash baseline)
- `tests/behavior/fixtures/triggers.yaml` has ≥5 cases with `expected_skill: <name>`
- Each `cases[i].expected_skill` matches `sidecar.name`
- Entry script documents the `0/1/2/3/70` exit-code contract

### Conditional / advisory

- `audit_mode_required` → emits warn (Q6 advisory v1; revisit if drift)
- `_aqg_context.sh` source pattern in SKILL.md → emits warn (advisory until PR-3b3 mass migration ships)

## Boundary

- AQG **own**: schema definition (`_skill_template_schema.py`), validator (`aqg_skill_validator.py`), sample (`templates/skill_template_example.json`)
- AQG **NOT own**: actual skill semantic content (author judgment), trigger discrimination correctness (nightly real-claude harness), cross-client behavior (Continuity Drill)
- Validator emits diagnostics; **does not** auto-fix or modify any file

## Roadmap

- **B-1 (this drop)**: schema + validator CLI + tests + sample sidecar + doctor entries
- **B-2 (next)**: generator CLI (`scripts/aqg_skill_gen.py`) — input sidecar → emit skill skeleton + sidecar + Claude wrapper + `GENERATED.md` cross-cutting checklist
- **B-3 (after B-2)**: `skills/aqg-skill-validator/` standalone skill (eats own dogfood; richer LLM-readable error guidance per Q3 default `doctor + skill (both)`)

## Open questions deferred

Per ADR §6, six questions resolved at Owner approval (2026-05-04):

| Q | Default | Note |
|---|---|---|
| Q1 | JSON sidecar format | Defends int/str auto-typing PR-5fg/5h bug class |
| Q2 | Python script generator (`scripts/aqg_skill_gen.py`) | Consistent with rest of AQG tooling |
| Q3 | Doctor check + standalone skill (both) | Doctor in B-1; skill in B-3 |
| Q4 | Include contract version field (`skill_template_schema: 1`) | Matches handoff manifest pattern |
| Q5 | In-tree direct emit (skills/`<name>`/) | Generator scope (B-2) |
| Q6 | `audit_mode_required` advisory v1 | Validator emits warn; revisit if author drift |
