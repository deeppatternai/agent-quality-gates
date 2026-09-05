#!/usr/bin/env python3
"""Create and validate project debugging case notes.

Exit codes:
  0: success — case notes created or validation passed
  1: validation failed — case file missing required sections / fields
  2: usage error — invalid arguments or unknown subcommand
  3: reserved — config or schema error (not currently emitted)
  70: reserved — internal error placeholder for future use
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import os
from pathlib import Path


REQUIRED_SECTIONS = [
    "Symptom",
    "Fresh Reproduction",
    "Evidence",
    "Hypotheses",
    "Root Cause",
    "Fix",
    "Verification",
    "Boundaries",
]

REQUIRED_SECTIONS_SET = set(REQUIRED_SECTIONS)

# A Root Cause whose whole normalized body is one of these is "not yet
# confirmed"; in that state the Fix must also be a pending marker (no fix
# without a confirmed root cause). Whole-value match, not substring, so a
# confirmed root cause that merely quotes a phrase is not mis-flagged.
PENDING_ROOT_CAUSE_MARKERS = {
    "not confirmed yet",
    "not confirmed",
    "unconfirmed",
    "pending",
    "pending investigation",
    "pending root cause",
    "unknown",
}
PENDING_FIX_MARKERS = {
    "pending",
    "pending root cause",
    "not applicable until root cause is confirmed",
    "n/a until root cause is confirmed",
}


DEFAULT_OUTPUT_DIR = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "aqg-debug-cases"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" .")


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """Char ranges covered by ``` / ~~~ fenced code blocks (so headings inside
    logs are not treated as section delimiters)."""
    spans: list[tuple[int, int]] = []
    fence_start = None
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            if fence_start is None:
                fence_start = pos
            else:
                spans.append((fence_start, pos + len(line)))
                fence_start = None
        pos += len(line)
    if fence_start is not None:  # unterminated fence runs to EOF
        spans.append((fence_start, len(text)))
    return spans


def _in_any(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= pos < hi for lo, hi in ranges)


def _has_standalone_placeholder_marker(body: str) -> bool:
    """True if a line is a bare task-marker placeholder (e.g. a lone `TODO` /
    `TODO:`), not the marker word appearing inside legitimate prose."""
    for line in body.splitlines():
        if re.match(r"^\s*todo\b[\s:.\-]*$", line, re.IGNORECASE):
            return True
    return False


TEMPLATE = """# Debug Case: {title}

- generated_at: {generated_at}
- scope: current project

## Symptom

__FILL_SYMPTOM__

## Fresh Reproduction

```bash
__FILL_COMMAND__
```

- exit: __FILL_EXIT_CODE__
- observed: __FILL_OBSERVED__

## Evidence

- __FILL_FACT_1_WITH_SOURCE__
- __FILL_FACT_2_WITH_SOURCE__

## Hypotheses

| hypothesis | evidence for | evidence against | test | result |
|---|---|---|---|---|
| __FILL_HYPOTHESIS__ | __FILL_EVIDENCE_FOR__ | __FILL_EVIDENCE_AGAINST__ | `__FILL_TEST_COMMAND__` | __FILL_RESULT__ |

## Root Cause

__FILL_ROOT_CAUSE_OR_NOT_CONFIRMED_YET__

## Fix

__FILL_FIX_OR_PENDING_ROOT_CAUSE__

## Verification

- original symptom: `__FILL_COMMAND__` -> __FILL_RESULT__
- regression scope (stakes-scaled — trivial: one check; high-stakes security/concurrency/multi-file/irreversible: the adjacent blast-radius surface, still focused, NOT the full suite — that authoritative run is CI's / the project gate's job): __FILL_SCOPE__
- regression check(s): `__FILL_COMMAND__` -> __FILL_RESULT__

## Boundaries

- production writes/deploys/restarts: __FILL_NONE_OR_AUTHORIZED_EVIDENCE__
- secrets/raw/private data: __FILL_NONE_OR_AUTHORIZATION__
- Owner/user decision needed: __FILL_NONE_OR_EXACT_BLOCKER__
"""


def slugify(title: str) -> str:
    slug = re.sub(r"[^\w.-]+", "-", title.strip().lower(), flags=re.UNICODE).strip("-")
    return slug or "debug-case"


def section_spans(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse required-section bodies.

    Only level-2 headings that NAME a required section and lie OUTSIDE fenced
    code blocks act as delimiters. A custom sub-heading therefore does not
    truncate a section (which would let a later placeholder escape validation),
    and a required name pasted inside a log fence does not satisfy validation.
    Returns (spans, duplicate_required_names).
    """
    fenced = _fenced_spans(text)
    heading_re = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = [
        m
        for m in heading_re.finditer(text)
        if m.group(1).strip() in REQUIRED_SECTIONS_SET and not _in_any(m.start(), fenced)
    ]
    spans: dict[str, str] = {}
    duplicates: list[str] = []
    for idx, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if name in spans:
            if name not in duplicates:
                duplicates.append(name)
            continue  # keep the first occurrence's body
        spans[name] = body
    return spans, duplicates


def validate(path: Path) -> int:
    if not path.exists():
        print(f"FAIL: debug case file not found: {path}")
        return 1
    if not path.is_file():
        print(f"FAIL: debug case path is not a regular file: {path}")
        return 1
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"FAIL: cannot read debug case {path}: {type(exc).__name__}")
        return 1

    spans, duplicates = section_spans(text)
    if duplicates:
        print("FAIL: duplicate required sections: " + ", ".join(duplicates))
        return 1
    missing = [section for section in REQUIRED_SECTIONS if section not in spans]
    if missing:
        print("FAIL: missing sections: " + ", ".join(missing))
        return 1

    placeholders = []
    for name in REQUIRED_SECTIONS:
        body = spans[name]
        if not body or "__FILL_" in body or _has_standalone_placeholder_marker(body):
            placeholders.append(name)
    if placeholders:
        print("FAIL: unfilled placeholders in: " + ", ".join(placeholders))
        return 1

    # If the root cause is still a pending marker, the Fix must also be pending
    # (no fix without a confirmed root cause). Whole-value match, so a confirmed
    # root cause that merely quotes such a phrase is not mis-flagged.
    if (
        _normalize(spans["Root Cause"]) in PENDING_ROOT_CAUSE_MARKERS
        and _normalize(spans["Fix"]) not in PENDING_FIX_MARKERS
    ):
        print("FAIL: root cause is a pending marker, but Fix is not a pending placeholder")
        return 1

    print("OK: debug case has required evidence sections")
    return 0


def create(title: str, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    name = f"{now.date().isoformat()}-{slugify(title)}.md"
    path = output_dir / name
    if path.exists():
        stem = path.stem
        suffix = path.suffix
        counter = 2
        while path.exists():
            path = output_dir / f"{stem}-{counter}{suffix}"
            counter += 1
    path.write_text(
        TEMPLATE.format(title=title, generated_at=now.isoformat()),
        encoding="utf-8",
    )
    print(path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    new_p = sub.add_parser("new")
    new_p.add_argument("--title", required=True)
    new_p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="where to write the debug case")

    val_p = sub.add_parser("validate")
    val_p.add_argument("path")

    args = parser.parse_args()
    if args.cmd == "new":
        return create(args.title, Path(args.output_dir))
    if args.cmd == "validate":
        return validate(Path(args.path))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
