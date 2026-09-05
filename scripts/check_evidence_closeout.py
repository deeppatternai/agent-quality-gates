#!/usr/bin/env python3
"""Warn or fail when a Markdown closeout lacks required evidence items."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path


REQUIRED_ITEMS = (
    "scope completed",
    "verification run",
    "audit adjudicated",
    "durable state updated",
    "production boundary",
    "remaining blockers",
)

EMPTY_VALUES = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "todo",
    "tbd",
    "pending",
}


def normalize(value: str) -> str:
    value = value.strip().strip("|").strip()
    value = re.sub(r"[*_`]", "", value)
    value = re.sub(r"[^a-z0-9 \-/]", " ", value.lower())
    value = value.replace("/", " ")
    value = value.replace("-", " ")
    return re.sub(r"\s+", " ", value).strip()


def is_placeholder(value: str) -> bool:
    stripped = value.strip()
    lowered = stripped.lower()
    normalized = normalize(stripped)
    if normalized in EMPTY_VALUES:
        return True
    if "__fill" in lowered:
        return True
    if re.search(r"\b(todo|tbd)\b", lowered):
        return True
    if re.search(r"<[^>]+>", stripped):
        return True
    # GP-07 (+ audit 6012a42d f2): a value with fewer than 3 alphanumeric
    # characters is not substantive evidence ("x", "ok", "x/y", "o-k", "n/a").
    # Counting compact alphanumerics (not whitespace tokens) also catches the
    # punctuation-delimited short fragments a single-token check missed, since
    # normalize() turns `/` and `-` into spaces. "none"/"done"/"yes" stay valid
    # (>= 3 alphanumerics).
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    if 0 < len(compact) < 3:
        return True
    return False


def heading_spans(text: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    spans: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        title = match.group(2).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        spans.append((title, text[start:end]))
    return spans


def is_evidence_heading_title(title: str) -> bool:
    """True iff a heading title marks an evidence/closeout block. Single source of
    truth shared by `has_evidence_heading` (PASS gate) and `evidence_blocks`
    (extraction) so the two can never drift on what counts as an evidence heading."""
    normalized = normalize(title)
    return "evidence" in normalized or "closeout" in normalized or "证据" in title


def has_evidence_heading(text: str) -> bool:
    """True iff the document carries an evidence/closeout/证据 heading.

    The evidence gate REQUIRES this (WS-8 P2-5c): a headingless doc that merely
    scatters the required labels must not clear the gate. `evidence_blocks` keeps
    a headingless fallback below for lenient item *extraction* only (diagnostics /
    boundary detection) — it never satisfies this heading requirement.

    Scope note: this is a self-discipline gate, not an adversarial boundary (same
    posture as the memory-write guard). The heading test is a substring match on
    the title, so an author who *wants* to evade it can (a heading inside a code
    fence, or a semantically-negative title like `## No Evidence Block`). Those are
    accepted residuals — an honest author does not do either, and a shell-capable
    agent can bypass any self-attestation gate regardless. The gate catches the
    common honest miss: forgetting the evidence heading."""
    return any(is_evidence_heading_title(title) for title, _ in heading_spans(text))


def evidence_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    for title, body in heading_spans(text):
        if is_evidence_heading_title(title):
            blocks.append(body)
    # Fallback for EXTRACTION ONLY (not a pass condition — see has_evidence_heading
    # / validate_text): lets callers still pull scattered label:value items (e.g.
    # boundary rows) out of a headingless doc for diagnostics.
    if not blocks and any(item in normalize(text) for item in REQUIRED_ITEMS):
        blocks.append(text)
    return blocks


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(cell and set(cell) <= {"-", ":", " "} for cell in cells)


def extract_table_items(block: str) -> dict[str, str]:
    items: dict[str, str] = {}
    lines = [line.strip() for line in block.splitlines() if line.strip().startswith("|") and line.strip().endswith("|")]
    for line in lines:
        cells = split_table_row(line)
        if is_separator(cells) or len(cells) < 2:
            continue
        label = normalize(cells[0])
        if label in REQUIRED_ITEMS:
            items[label] = " | ".join(cells[1:]).strip()
    return items


def extract_colon_items(block: str) -> dict[str, str]:
    items: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        match = re.match(r"^(?:\*\*)?([^:*|]{3,80}?)(?:\*\*)?\s*:\s*(.+)$", line)
        if not match:
            continue
        label = normalize(match.group(1))
        if label in REQUIRED_ITEMS:
            items[label] = match.group(2).strip()
    return items


def extract_items(blocks: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for block in blocks:
        for label, value in extract_table_items(block).items():
            found[label] = value
        for label, value in extract_colon_items(block).items():
            found[label] = value
    return found


def validate_text(text: str) -> tuple[bool, list[str], dict[str, str]]:
    blocks = evidence_blocks(text)
    issues: list[str] = []
    if not blocks:
        issues.append("missing evidence block heading or required evidence labels")
        return False, issues, {}
    if not has_evidence_heading(text):
        # WS-8 P2-5c: labels were found via the headingless fallback. A genuine
        # evidence/closeout heading is required — do not let a headingless doc pass.
        issues.append("missing evidence block heading (add an ## Evidence Block heading)")

    found = extract_items(blocks)
    for item in REQUIRED_ITEMS:
        if item not in found:
            issues.append(f"missing evidence item: {item}")
        elif is_placeholder(found[item]):
            issues.append(f"evidence item has placeholder value: {item}")
    return not issues, issues, found


def read_input(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    return sys.stdin.read()


def self_test() -> int:
    valid = """# PR

## Evidence Block

| item | evidence |
|---|---|
| scope completed | scripts and docs updated |
| verification run | `python3 scripts/check_evidence_closeout.py --self-test` -> exit 0 |
| audit adjudicated | no external audit used; local review completed |
| durable state updated | PR body updated with evidence |
| production boundary | no production, secrets, or raw data touched |
| remaining blockers | none |
"""
    missing = valid.replace("| remaining blockers | none |\n", "")
    placeholder = valid.replace("scripts and docs updated", "TODO")

    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        good = tmp / "good.md"
        good.write_text(valid, encoding="utf-8")
        ok, issues, found = validate_text(good.read_text(encoding="utf-8"))
        assert ok and len(found) == len(REQUIRED_ITEMS), (issues, found)

        for name, text in {"missing.md": missing, "placeholder.md": placeholder}.items():
            bad = tmp / name
            bad.write_text(text, encoding="utf-8")
            ok, issues, _ = validate_text(bad.read_text(encoding="utf-8"))
            assert not ok and issues, (name, issues)

    print("OK: check_evidence_closeout self-test passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Encoding robustness: on Windows the std streams default to cp936, so Chinese
    # output would mojibake / raise UnicodeEncodeError. Force UTF-8 on output —
    # effectively a no-op on Linux/macOS (already UTF-8); errors="replace" only
    # affects the rare undecodable char, and never crashes. stdin stays strict so
    # malformed piped input surfaces instead of being silently replaced.
    # Same approach as aqg_re_anchor.py; regression: tests/test_win_utf8_stdout.py.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8")  # strict: surface bad input
    except (AttributeError, ValueError, OSError):
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", help="markdown file; omit to read stdin")
    parser.add_argument("--strict", action="store_true", help="return non-zero when evidence is incomplete")
    parser.add_argument("--self-test", action="store_true", help="run offline markdown fixture self-test")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    ok, issues, found = validate_text(read_input(args.path))
    mode = "strict" if args.strict else "warn-only"
    if ok:
        print(f"OK: evidence closeout complete ({mode}); items={len(found)}")
        return 0

    print(f"{'FAIL' if args.strict else 'WARN'}: evidence closeout incomplete ({mode})")
    for issue in issues:
        print(f"- {issue}")
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
