#!/usr/bin/env python3
"""Validate AQG agent-pack structure without external dependencies."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


EXIT_USAGE = 2
EXIT_VALIDATION = 1
EXIT_INTERNAL = 70

FORBIDDEN_WARN_ONLY_TOKENS = (
    "exit 2",
    '"decision": "block"',
    '"continue": false',
    "permission-deny",
)
# D15: capture the FULL exit/return code so multi-digit nonzero codes are caught.
# The old `[1-9]\b` matched only single digits 1-9, so `exit 10` / `exit 127`
# slipped through (the digit is not followed by a word boundary) and a warn-only
# hook could block undetected. We now capture `\d+` and flag any nonzero value.
BLOCKING_COMMAND_RE = re.compile(r"\b(?:exit|return)\s+(\d+)\b")

# T1-B Hard/Soft dependency validation (per AQG absorption plan a1, AQG-T1-B).
# Source: mattpocock skills ADR 0001-explicit-setup-pointer-only-for-hard-dependencies.md
# Format: `dependencies: name1:hard, name2:soft, ...` (comma-separated).
DEPENDENCY_ENTRY_RE = re.compile(r"^([a-zA-Z0-9_/.-]+):(hard|soft)$")

# Hard-dep setup pointer phrasings (audit F1 fix: covers "if missing/not
# installed/not set up, run/install/configure X" variations + classic mattpocock
# wording). Each pattern is searched within the SAME paragraph as the dep name
# to avoid satisfying multiple deps with one generic word.
HARD_DEP_POINTER_PATTERNS = (
    re.compile(r"should\s+have\s+been\s+provided", re.IGNORECASE),
    re.compile(r"if\s+(?:not|missing|not\s+installed|not\s+set\s+up)[^.\n]*\b(?:run|install|configure|set\s+up)\b", re.IGNORECASE),
    re.compile(r"\b(?:run|install|configure)\b[^.\n]*\bif\s+(?:not|missing)", re.IGNORECASE),
    re.compile(r"required\s+setup", re.IGNORECASE),
    re.compile(r"setup\s+pointer", re.IGNORECASE),
    re.compile(r"\bsetup\b[^.\n]*\b(?:command|script|cli|tool)\b", re.IGNORECASE),
)

# T1-C Description style lint (per AQG absorption plan a1, AQG-T1-C).
# Source: mattpocock skills/productivity/write-a-skill/SKILL.md Description Requirements.
DESCRIPTION_MAX_CHARS = 1024
DESCRIPTION_WARN_CHARS = 800
USE_WHEN_PATTERNS = (
    re.compile(r"\buse\s+when\b", re.IGNORECASE),
    re.compile(r"\btrigger", re.IGNORECASE),
    re.compile(r"\bwhen\s+(?:user|writing|reviewing|debugging|creating|building|adding|modifying|implementing|the )", re.IGNORECASE),
)
# Conservative first-person check — flag only obvious agent self-reference, not
# legitimate trigger sentences like "Use when you write code".
FIRST_PERSON_AGENT_PATTERNS = (
    re.compile(r"\bI\s+(?:help|am|will|can|do)\b"),
    re.compile(r"\bwe\s+(?:help|are|will|can|do)\b", re.IGNORECASE),
)


class ValidationError(ValueError):
    """Raised when an agent pack fails validation."""


def parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValidationError(f"{path}: missing frontmatter start")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip():
            continue
        if ":" not in line:
            raise ValidationError(f"{path}: malformed frontmatter line: {line}")
        key, value = line.split(":", 1)
        if value.strip() in {"|", ">"}:
            raise ValidationError(f"{path}: multiline frontmatter values are not supported")
        if key.strip() in fields:
            raise ValidationError(f"{path}: duplicate frontmatter key: {key.strip()}")
        fields[key.strip()] = value.strip()
    else:
        raise ValidationError(f"{path}: missing frontmatter end")
    for key in ("name", "description"):
        if not fields.get(key):
            raise ValidationError(f"{path}: missing frontmatter {key}")
    return fields


# Match all script refs of the form `$aqg_root/<path>.py` or `${aqg_root}/<path>.py` in SKILL.md.
# Use [\w./-] to allow common Python path chars; whitespace/quote boundaries naturally terminate the match.
SCRIPT_REF_RE = re.compile(r"\$(?:aqg_root|\{aqg_root\})/([\w./-]+\.py)")


def extract_script_refs(text: str) -> list[str]:
    """Extract all `$aqg_root/.../*.py` references from SKILL.md text, deduplicated and order-preserving."""
    seen: list[str] = []
    for match in SCRIPT_REF_RE.findall(text):
        if match not in seen:
            seen.append(match)
    return seen


def parse_dependencies(value: str) -> list[tuple[str, str]]:
    """Parse 'name1:hard, name2:soft' frontmatter into [(name, kind), ...].

    T1-B per AQG absorption plan a1. Raises ValidationError on malformed entry.
    """
    if not value.strip():
        return []
    parts = [p.strip() for p in value.split(",") if p.strip()]
    result: list[tuple[str, str]] = []
    for part in parts:
        m = DEPENDENCY_ENTRY_RE.match(part)
        if not m:
            raise ValidationError(
                f"invalid dependencies entry '{part}': "
                f"expected 'name:hard' or 'name:soft' (comma-separated)"
            )
        result.append((m.group(1), m.group(2)))
    return result


def validate_dependencies_field(
    fields: dict[str, str], text: str, path: Path
) -> list[str]:
    """T1-B: validate Hard/Soft dependency declarations.

    Returns list of WARN messages. Raises ValidationError on syntax errors
    or missing setup pointer for hard dependencies.

    Backward compat: skills without `dependencies:` field do NOT fail (opt-in;
    existing 10 AQG skills retrofit per-PR over time).
    """
    warnings: list[str] = []
    dep_value = fields.get("dependencies", "")
    if not dep_value:
        return warnings  # opt-in — no field means no validation

    deps = parse_dependencies(dep_value)
    if not deps:
        return warnings

    # Audit F1 fix: per-name + same-paragraph check. Split body into paragraphs
    # (blank-line separated). For each hard dep, REQUIRE the dep name + any
    # pointer phrase appear in the SAME paragraph — otherwise generic
    # "prerequisite" elsewhere would satisfy all hard deps.
    paragraphs = re.split(r"\n\s*\n", text)
    for name, kind in deps:
        if kind == "hard":
            name_pattern = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
            satisfied = False
            for para in paragraphs:
                if not name_pattern.search(para):
                    continue
                if any(p.search(para) for p in HARD_DEP_POINTER_PATTERNS):
                    satisfied = True
                    break
            if not satisfied:
                raise ValidationError(
                    f"{path}: hard dependency '{name}' declared in frontmatter "
                    f"but no setup pointer found in the same paragraph as the "
                    f"dep name. Expected phrases like 'should have been provided', "
                    f"'if not installed, run X', 'if missing, install Y', "
                    f"'required setup', or 'setup pointer'."
                )
        elif kind == "soft":
            # Audit F2 fix: case-insensitive boundary-aware match (re.escape +
            # word boundary). Handles "Glossary" vs "glossary" and avoids
            # "doc" matching "documentation".
            name_pattern = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
            if not name_pattern.search(text):
                warnings.append(
                    f"{path}: soft dependency '{name}' declared but not "
                    f"referenced in body (recommended: mention in vague prose)"
                )
    return warnings


def validate_description_style(
    fields: dict[str, str], path: Path
) -> list[str]:
    """T1-C: validate description frontmatter style.

    Returns list of WARN messages. Raises ValidationError on hard limit
    (length > DESCRIPTION_MAX_CHARS).

    Source: mattpocock skills/productivity/write-a-skill/SKILL.md.
    """
    warnings: list[str] = []
    desc = fields.get("description", "")
    if not desc:
        # validate_skill already raises on missing — defense in depth.
        return warnings

    if len(desc) > DESCRIPTION_MAX_CHARS:
        raise ValidationError(
            f"{path}: description length {len(desc)} exceeds "
            f"{DESCRIPTION_MAX_CHARS} char hard limit"
        )
    if len(desc) > DESCRIPTION_WARN_CHARS:
        warnings.append(
            f"{path}: description length {len(desc)} exceeds "
            f"{DESCRIPTION_WARN_CHARS} char soft limit "
            f"(recommended < {DESCRIPTION_WARN_CHARS})"
        )

    # Trigger sentence check (Use when / when [action] / trigger)
    if not any(p.search(desc) for p in USE_WHEN_PATTERNS):
        warnings.append(
            f"{path}: description missing 'Use when' / trigger sentence "
            f"(recommended for skill auto-discovery)"
        )

    # Agent self-reference check (third-person guideline)
    if any(p.search(desc) for p in FIRST_PERSON_AGENT_PATTERNS):
        warnings.append(
            f"{path}: description uses first-person agent self-reference "
            f"('I help', 'we are', etc); recommended third-person per "
            f"mattpocock skill style"
        )

    return warnings


def extract_body(text: str) -> str:
    """Return the SKILL.md body (everything after the closing `---` frontmatter)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text  # no frontmatter — body is whole file
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[i + 1 :])
    return ""  # frontmatter never closed — defensive


def validate_skill(path: Path, repo_root: Path | None = None) -> list[str]:
    """Validate a Claude Code skill SKILL.md.

    Raises ValidationError on FAIL conditions. Returns list of WARN messages
    (non-fatal; caller prints to stderr).
    """
    fields = parse_frontmatter(path)
    text = path.read_text(encoding="utf-8")
    if not fields["name"].startswith("aqg-"):
        raise ValidationError(f"{path}: Claude Code skill name must start with aqg-")
    if "scripts/" not in text and "quality-gates.json" not in text:
        raise ValidationError(f"{path}: skill must reference shared AQG scripts or config")
    if "CLAUDE_SKILL_DIR" not in text and "AQG_ROOT" not in text:
        raise ValidationError(f"{path}: skill must resolve from CLAUDE_SKILL_DIR or AQG_ROOT")

    # T1-B + T1-C: dependency declaration + description style lint.
    # Both opt-in: missing dependencies field = skip; description warnings
    # don't fail. Hard violations (syntax error / length > 1024) still raise.
    # Use body (post-frontmatter) so soft-dep name check doesn't match the
    # `dependencies:` frontmatter line itself.
    body = extract_body(text)
    warnings: list[str] = []
    warnings.extend(validate_dependencies_field(fields, body, path))
    warnings.extend(validate_description_style(fields, path))

    # Cross-pack dependency check: the $aqg_root/...py scripts referenced by SKILL.md must actually exist.
    # Prevents Claude Code SKILL.md refs from silently breaking after Codex skills are renamed/moved.
    # Test fixtures don't write real script refs, so skip when there are no refs.
    if repo_root is None:
        return warnings
    refs = extract_script_refs(text)
    # D16: a captured ref may contain `../` or an absolute-looking `//etc/...`,
    # which `repo_root / ref` would resolve OUTSIDE the repo — the is_file()
    # check could then validate an out-of-repo script. Reject any ref that
    # escapes repo_root before the existence check.
    repo_resolved = repo_root.resolve()
    missing: list[str] = []
    for ref in refs:
        resolved = (repo_root / ref).resolve()
        try:
            inside = resolved.is_relative_to(repo_resolved)
        except AttributeError:  # Python < 3.9: relative_to raises ValueError if outside
            # (use the Path API, not a hard-coded separator — platform-agnostic)
            try:
                resolved.relative_to(repo_resolved)
                inside = True
            except ValueError:
                inside = False
        if not inside:
            raise ValidationError(
                f"{path}: script ref escapes the repo root (path traversal): {ref}"
            )
        if not resolved.is_file():
            missing.append(ref)
    if missing:
        raise ValidationError(
            f"{path}: references missing scripts: {', '.join(missing)} "
            f"(checked under {repo_root})"
        )
    return warnings


def iter_hook_commands(value: Any) -> list[str]:
    commands: list[str] = []
    if type(value) is dict:
        if type(value.get("command")) is str:
            commands.append(value["command"])
        for item in value.values():
            commands.extend(iter_hook_commands(item))
    elif type(value) is list:
        for item in value:
            commands.extend(iter_hook_commands(item))
    return commands


def iter_hook_entries(value: Any) -> list[dict]:
    """Yield every hook entry dict (the leaf object containing `type` + `command`).

    Used to inspect per-command attributes like `_blocking: true`. A hook entry is
    a dict that has a string `command` field and (typically) a string `type` field.
    """
    entries: list[dict] = []
    if type(value) is dict:
        if type(value.get("command")) is str:
            entries.append(value)
        for item in value.values():
            entries.extend(iter_hook_entries(item))
    elif type(value) is list:
        for item in value:
            entries.extend(iter_hook_entries(item))
    return entries


def walk_json(value: Any) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if type(value) is dict:
        for key, item in value.items():
            items.append((str(key), item))
            items.extend(walk_json(item))
    elif type(value) is list:
        for item in value:
            items.extend(walk_json(item))
    return items


def validate_hook(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc

    # Audit gpt-5.5 #3 fix: per-command `_blocking: true` attribute scopes the
    # warn-only opt-out to the specific hook entry, not the entire file. This is
    # tighter than the file-level `_warn_only: false` previously considered:
    # the blocking example (settings.blocking.example.json) mixes 2 blocking PreToolUse hooks with warn-only hooks;
    # a file-level switch would let a future accidental `exit 1` in any of the
    # warn-only hooks bypass validation. Per-command marker prevents that drift.
    #
    # Backward compat: a hook entry without `_blocking` defaults to warn-only
    # (False). The legacy file-level `_warn_only: false` key is rejected loudly
    # to force migration to per-command marking (no silent contract drift).
    if "_warn_only" in payload:
        raise ValidationError(
            f"{path}: file-level `_warn_only` key is no longer supported; "
            "use per-command `_blocking: true` on each intentionally-blocking hook entry"
        )

    blocking_commands: set[str] = set()
    for entry in iter_hook_entries(payload):
        if entry.get("_blocking", False):
            if entry["_blocking"] is not True:
                raise ValidationError(
                    f"{path}: _blocking must be `true` boolean (got {entry['_blocking']!r})"
                )
            cmd_value = entry.get("command", "")
            if type(cmd_value) is str:
                blocking_commands.add(cmd_value)

    text = path.read_text(encoding="utf-8")
    lowered = text.lower()

    # Token-level forbidden-token scan: only flag when the token appears in a
    # warn-only command (i.e., a command NOT marked `_blocking: true`).
    for token in FORBIDDEN_WARN_ONLY_TOKENS:
        if token not in lowered:
            continue
        # Find token within at-least-one non-blocking command. If ALL occurrences
        # are inside blocking commands, allow; otherwise reject.
        for cmd in iter_hook_commands(payload):
            if token in cmd.lower() and cmd not in blocking_commands:
                raise ValidationError(f"{path}: warn-only hook contains forbidden blocking token: {token}")

    # Structured blocking field check (`decision=block`, `continue=false`,
    # `permissionDecision=deny|ask`) applies to non-blocking entries only. Walk
    # entries and check on parent entry context.
    for entry in iter_hook_entries(payload):
        if entry.get("_blocking", False) is True:
            continue
        for key, value in walk_json(entry):
            key_normalized = key.lower()
            if key_normalized == "decision" and type(value) is str and value.lower() == "block":
                raise ValidationError(f"{path}: warn-only hook contains decision=block")
            if key_normalized == "continue" and value is False:
                raise ValidationError(f"{path}: warn-only hook contains continue=false")
            if key_normalized == "permissiondecision" and type(value) is str and value.lower() in {"deny", "ask"}:
                raise ValidationError(f"{path}: warn-only hook contains permissionDecision={value}")

    commands = iter_hook_commands(payload)
    if not commands:
        raise ValidationError(f"{path}: no hook command found")
    for command in commands:
        if "$CLAUDE_PROJECT_DIR" not in command and "${CLAUDE_PROJECT_DIR" not in command:
            raise ValidationError(f"{path}: hook command must use CLAUDE_PROJECT_DIR")
        if command in blocking_commands:
            continue  # marked `_blocking: true` — skip warn-only blocking checks
        blocking_match = BLOCKING_COMMAND_RE.search(command)
        if blocking_match and blocking_match.group(1).strip("0"):
            # nonzero exit/return code (any nonzero digit survives strip("0")).
            # Conservative lint: this scans the raw command text of a *curated
            # example* hook, so a literal `exit 10` even inside a quote/comment is
            # (intentionally) flagged — example hooks must stay visibly warn-only.
            # Full shell tokenization is out of scope for linting a few examples.
            raise ValidationError(f"{path}: warn-only hook command contains blocking exit/return")
        if re.search(r"\|\|\s*false\b", command, re.IGNORECASE):  # D15: also catch `|| FALSE`
            raise ValidationError(f"{path}: warn-only hook command contains || false")
        if "|| true" not in command and "exit 0" not in command:
            raise ValidationError(f"{path}: warn-only hook command must avoid blocking")


def validate_claude_code_pack(pack: Path) -> list[str]:
    if not pack.is_dir():
        raise ValidationError(f"pack path is not a directory: {pack}")
    skill_files = sorted((pack / "skills").glob("*/SKILL.md"))
    if not skill_files:
        raise ValidationError(f"{pack}: no Claude Code skills found")

    # repo_root is used for the cross-pack dependency check: pack/agent-packs/claude-code, 2 levels up = repo root.
    # Only enable the cross-check when the layout really is agent-packs/claude-code; test fixtures use a temporary
    # pack path that doesn't match this structure, so the cross-check is skipped automatically (keeps the existing fixture tests from regressing).
    repo_root: Path | None = None
    if pack.name == "claude-code" and pack.parent.name == "agent-packs":
        candidate = pack.parent.parent
        if (candidate / "VERSION").is_file() and (candidate / "scripts").is_dir():
            repo_root = candidate

    notes: list[str] = []
    for skill in skill_files:
        skill_warnings = validate_skill(skill, repo_root=repo_root)
        for w in skill_warnings:
            # WARN to stderr (non-fatal), keeps stdout clean for "OK ..." log.
            print(f"WARN: {w}", file=sys.stderr)
        notes.append(f"OK skill {skill}")
    hook_files = sorted((pack / "hooks").glob("*.json"))
    if not hook_files:
        raise ValidationError(f"{pack}: no hook JSON examples found")
    for hook in hook_files:
        validate_hook(hook)
        notes.append(f"OK hook {hook}")
    install = pack / "install.sh"
    if not install.exists():
        raise ValidationError(f"{pack}: missing install.sh")
    notes.append(f"OK install {install}")
    return notes


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate AQG agent-pack layout.")
    parser.add_argument("--agent", required=True, choices=["claude-code"], help="agent pack type")
    parser.add_argument("--pack", required=True, help="agent pack path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.agent == "claude-code":
            notes = validate_claude_code_pack(Path(args.pack).expanduser())
        else:
            raise ValidationError(f"unsupported agent: {args.agent}")
        for note in notes:
            print(note)
        return 0
    except ValidationError as exc:
        print(f"VALIDATION_ERROR: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    except OSError as exc:
        print(f"INTERNAL_ERROR: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # noqa: BLE001 - validator should classify unexpected failures
        print(f"INTERNAL_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
