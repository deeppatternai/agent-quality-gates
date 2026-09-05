"""Shipped SKILL.md files must not instruct an agent to call a tool that does not exist.

`de_audit` is not a tool the Decision Engine MCP server exposes — the real ones are
`audit_skill_submit` / `submit_audit`. Five skills nonetheless told the caller to
invoke it, two of them in the `description:` frontmatter that the skill picker
reads. These files are installed onto users' machines, so the wrong name shipped.

The fix names the SKILL (`/audit`) rather than a tool: a tool name that has already
drifted once will drift again, while the skill name is stable across Claude Code,
Codex and Cursor. This test keeps it that way.

Scope, stated precisely rather than aspirationally: every file whose text is
COPIED VERBATIM onto a user's machine or read as an instruction by an agent —
both SKILL.md trees, the `skill.template.json` sidecars, the `examples/` rules
templates that users append into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`,
and the READMEs.

The first version of this docstring claimed "every file that ships skill prose"
while globbing only SKILL.md and sidecars. That overclaim is the same defect this
file exists to catch, and it hid a real gap: the `examples/` templates — the
literal source of users' installed rules blocks — still named the retired tool
after the sweep that declared itself done.

Deliberately NOT covered, and tracked as debt in the policy's migration table
instead of being silently ignored:
- `skills/*/scripts/*.py` docstrings and comments, which mention the retired name
  while EXPLAINING the boundary ("this script does NOT call de_audit"). Their
  emitted strings — the ones an agent acts on — were swept.
- `docs/AUDIT_DECISION_MODEL.md` (+ zh-CN), which is neither executed nor copied
  to a user's machine.
- `posttooluse_code_construction_reminder.sh`, which mentions it in a comment
  recording this very history.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RETIRED_TOOL_NAME = "de_audit"


def _skill_docs() -> list[Path]:
    """Every file that ships a skill's prose to a user's machine.

    The first version of this scan covered SKILL.md only and missed
    `skill.template.json` — the SIDECAR, which carries the canonical description
    the validator compares against. Five sidecars still named the retired tool
    after the SKILL.md sweep, and the skill validator caught it, not this test.
    """
    return sorted(
        list((REPO / "skills").glob("*/SKILL.md"))
        + list((REPO / "agent-packs").glob("*/skills/*/SKILL.md"))
        + list((REPO / "skills").glob("*/skill.template.json"))
        + list((REPO / "agent-packs").glob("*/skills/*/skill.template.json"))
        # The rules templates users append into ~/.claude/CLAUDE.md and
        # ~/.codex/AGENTS.md. These are the highest-stakes files in the scan:
        # their text becomes the always-resident instruction block on a real
        # machine, so a wrong tool name here is executed, not merely read.
        + list((REPO / "examples").glob("aqg-*.example.md"))
        + [p for p in (REPO / "README.md", REPO / "README.zh-CN.md") if p.is_file()]
    )


def test_no_shipped_skill_doc_names_the_retired_tool() -> None:
    offenders: list[str] = []
    for doc in _skill_docs():
        text = doc.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if RETIRED_TOOL_NAME in line:
                offenders.append(f"{doc.relative_to(REPO)}:{lineno}: {line.strip()[:90]}")
    assert not offenders, (
        f"{len(offenders)} shipped skill-doc line(s) still name the non-existent "
        f"`{RETIRED_TOOL_NAME}` tool:\n" + "\n".join(offenders)
    )


def _emitted_string_literals(path: Path) -> list[tuple[int, str]]:
    """Every string literal in a module EXCEPT docstrings.

    Comments never enter the AST, and docstrings are excluded explicitly, so what
    is left is the text the program can actually emit — prints, `--help` text,
    ledger skeletons, returned messages. That is the precise line between
    "prose explaining the boundary" (allowed to name the retired tool while
    saying the script does NOT call it) and "an instruction an agent will act on".
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def _runtime_scripts() -> list[Path]:
    return sorted(
        list((REPO / "skills").glob("*/scripts/*.py")) + list((REPO / "scripts").glob("*.py"))
    )


def test_no_emitted_runtime_string_names_the_retired_tool() -> None:
    """The half the doc scan cannot see: what the scripts PRINT.

    The doc scan reads shipped prose. It never opens `skills/*/scripts/*.py`, so
    a `print("run `de_audit(...)`")` sailed past it — and the migration-debt row
    initially claimed those emitted strings were "kept done" by that scan, which
    was false. This test is what makes the claim true.

    Docstrings and comments are deliberately out of scope: several of them say
    "this script does NOT call de_audit", which is correct and should survive.
    """
    offenders: list[str] = []
    for script in _runtime_scripts():
        for lineno, text in _emitted_string_literals(script):
            if RETIRED_TOOL_NAME in text:
                offenders.append(
                    f"{script.relative_to(REPO)}:{lineno}: {text.strip()[:80]}"
                )
    assert not offenders, (
        f"{len(offenders)} runtime string(s) still instruct the caller to use the "
        f"non-existent `{RETIRED_TOOL_NAME}` tool:\n" + "\n".join(offenders)
    )


def test_the_runtime_scan_reaches_the_scripts_that_matter() -> None:
    """A scan that opened no files would pass vacuously."""
    scripts = _runtime_scripts()
    assert len(scripts) >= 20, f"runtime scan looks too narrow: {len(scripts)} files"
    rel = {p.relative_to(REPO).as_posix() for p in scripts}
    for must in (
        "skills/aqg-phase-transition/scripts/aqg_phase_emit.py",
        "skills/aqg-multi-review/scripts/aqg_multi_review.py",
        "scripts/aqg_skill_validator.py",
    ):
        assert must in rel, f"runtime scan does not reach {must}"
    # And it must actually be reading literals, not returning an empty list.
    literals = _emitted_string_literals(REPO / "skills/aqg-phase-transition/scripts/aqg_phase_emit.py")
    assert len(literals) > 20, f"literal extraction looks broken: {len(literals)} found"


def test_the_fixture_would_catch_a_regression() -> None:
    """The scan reaches both copies of every skill, not just one tree."""
    docs = _skill_docs()
    assert len(docs) >= 45, f"scan looks too narrow: only {len(docs)} shipped docs found"
    trees = {"skills" if "agent-packs" not in str(d) else "agent-packs" for d in docs}
    assert trees == {"skills", "agent-packs"}, f"only scanned {trees}"
    kinds = {d.name for d in docs}
    assert {"SKILL.md", "skill.template.json"} <= kinds, (
        f"sidecars must be scanned too, not only SKILL.md; found {kinds}"
    )
    # The rules templates are the reason this scan exists at all: their text is
    # what lands in a user's always-resident instruction block. Assert them by
    # name so a future narrowing of the glob is a red test, not a silent gap.
    rel = {d.relative_to(REPO).as_posix() for d in docs}
    for must in (
        "examples/aqg-claude-rules.example.md",
        "examples/aqg-codex-agents.example.md",
        "README.md",
        "README.zh-CN.md",
    ):
        assert must in rel, f"installed-verbatim file dropped from the scan: {must}"
    # A kind-only guard cannot see a tree x kind hole: sidecars exist under
    # skills/ today and not under agent-packs/, so scanning one tree's sidecars
    # satisfies the kind check while the other tree stays unscanned the moment a
    # sidecar appears there. Assert the globbed PATTERNS instead of the files
    # they happen to match today.
    patterns = {
        ("skills", "SKILL.md"),
        ("agent-packs", "SKILL.md"),
        ("skills", "skill.template.json"),
        ("agent-packs", "skill.template.json"),
    }
    covered = {
        ("skills" if "agent-packs" not in str(d) else "agent-packs", d.name) for d in docs
    }
    missing = patterns - covered
    assert missing <= {("agent-packs", "skill.template.json")}, f"unscanned: {missing}"
