# aqg-example: GUIDE touchpoint mapping

This file maps every numbered checklist item in
[`docs/SKILL_AUTHORING_GUIDE.md`](../../docs/SKILL_AUTHORING_GUIDE.md) to
the specific line/file in this template that satisfies it. Use it as a
self-check after copying the template into `skills/aqg-<name>/`.

## §1 Skill anatomy

| GUIDE | satisfied by |
|---|---|
| §1.1 frontmatter has only `name` + `description` | `SKILL.md` lines 1-4 |
| §1.2 body < 150 lines | `SKILL.md` total = ~55 lines |
| §1.3 scripts/ is Python stdlib only with self_test | `scripts/example_check.py` (no third-party imports), `scripts/self_test.py` (9 tests) |
| §1.4 exit codes 0 / 1 / 2 / 3 / 70 | `example_check.py` `EXIT_*` constants + `self_test.py` exercises all 5 (default → 0, --fail / --required-file missing → 1, unknown flag / invalid --repo → 2, --bad-config → 3, --crash → 70) |

## §2 Description grammar

| GUIDE | satisfied by |
|---|---|
| §2.1 lead verb | `SKILL.md` description starts with "Demonstrate" |
| §2.2 trigger words from user vocabulary | "engineer learning", "starting point", "copy-pasteable" — words a real new author would use |
| §2.3 boundary clause | "Not for production work; not registered in install/doctor" |
| §2.4 path-portable declaration | "in any repository" |

## §3 AQG_ROOT resolution

| GUIDE | satisfied by |
|---|---|
| §3.1 reuse boilerplate verbatim | `SKILL.md` "How To Run" copies §3.1 snippet exactly |

## §4 Packaging / install / release checklist

This template intentionally **skips** touchpoints 3-11 because it is
not a real skill (it lives in `templates/`, not `skills/`). When you
copy it into `skills/aqg-<name>/`, you must add:

| # | what to add | reference |
|---|---|---|
| 1 | this `SKILL.md` (already done — you copied it) | — |
| 2 | a Claude wrapper at `agent-packs/claude-code/skills/aqg-<name>/SKILL.md` | see `agent-packs/claude-code/skills/aqg-audit-adjudication/SKILL.md` |
| 3 | `scripts/install.sh` `skills` array entry | grep "aqg-startup-preflight" in `scripts/install.sh` |
| 4 | `agent-packs/claude-code/install.sh` `skills` array entry | same file pattern |
| 5 | `scripts/aqg_doctor.py` `CLAUDE_SKILL_NAMES` | grep "aqg-startup-preflight" |
| 6 | `scripts/aqg_doctor.py` `CODEX_SKILL_NAMES` | same line area |
| 7 | `tests/behavior/fixtures/triggers.yaml` ≥5 cases (~70/30 description vs explicit) | see existing per-skill blocks |
| 8 | `skills/aqg-<name>/skill.template.json` `wrapper_generated: true` + `aqg_skill_gen.py regen aqg-<name>` | the committed wrapper is regen→git-status gated (replaced the retired drift-hash baseline, 2026-05-31 §6/§7) |
| 9 | `skills/aqg-<name>/agents/openai.yaml` `interface.display_name: "AQG <Name>"` | without it the Codex picker falls back to the slug and renders `Aqg <Name>`; validator-enforced (`_validate_codex_interface`). grep `display_name` in an existing `agents/openai.yaml` |
| 10 | `tests/behavior/test_aqg_skill_trigger_canary.py` `SKILL_TRIGGER_KEYWORDS` entry: `aqg-<name>` → ≥1 trigger keyword present in the wrapper description | **new skill MUST add an entry** or `test_keyword_map_covers_skill_roster` fails (1:1 roster check). grep `aqg-startup-preflight` in that file |
| 11 | `tests/behavior/test_aqg_security_boundary_canary.py`: `aqg-<name>` in **either** `SECURITY_BOUNDARY_SIGNATURES` (pin a body boundary phrase) **or** `KNOWN_NO_SECURITY_BOUNDARY` (with exemption reason) | **new skill MUST add one entry** or `test_roster_is_partitioned` fails. grep `aqg-startup-preflight` in that file |

After 11 touchpoints land, run:

```bash
python3 scripts/aqg_doctor.py --no-cli      # expect WARN=0 / FAIL=0 (PASS count tracks roster size)
python3 -m pytest tests/behavior/           # all green
python3 templates/example-skill/scripts/self_test.py   # all PASS
```

## §5 Path portability

| GUIDE | satisfied by |
|---|---|
| no hardcoded `~/.codex` / `~/.claude` / `/Users/*` | grep template — none present |
| `--repo` defaults to git root via `git rev-parse --show-toplevel` | `example_check.py` `_resolve_repo()` |
| `--required-file` validates a path under the resolved repo | `example_check.py` body + `self_test.py test_required_file_*` |
| invalid `--repo` raises `ValueError` mapped to exit 2 | `example_check.py main()` boundary + `self_test.py test_invalid_repo_*` |

## §6 Boundary declarations

| GUIDE | satisfied by |
|---|---|
| explicit boundaries section | `SKILL.md` "## Boundaries" |
| no production write / no destructive | `example_check.py` only prints |

## §7 Behavior testing

This template does **not** ship fixture entries (touchpoint 7) — those
go in the real `tests/behavior/fixtures/triggers.yaml` after you copy
the template into `skills/aqg-<name>/`. Per §7.1 of the GUIDE, you need
≥5 cases with a roughly 70/30 description-based vs explicit-invocation
mix. The `scripts/self_test.py` here exercises the §1.4 exit-code
contract (9/9 PASS) which is the unit-level minimum required by §1.3.

## §8 Anti-patterns avoided

| anti-pattern | how this template avoids it |
|---|---|
| #1 inline status enum / decision tree | workflow is one bullet, real logic is in `example_check.py` |
| #2 over-prose | total markdown < 60 lines |
| #3 hardcoded paths | none |
| #4 in-skill enforcement | only prints |
| #5 internal jargon | description uses "starting point", "copy-pasteable" |
| #6 third-party imports | `example_check.py` is stdlib-only |
| #7 frontmatter extras | only `name` + `description` |
| #8 invented resolver | reuses §3.1 boilerplate verbatim |
