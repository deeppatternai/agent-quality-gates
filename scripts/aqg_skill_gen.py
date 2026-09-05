#!/usr/bin/env python3
"""Skill template generator (Wave 4+ machine-template B-2).

Implements the §4.1 spec (Owner accepted "B as the full default" 2026-05-04).

Input: sidecar JSON manifest at `skills/<name>/skill.template.json` shape.
Output (in-tree direct emit per Q5 default):
  - skills/<name>/SKILL.md (frontmatter = name + description only)
  - skills/<name>/scripts/<entry>.py (skeleton with exit-code contract docstring)
  - skills/<name>/scripts/self_test.py (minimal self-test skeleton)
  - skills/<name>/skill.template.json (sidecar persisted)
  - skills/<name>/agents/openai.yaml (Codex interface — display_name required)
  - skills/<name>/GENERATED.md (checklist for cross-cutting registration)
  - agent-packs/claude-code/skills/<name>/SKILL.md (Claude wrapper)

Generator does NOT directly write the cross-cutting registration files
(install scripts × 2, doctor tuples, triggers.yaml, trigger canary).
Those are listed in `skills/<name>/GENERATED.md` for the LLM/Owner to
apply manually because each touchpoint is a surgical edit on a
hand-curated file (per ADR §4.1 a2 audit #1 fix).

CLI:
    # legacy scaffold (one-shot brand-new-skill bootstrap; unchanged):
    python3 scripts/aqg_skill_gen.py <sidecar.json>
    python3 scripts/aqg_skill_gen.py <sidecar.json> --force    # overwrite
    python3 scripts/aqg_skill_gen.py <sidecar.json> --dry-run  # preview only

    # wrapper regeneration (SKILL.md-from-source overlay, spec 2026-05-31 §4/§5):
    python3 scripts/aqg_skill_gen.py regen <skill>             # write one wrapper
    python3 scripts/aqg_skill_gen.py regen --all               # write all managed
    python3 scripts/aqg_skill_gen.py regen --check <skill>     # diff-only, no write
    python3 scripts/aqg_skill_gen.py regen --check --all       # diff-only, all managed
    python3 scripts/aqg_skill_gen.py regen --check --orphans   # assert no orphan wrappers

Exit codes:
    0: success
    1: schema or write failure / regen drift or orphan detected
    2: usage / sidecar load error
    3: file exists (without --force)
    70: internal error

No third-party dependencies, stdlib only (the generated skill is also stdlib only).
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# The wrapper-overlay core lives in a sibling module (pure, unit-tested). Import
# is_managed_skill + build_wrapper through it so the migration predicate is the
# ONE shared helper across regen / validator / check_fixture_mix (spec §4).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _aqg_wrapper_overlay import build_wrapper, is_managed_skill  # noqa: E402


EXIT_OK = 0
EXIT_SCHEMA_OR_WRITE = 1
EXIT_USAGE = 2
EXIT_FILE_EXISTS = 3
EXIT_INTERNAL = 70

SIDECAR_FILENAME = "skill.template.json"

# D8: single source of truth for the cross-cutting registration touchpoint count.
# _render_generated_md emits exactly this many `## N.` sections; the GENERATED.md
# heading + the CLI "next steps" line read it so a stale hardcoded digit can no
# longer drift from the actual checklist (audit found doc said "6", body had 7).
CROSS_CUTTING_TOUCHPOINTS = 7


# ===== Rendering safety helpers (D1: injection-proof generated artifacts) =====


def _quoted_scalar(value: str) -> str:
    """Render *value* as a double-quoted scalar valid as BOTH a YAML 1.1/1.2
    flow scalar AND a Python string literal (json.dumps output is a subset of
    each grammar).

    A description that contains ':' / newlines / quotes / '#' / a leading YAML
    indicator can no longer break the SKILL.md frontmatter or inject a second
    key, and the same encoder is reused for the generated python argparse /
    self-test string literals. aqg_skill_validator parses frontmatter with
    yaml.safe_load, which round-trips this back to the original string for the
    sidecar-equality check.

    ensure_ascii=False (audit f1): a non-BMP code point (emoji / astral) would
    otherwise be emitted as a JSON surrogate PAIR (\\udXXX\\udYYY); PyYAML
    safe_load keeps the two surrogate code points (breaking the round-trip
    equality) and a generated python literal carrying surrogates raises
    UnicodeEncodeError when argparse prints --help. Emitting raw UTF-8 (every
    generated file is written encoding='utf-8') round-trips faithfully.
    """
    return json.dumps(value, ensure_ascii=False)


def _escape_for_triple_double(body: str) -> str:
    """Escape *body* so it can be embedded inside a generated \"\"\"...\"\"\"
    docstring without prematurely closing it or producing invalid source.

    Order matters: escape backslashes first (so neither an author backslash nor
    the ones we add can pair with a following char), strip C0 controls except
    newline/tab (a NUL can't appear in source), then escape EVERY double-quote.
    In a triple-double-quoted literal an escaped \\" is a literal quote that can
    never start or extend the closing delimiter, so \"\"\" -> \\"\\"\\" is inert
    and a trailing quote/backslash can't merge with the closing \"\"\".
    """
    body = body.replace("\\", "\\\\")
    body = "".join(ch for ch in body if ch in "\n\t" or ch >= " ")
    body = body.replace('"', '\\"')
    return body


# ===== Repo root resolution =====


def _find_repo_root(start: Path | None = None) -> Path:
    if start is None:
        start = Path(__file__).resolve().parent
    p = start.resolve()
    for _ in range(10):
        if (p / "VERSION").is_file() and (p / "scripts").is_dir() and (p / "templates").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    raise RuntimeError(
        f"cannot find AQG repo root from {start}; expected VERSION + scripts/ + templates/"
    )


# ===== Sidecar loader =====


def _load_sidecar(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise RuntimeError(
            f"sidecar root must be JSON object, got {type(loaded).__name__}"
        )
    return loaded


# ===== Renderers =====


def _render_skill_md(sidecar: dict[str, Any]) -> str:
    """Render skills/<name>/SKILL.md.

    Frontmatter: ONLY name + description (per GUIDE §1 + ADR §3.5).
    Body sections: heading + Trigger / How To Run / Boundaries / Owner Edges
    skeletons. LLM/author fills semantic content; generator only emits
    scaffolding + the AQG_ROOT resolution `source ... _aqg_context.sh` line.
    """
    name = sidecar["name"]
    description = sidecar["description"]
    entry_script = sidecar["entry_script"]
    boundary_class = sidecar.get("boundary_class", "read-only")
    aqg_agent_gating = sidecar.get("aqg_agent_gating", False)
    triggers = sidecar.get("trigger_grammar", {})
    keywords = triggers.get("keywords", []) if isinstance(triggers, dict) else []
    sentence_patterns = triggers.get("sentence_patterns", []) if isinstance(triggers, dict) else []

    # entry_script may live under skills/<name>/scripts/ or scripts/<entry>.py
    # When under skills/<name>/scripts/, How To Run uses CLAUDE_SKILL_DIR-aware path
    if entry_script.startswith(f"skills/{name}/"):
        entry_rel_to_skill = entry_script[len(f"skills/{name}/"):]
    else:
        entry_rel_to_skill = None  # entry lives in repo-level scripts/

    # AQG_AGENT gating example block (only for non-read-only)
    gating_block = ""
    if aqg_agent_gating:
        gating_block = (
            "\n\n## AQG_AGENT gating\n\n"
            "Set `AQG_AGENT` to one of `{codex, claude, human-opt-in, ci-rerun}` "
            "before invoking; humans without `AQG_AGENT` get transparent pass.\n\n"
            "```bash\n"
            "AQG_AGENT=codex git commit -m \"...\"\n"
            "```\n"
        )

    # How To Run block
    if entry_rel_to_skill:
        run_invoke = (
            f'python3 "$aqg_root/skills/{name}/{entry_rel_to_skill}" '
            f'--mode <X>'
        )
    else:
        run_invoke = f'python3 "$aqg_root/{entry_script}" --mode <X>'

    how_to_run = (
        "## How To Run\n\n"
        "```bash\n"
        "# Source the AQG_ROOT resolver helper. Bootstrap order:\n"
        "#   1. AQG_ROOT env > 2. CLAUDE_SKILL_DIR/.aqg-root sentinel\n"
        "#   3. fallback to standard install path.\n"
        "source \"${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh\" || {\n"
        "  echo \"ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout.\" >&2\n"
        "  exit 2\n"
        "}\n"
        f'{run_invoke}\n'
        "```\n"
    )

    # Trigger section — populate from sidecar trigger_grammar
    trigger_section = "## Trigger\n\nUse this skill when:\n\n"
    if keywords:
        trigger_section += f"- The user mentions any of: {', '.join(f'`{k}`' for k in keywords[:8])}\n"
    if sentence_patterns:
        trigger_section += "- The request matches patterns like:\n"
        for p in sentence_patterns[:5]:
            trigger_section += f"  - {p!r}\n"
    if not keywords and not sentence_patterns:
        trigger_section += "- (TODO: fill trigger conditions matching the description)\n"

    # Boundaries section per audit-requested allowlist
    boundaries_section = (
        "## Boundaries\n\n"
        f"- boundary_class: `{boundary_class}` (per sidecar)\n"
        "- Reads:\n"
    )
    for p in sidecar.get("reads_paths", []):
        boundaries_section += f"  - `{p}`\n"
    if sidecar.get("writes_paths"):
        boundaries_section += "- Writes:\n"
        for p in sidecar["writes_paths"]:
            boundaries_section += f"  - `{p}`\n"
    boundaries_section += "- Forbidden (must not touch):\n"
    for p in sidecar.get("forbidden_paths", []):
        boundaries_section += f"  - `{p}`\n"
    owner_only = sidecar.get("owner_only_actions", [])
    if owner_only:
        boundaries_section += "- Owner-only actions (require explicit Owner statement):\n"
        for a in owner_only:
            boundaries_section += f"  - {a}\n"

    # Owner Edges section
    owner_edges = (
        "## Owner Edges\n\n"
        "Stop and ask Owner when:\n\n"
        "- (TODO) production-adjacent action without prior authorization\n"
        "- (TODO) data classification escalation\n"
        "- (TODO) any Owner-only action listed above\n"
    )

    body = (
        f"---\n"
        f"name: {name}\n"
        f"description: {_quoted_scalar(description)}\n"
        f"---\n\n"
        f"# {_humanize_name(name)}\n\n"
        f"<!-- Generated by scripts/aqg_skill_gen.py from skill.template.json. "
        f"Edit the body sections; keep frontmatter as `name` + `description` only. -->\n\n"
        f"{trigger_section}\n"
        f"{how_to_run}\n"
        f"{boundaries_section}\n"
        f"{owner_edges}"
        f"{gating_block}"
    )
    return body


def _render_claude_wrapper_skill_md(sidecar: dict[str, Any]) -> str:
    """Render agent-packs/claude-code/skills/<name>/SKILL.md.

    Claude wrapper has same frontmatter + a How To Run block referencing the
    source skill via CLAUDE_SKILL_DIR. Body is otherwise minimal."""
    name = sidecar["name"]
    description = sidecar["description"]
    entry_script = sidecar["entry_script"]
    if entry_script.startswith(f"skills/{name}/"):
        entry_rel_to_skill = entry_script[len(f"skills/{name}/"):]
    else:
        entry_rel_to_skill = None

    if entry_rel_to_skill:
        run_invoke = (
            f'python3 "$aqg_root/skills/{name}/{entry_rel_to_skill}" '
            f'--mode <X>'
        )
    else:
        run_invoke = f'python3 "$aqg_root/{entry_script}" --mode <X>'

    return (
        f"---\n"
        f"name: {name}\n"
        f"description: {_quoted_scalar(description)}\n"
        f"---\n\n"
        f"# {_humanize_name(name)} (Claude wrapper)\n\n"
        f"<!-- Generated by scripts/aqg_skill_gen.py. This wrapper points "
        f"Claude Code at the source skill in skills/{name}/. -->\n\n"
        "## How To Run\n\n"
        "```bash\n"
        "# Resolve AQG_ROOT via CLAUDE_SKILL_DIR sentinel (Claude Code provides "
        "$CLAUDE_SKILL_DIR), with env / fallback per the shared helper.\n"
        "source \"${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh\" || {\n"
        "  echo \"ERROR: cannot resolve AQG root.\" >&2\n"
        "  exit 2\n"
        "}\n"
        f'{run_invoke}\n'
        "```\n\n"
        "## Boundaries\n\n"
        f"See source skill `skills/{name}/SKILL.md` for the canonical boundary declaration.\n"
    )


def _render_openai_yaml(sidecar: dict[str, Any]) -> str:
    """Render skills/<name>/agents/openai.yaml — the Codex interface manifest.

    A non-empty `interface.display_name` is required by aqg_skill_validator
    (`_validate_codex_interface`): without it the Codex skill picker falls back
    to the directory slug and renders "Aqg <Name>". display_name uses the
    humanized "AQG <Name>" form; short_description / default_prompt are seeded
    from the description and SHOULD be tightened by the author.
    """
    name = sidecar["name"]
    description = sidecar.get("description", "") or ""
    display_name = _humanize_name(name)
    # short_description is an author-editable seed — no hard length cap: shipped
    # skills' values run up to ~97 chars, so the earlier 70-char truncation was
    # tighter than the actual norm (audit b9d506d2 f1, adjudicated relax-not-shorten).
    summary = description.split(".", 1)[0].strip() or display_name
    default_prompt = f"Run {display_name} for the current task and report the result."
    # _quoted_scalar (json.dumps ensure_ascii=False) gives a valid YAML double-quoted scalar.
    return (
        "interface:\n"
        f"  display_name: {_quoted_scalar(display_name)}\n"
        f"  short_description: {_quoted_scalar(summary)}\n"
        f"  default_prompt: {_quoted_scalar(default_prompt)}\n"
    )


def _render_entry_script(sidecar: dict[str, Any]) -> str:
    """Render skills/<name>/scripts/<entry>.py — Python script skeleton with
    exit-code contract documented in module docstring per ADR §3.2."""
    name = sidecar["name"]
    description = sidecar["description"]
    entry_script = sidecar["entry_script"]
    cli_contract = sidecar.get("cli_contract", {})

    docstring_codes = []
    for code in (0, 1, 2, 3, 70):
        desc = cli_contract.get(str(code), cli_contract.get(code, "<TODO description>"))
        docstring_codes.append(f"    {code}: {desc}")
    exit_code_block = "Exit codes:\n" + "\n".join(docstring_codes)

    summary = description.split(".", 1)[0] if description else _humanize_name(name)
    if len(summary) > 80:
        summary = summary[:77] + "..."

    # D1: the free-text summary + cli_contract descriptions flow into the
    # generated module docstring, so escape them (a `"""` in a description would
    # otherwise close the docstring and inject code). The argparse strings below
    # use _quoted_scalar — a valid Python string literal — for the same reason.
    doc_summary = _escape_for_triple_double(summary)
    doc_exit_block = _escape_for_triple_double(exit_code_block)

    return (
        f'#!/usr/bin/env python3\n'
        f'"""{doc_summary}\n\n'
        f'{doc_exit_block}\n'
        f'"""\n\n'
        f'from __future__ import annotations\n\n'
        f'import argparse\n'
        f'import sys\n'
        f'\n'
        f'\n'
        f'EXIT_OK = 0\n'
        f'EXIT_CHECK_FAIL = 1\n'
        f'EXIT_USAGE = 2\n'
        f'EXIT_CONFIG = 3\n'
        f'EXIT_INTERNAL = 70\n'
        f'\n'
        f'# a3 audit fix #1: marker indicating this script is a generator stub.\n'
        f'# Author MUST set _SKILL_IMPLEMENTED = True after filling in main() logic.\n'
        f'# Until then, non-help invocations fail-closed with EXIT_INTERNAL.\n'
        f'_SKILL_IMPLEMENTED = False\n'
        f'\n'
        f'\n'
        f'def main(argv: list[str] | None = None) -> int:\n'
        f'    parser = argparse.ArgumentParser(\n'
        f'        prog={_quoted_scalar(Path(entry_script).stem)},\n'
        f'        description={_quoted_scalar(summary)},\n'
        f'    )\n'
        f'    parser.add_argument("--mode", required=True, help="(TODO: define modes)")\n'
        f'    parser.add_argument("--json", action="store_true", help="JSON output")\n'
        f'    args = parser.parse_args(argv)\n'
        f'\n'
        f'    if not _SKILL_IMPLEMENTED:\n'
        f'        print(\n'
        f'            f"ERROR: skill {name!r} is a generator stub; "\n'
        f'            f"set _SKILL_IMPLEMENTED=True after filling main() logic.",\n'
        f'            file=sys.stderr,\n'
        f'        )\n'
        f'        return EXIT_INTERNAL\n'
        f'\n'
        f'    # TODO: implement skill logic per sidecar.cli_contract output_shape.\n'
        f'    print(f"Stub: mode={{args.mode}} (skill={name!r})")\n'
        f'    return EXIT_OK\n'
        f'\n'
        f'\n'
        f'if __name__ == "__main__":\n'
        f'    raise SystemExit(main())\n'
    )


def _render_self_test(sidecar: dict[str, Any]) -> str:
    """Render skills/<name>/scripts/self_test.py — minimal self-test skeleton."""
    name = sidecar["name"]
    entry_script = sidecar["entry_script"]
    entry_module = Path(entry_script).stem
    # D1: entry_script flows into the docstring (escape) while the module name +
    # skill name flow into Python string literals (_quoted_scalar → valid literal).
    return (
        f'#!/usr/bin/env python3\n'
        f'"""Minimal self-test for {_escape_for_triple_double(entry_script)}."""\n\n'
        f'from __future__ import annotations\n\n'
        f'import subprocess\n'
        f'import sys\n'
        f'from pathlib import Path\n'
        f'\n'
        f'\n'
        f'def test_help_smoke() -> None:\n'
        f'    """Smoke: --help should exit 0 and print description."""\n'
        f'    script = Path(__file__).resolve().parent / {_quoted_scalar(entry_module + ".py")}\n'
        f'    proc = subprocess.run(\n'
        f'        [sys.executable, str(script), "--help"],\n'
        f'        text=True, capture_output=True,\n'
        f'    )\n'
        f'    assert proc.returncode == 0, proc.stderr\n'
        f'    assert {_quoted_scalar(name)} in proc.stdout or proc.stdout  # has some output\n'
    )


def _render_generated_md(sidecar: dict[str, Any]) -> str:
    """Render skills/<name>/GENERATED.md — the cross-cutting registration
    checklist. Emits exactly CROSS_CUTTING_TOUCHPOINTS `## N.` sections; the
    LLM/Owner applies the items then deletes the file. Per ADR §4.1 a2 #1 + #5."""
    name = sidecar["name"]
    cases = sidecar.get("cases", [])
    case_block = ""
    for c in cases[:5]:
        if isinstance(c, dict):
            # f3 (verify-round): every scalar carrying sidecar/case data is
            # double-quoted via _quoted_scalar so a schema-valid `id: on` / `123`
            # / `null` cannot YAML-implicit-type to bool / int / None when this
            # block is copy-pasted into triggers.yaml. The two *_sha256_first8
            # fields stay literal `null` on purpose — check_fixture_mix.py I7
            # reads them as YAML null to assert the managed⟺M2-null invariant.
            # The .get() keys + defaults are byte-preserved from the pre-fix line
            # (this hunk only adds quoting; it deliberately changes no defaults).
            case_block += (
                f'  - id: {_quoted_scalar(str(c.get("id", "TODO")))}\n'
                f'    style: {_quoted_scalar(str(c.get("style", "description-based")))}\n'
                f'    prompt: {_quoted_scalar(str(c.get("prompt") or "TODO"))}\n'
                f'    expected_skill: {_quoted_scalar(name)}\n'
                f'    skill_file_ref: {_quoted_scalar(f"skills/{name}/SKILL.md")}\n'
                f'    description_sha256_first8: null\n'
                f'    trigger_section_sha256_first8: null\n'
            )
    return (
        f"# Cross-Cutting Registration Checklist for `{name}`\n\n"
        f"**Generated by `scripts/aqg_skill_gen.py`**.\n\n"
        f"This file lists the {CROSS_CUTTING_TOUCHPOINTS} surgical edits needed to register the new\n"
        f"skill across hand-curated repo files. The generator does NOT auto-edit\n"
        f"these files (each is structured prose; auto-edit risks reformatting).\n"
        f"Apply each item, then **delete this file** — `aqg_skill_validator.py`\n"
        f"treats `GENERATED.md` presence as 'skill not yet registered'.\n\n"
        f"## 1. `scripts/install.sh`\n\n"
        f"Add `{name}` to the source-skill install loop alongside the other\n"
        f"`aqg-*` entries.\n\n"
        f"## 2. `agent-packs/claude-code/install.sh`\n\n"
        f"Add `{name}` to the Claude wrapper install loop.\n\n"
        f"## 3. `scripts/aqg_doctor.py`\n\n"
        f"Append `\"{name}\",` to both `CLAUDE_SKILL_NAMES` AND `CODEX_SKILL_NAMES` tuples.\n\n"
        f"## 4. `tests/behavior/fixtures/triggers.yaml`\n\n"
        f"Append the following 5 cases:\n\n"
        f"```yaml\n"
        f"{case_block}"
        f"```\n\n"
        f"Keep the M2 hash fields (`description_sha256_first8` / "
        f"`trigger_section_sha256_first8`) set to `null` — do not omit them. The\n"
        f"legacy drift-hash baseline was retired (spec 2026-05-31 §6/§7) so they\n"
        f"are no longer recomputed, but `check_fixture_mix.py` I7 reads them to\n"
        f"assert the `managed ⟺ M2-null` invariant; a managed skill must carry\n"
        f"them as null.\n\n"
        f"## 5. `tests/behavior/test_aqg_skill_trigger_canary.py`\n\n"
        f"Add `{name}` to `SKILL_TRIGGER_KEYWORDS` with >=1 trigger keyword that\n"
        f"actually appears in the description. The roster check fails CI (born red)\n"
        f"if the entry is missing. NB: `agents/openai.yaml` (display_name) is\n"
        f"auto-scaffolded by this generator — it is NOT a manual checklist step.\n\n"
        f"## 6. Sidecar `wrapper_generated: true` + regenerate the wrapper\n\n"
        f"Set `\"wrapper_generated\": true` in `skills/{name}/skill.template.json`, "
        f"then generate the Claude wrapper from the source SKILL.md:\n\n"
        f"```bash\n"
        f"python3 scripts/aqg_skill_gen.py regen {name}\n"
        f"```\n\n"
        f"The committed wrapper is then guarded by the CI regen→git-status gate\n"
        f"(this replaces the retired drift-hash baseline). "
        f"`aqg_skill_validator.py` requires this flag — an unmanaged skill fails\n"
        f"cross-cutting registration.\n\n"
        f"## 7. (Optional) `scripts/install_aqg_construction_hook.py`\n\n"
        f"Only if this skill needs a PostToolUse / pre-commit hook. Skip if not.\n\n"
        f"---\n\n"
        f"## After Applying\n\n"
        f"1. Delete this file: `rm skills/{name}/GENERATED.md`\n"
        f"2. Run validator: `python3 scripts/aqg_skill_validator.py {name}`\n"
        f"3. Run doctor: `python3 scripts/aqg_doctor.py` (PASS count should increase)\n"
        f"4. Run pytest: `python3 -m pytest tests/test_aqg_skill_validator.py`\n"
    )


def _humanize_name(skill_name: str) -> str:
    """aqg-startup-preflight → AQG Startup Preflight."""
    return " ".join(part.upper() if part == "aqg" else part.capitalize()
                    for part in skill_name.split("-"))


def _assert_outputs_within_repo(outputs: dict[str, Path], repo_root: Path) -> None:
    """D7 generator belt: every output path must resolve inside *repo_root*.

    The schema already pins entry_script / self_test_entrypoint under
    skills/<name>/ and rejects ../ absolute / drive paths, so this is
    defense-in-depth: if that gate is ever bypassed or refactored, the generator
    still refuses to write outside the repo. resolve() collapses symlinks / ..
    before the containment check. Raises RuntimeError naming the offending role.

    Scope (audit f2): this catches every symlink / .. that EXISTS at check time
    (resolve() follows them). It does NOT defend against a concurrent attacker
    swapping a path component for a symlink in the window between this check and
    the later open() / os.replace() — that TOCTOU is out of scope for a
    single-process trusted-repo generator (and the non-force write uses
    open(..., 'x') with O_EXCL, which won't follow an existing symlink to a new
    target). Full openat-style protection would be over-engineering here.
    """
    repo_resolved = repo_root.resolve()
    for role, out_path in outputs.items():
        out_resolved = out_path.resolve()
        if out_resolved != repo_resolved and repo_resolved not in out_resolved.parents:
            raise RuntimeError(
                f"refusing to write {role!r} outside repo root: "
                f"{out_path} -> {out_resolved}"
            )


# ===== Generator main =====


def generate_skill(
    sidecar: dict[str, Any],
    *,
    repo_root: Path,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Path]:
    """Emit the skill's output files (6, or 7 when entry_script is generated).
    Returns mapping of role → path.

    Validates the sidecar via _skill_template_schema.check_skill_template
    before any write. Refuses to overwrite existing files unless force=True.
    Atomic open('x') for non-force creates."""
    sys.path.insert(0, str(repo_root / "scripts"))
    from _skill_template_schema import check_skill_template  # noqa: E402

    schema_result = check_skill_template(sidecar)
    if not schema_result.is_safe:
        raise RuntimeError(
            "sidecar schema invalid; cannot generate.\n"
            + "\n".join(f"  - {v}" for v in schema_result.violations)
        )

    name = sidecar["name"]
    entry_script = sidecar["entry_script"]

    # Compute output paths
    skill_dir = repo_root / "skills" / name
    scripts_dir = skill_dir / "scripts"
    claude_skill_dir = repo_root / "agent-packs" / "claude-code" / "skills" / name

    # Determine entry script absolute path
    if entry_script.startswith(f"skills/{name}/"):
        entry_path = repo_root / entry_script
    else:
        # entry lives at repo-level scripts/<entry>.py — generator does NOT
        # emit there because that path is shared / hand-curated.
        # a3 audit fix #3: if the repo-level target does NOT already exist,
        # fail generation — emitted self-test + How To Run point at a
        # nonexistent executable, producing a broken skill.
        repo_entry_abs = repo_root / entry_script
        if not repo_entry_abs.is_file():
            raise RuntimeError(
                f"sidecar.entry_script={entry_script!r} points at a repo-level "
                f"path that does not exist; either move the entry under "
                f"skills/{name}/scripts/ (so the generator can emit it) or "
                f"create the repo-level script first before running the generator"
            )
        entry_path = None

    outputs: dict[str, Path] = {
        "skill_md": skill_dir / "SKILL.md",
        "self_test": scripts_dir / "self_test.py",
        "sidecar": skill_dir / SIDECAR_FILENAME,
        "openai_yaml": skill_dir / "agents" / "openai.yaml",
        "generated_md": skill_dir / "GENERATED.md",
        "claude_wrapper": claude_skill_dir / "SKILL.md",
    }
    if entry_path is not None:
        outputs["entry_script"] = entry_path

    # D7 (generator belt): assert every output path resolves inside repo_root.
    _assert_outputs_within_repo(outputs, repo_root)

    # Render content for each output
    rendered: dict[Path, str] = {
        outputs["skill_md"]: _render_skill_md(sidecar),
        outputs["self_test"]: _render_self_test(sidecar),
        outputs["sidecar"]: json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n",
        outputs["openai_yaml"]: _render_openai_yaml(sidecar),
        outputs["generated_md"]: _render_generated_md(sidecar),
        outputs["claude_wrapper"]: _render_claude_wrapper_skill_md(sidecar),
    }
    if entry_path is not None:
        rendered[entry_path] = _render_entry_script(sidecar)

    # Dry-run: just return what would be written
    if dry_run:
        return outputs

    # Pre-flight: check for existing files (unless force)
    if not force:
        for p in rendered:
            if p.exists():
                raise FileExistsError(
                    f"refusing to overwrite {p.relative_to(repo_root)} "
                    f"(use --force to overwrite)"
                )

    # a3 audit fix #5: atomic write for both modes.
    # - non-force: open('x') (O_EXCL) for atomic exclusive create
    # - force: write to .tmp sibling then os.replace (atomic rename)
    # D14: create EACH file's parent (parents=True) just before writing it, so a
    # deeply-nested entry_script (skills/<name>/scripts/sub/run.py) doesn't
    # FileNotFoundError on open(). Replaces the old fixed-set mkdir that only
    # created the top-level scripts/ + agents/ dirs.
    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if force:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=str(path.parent),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.replace(tmp_path, path)
            except Exception:
                # Clean up tmp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        else:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(content)

    return outputs


# ===== regen: wrapper regeneration from source (overlay design) =====
#
# The source skills/<X>/SKILL.md is the hand-edited truth; the Claude wrapper
# agent-packs/claude-code/skills/<X>/SKILL.md is a generated verbatim copy with
# the sidecar host_overrides.claude anchor swaps applied (see
# scripts/_aqg_wrapper_overlay.py + the spec 2026-05-31 §2-§5). `regen` owns the
# filesystem reads/writes; build_wrapper is the pure transform.


def _source_skill_md_path(repo_root: Path, skill: str) -> Path:
    return repo_root / "skills" / skill / "SKILL.md"


def _source_sidecar_path(repo_root: Path, skill: str) -> Path:
    return repo_root / "skills" / skill / SIDECAR_FILENAME


def _wrapper_skill_md_path(repo_root: Path, skill: str) -> Path:
    return repo_root / "agent-packs" / "claude-code" / "skills" / skill / "SKILL.md"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write content to path atomically (temp sibling + os.replace).

    Mirrors the force-overwrite path in generate_skill so a regen interrupted
    mid-write can never leave a partially-written wrapper on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:  # aqg: top-level boundary — clean up the temp file on ANY
        # failure (write/replace) then re-raise; never leak a partial wrapper or
        # an orphan .tmp. Mirrors the force-write path in generate_skill.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def render_wrapper_for_skill(repo_root: Path, skill: str) -> str:
    """Render the Claude wrapper text for one managed skill (pure of writes).

    Reads the source SKILL.md + sidecar, asserts the skill is managed, and
    returns build_wrapper(source, sidecar). Raises RuntimeError with a clear
    message on a missing file / unmanaged skill / malformed override so the CLI
    can map it to a non-zero exit.
    """
    source_md = _source_skill_md_path(repo_root, skill)
    sidecar_path = _source_sidecar_path(repo_root, skill)
    if not source_md.is_file():
        raise RuntimeError(f"source SKILL.md not found: {source_md}")
    if not sidecar_path.is_file():
        raise RuntimeError(f"sidecar not found: {sidecar_path}")
    try:
        sidecar = _load_sidecar(sidecar_path)
    except (json.JSONDecodeError, OSError, RuntimeError) as exc:
        raise RuntimeError(f"failed to load sidecar {sidecar_path}: {exc}") from exc
    if not is_managed_skill(sidecar):
        raise RuntimeError(
            f"skill {skill!r} is not managed (sidecar lacks "
            f"'wrapper_generated: true'); regen only handles managed skills"
        )
    source_text = source_md.read_text(encoding="utf-8")
    try:
        return build_wrapper(source_text, sidecar)
    except ValueError as exc:
        # build_wrapper raises ValueError on a stale/ambiguous/frontmatter anchor
        # or malformed overrides — surface it as a regen failure naming the skill.
        raise RuntimeError(f"skill {skill!r}: {exc}") from exc


def managed_skill_names(repo_root: Path) -> list[str]:
    """Sorted list of skills/aqg-*/ dirs whose sidecar is_managed_skill is True.

    Legacy/unmanaged skills (and dirs without a sidecar) are skipped silently —
    this is what makes the incremental migration safe (spec §4/§9): regen --all
    only ever rewrites managed wrappers.
    """
    skills_dir = repo_root / "skills"
    if not skills_dir.is_dir():
        return []
    managed: list[str] = []
    for d in sorted(skills_dir.glob("aqg-*")):
        if not d.is_dir():
            continue
        sidecar_path = d / SIDECAR_FILENAME
        if not sidecar_path.is_file():
            continue
        try:
            sidecar = _load_sidecar(sidecar_path)
        except (json.JSONDecodeError, OSError, RuntimeError):
            # A malformed sidecar is not silently "managed"; skip it here (the
            # validator / schema gate reports malformed sidecars separately).
            continue
        if is_managed_skill(sidecar):
            managed.append(d.name)
    return managed


def find_orphan_wrappers(repo_root: Path) -> list[str]:
    """Wrapper dirs under agent-packs/claude-code/skills/aqg-*/ with no matching
    source dir under skills/aqg-*/ (a deleted source would otherwise leave a
    stale wrapper that regen --all never revisits — spec §5 round-2 gemini-f1)."""
    wrappers_dir = repo_root / "agent-packs" / "claude-code" / "skills"
    if not wrappers_dir.is_dir():
        return []
    orphans: list[str] = []
    for d in sorted(wrappers_dir.glob("aqg-*")):
        if not d.is_dir():
            continue
        if not (repo_root / "skills" / d.name).is_dir():
            orphans.append(d.name)
    return orphans


def _diff_summary(skill: str, expected: str, actual: str) -> str:
    """Unified-diff summary (committed wrapper vs freshly rendered)."""
    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=f"committed: agent-packs/claude-code/skills/{skill}/SKILL.md",
        tofile=f"regenerated: {skill}",
    )
    return "".join(diff)


def regen_one(repo_root: Path, skill: str, *, check: bool) -> str | None:
    """Regenerate (or, when check=True, verify) one managed skill's wrapper.

    Returns None on success/no-diff. When check=True and the committed wrapper
    differs from the freshly rendered one, returns a diff summary string (the
    caller maps a non-None return to a non-zero exit). Raises RuntimeError on a
    render failure (missing source / unmanaged / bad anchor).
    """
    rendered = render_wrapper_for_skill(repo_root, skill)
    wrapper_path = _wrapper_skill_md_path(repo_root, skill)
    if check:
        committed = (
            wrapper_path.read_text(encoding="utf-8")
            if wrapper_path.is_file()
            else ""
        )
        if committed == rendered:
            return None
        if not wrapper_path.is_file():
            return (
                f"{skill}: committed wrapper missing "
                f"({wrapper_path}); regen would create it\n"
                + _diff_summary(skill, rendered, "")
            )
        return _diff_summary(skill, rendered, committed)
    _atomic_write_text(wrapper_path, rendered)
    return None


def regen_main(argv: list[str]) -> int:
    """Handle the `regen` subcommand. `argv` excludes the leading 'regen' token."""
    parser = argparse.ArgumentParser(
        prog="aqg_skill_gen.py regen",
        description=(
            "Regenerate the Claude wrapper SKILL.md from the source SKILL.md + "
            "sidecar host_overrides (overlay design)."
        ),
    )
    parser.add_argument(
        "skill", nargs="?", default=None,
        help="skill name (e.g. aqg-startup-preflight); omit with --all",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="regen every managed skill (skips legacy/unmanaged silently)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="compare committed wrapper to freshly rendered; non-zero on mismatch",
    )
    parser.add_argument(
        "--orphans", action="store_true",
        help="assert every wrapper dir has a matching source dir (no orphans)",
    )
    args = parser.parse_args(argv)

    try:
        repo_root = _find_repo_root()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INTERNAL

    # --orphans can run alone (regen --check --orphans) or fold into --all.
    orphan_violation = False
    if args.orphans:
        orphans = find_orphan_wrappers(repo_root)
        if orphans:
            for o in orphans:
                print(
                    f"::error::orphan wrapper with no source dir: "
                    f"agent-packs/claude-code/skills/{o} (skills/{o} missing)",
                    file=sys.stderr,
                )
            orphan_violation = True
        # --orphans with no skill and not --all → orphan-only mode; done here.
        if not args.all and args.skill is None:
            return EXIT_SCHEMA_OR_WRITE if orphan_violation else EXIT_OK

    # Determine the target skill set.
    if args.all:
        skills = managed_skill_names(repo_root)
    elif args.skill is not None:
        skills = [args.skill]
    else:
        print(
            "ERROR: regen needs a <skill> argument or --all (or --check --orphans)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    drift = False
    for skill in skills:
        try:
            result = regen_one(repo_root, skill, check=args.check)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_SCHEMA_OR_WRITE
        if args.check:
            if result is not None:
                print(
                    f"::error::wrapper out of sync for {skill} — run "
                    f"'aqg_skill_gen.py regen {skill}' and commit.",
                    file=sys.stderr,
                )
                print(result, file=sys.stderr)
                drift = True
            else:
                print(f"OK: {skill} wrapper matches source")
        else:
            print(f"OK: regenerated wrapper for {skill}")

    if drift or orphan_violation:
        return EXIT_SCHEMA_OR_WRITE
    return EXIT_OK


# ===== CLI =====


def main(argv: list[str] | None = None) -> int:
    # Subcommand dispatch: the FIRST positional token decides the mode. The
    # literal 'regen' routes to regen_main; anything else falls through to the
    # legacy positional-sidecar scaffold path (preserved byte-for-byte so
    # existing tests + the GUIDE invocation keep working).
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "regen":
        return regen_main(raw[1:])

    parser = argparse.ArgumentParser(
        prog="aqg_skill_gen",
        description="Generate AQG skill skeleton from sidecar JSON manifest.",
    )
    parser.add_argument("sidecar", help="path to sidecar JSON file")
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite existing output files (default: refuse + exit 3)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="preview output paths without writing files",
    )
    args = parser.parse_args(argv)

    sidecar_path = Path(args.sidecar).resolve()
    if not sidecar_path.is_file():
        print(f"ERROR: sidecar not found: {sidecar_path}", file=sys.stderr)
        return EXIT_USAGE

    try:
        sidecar = _load_sidecar(sidecar_path)
    except (json.JSONDecodeError, OSError, RuntimeError) as exc:
        print(f"ERROR: failed to load sidecar: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        repo_root = _find_repo_root()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INTERNAL

    try:
        outputs = generate_skill(
            sidecar, repo_root=repo_root, force=args.force, dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(f"ERROR (schema): {exc}", file=sys.stderr)
        return EXIT_SCHEMA_OR_WRITE
    except UnicodeError as exc:
        # f1 belt (verify-round): the schema now rejects lone surrogates, so a
        # sidecar can no longer reach the utf-8 write carrying one. This catch is
        # defense-in-depth — if that gate is ever bypassed or regressed, a
        # surrogate yields a clean error instead of a traceback (cf. #220's
        # decode belt). UnicodeError is a ValueError subclass, unrelated to the
        # RuntimeError / FileExistsError / OSError branches, so ordering is free.
        print(f"ERROR (encoding): {exc}", file=sys.stderr)
        return EXIT_SCHEMA_OR_WRITE
    except FileExistsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FILE_EXISTS
    except OSError as exc:
        print(f"ERROR (write): {exc}", file=sys.stderr)
        return EXIT_SCHEMA_OR_WRITE

    if args.dry_run:
        print(f"DRY RUN: would generate {len(outputs)} file(s):")
    else:
        print(f"OK: generated {len(outputs)} file(s) for skill {sidecar['name']!r}:")
    for role, path in outputs.items():
        try:
            display = path.relative_to(repo_root)
        except ValueError:
            display = path
        print(f"  [{role}] {display}")

    if not args.dry_run:
        print("\nNext steps (see GENERATED.md inside the skill dir):")
        print(f"  1. Apply the {CROSS_CUTTING_TOUCHPOINTS} cross-cutting registration items in GENERATED.md")
        print(f"  2. Delete GENERATED.md (rm skills/{sidecar['name']}/GENERATED.md)")
        print(f"  3. Run: python3 scripts/aqg_skill_validator.py {sidecar['name']}")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
