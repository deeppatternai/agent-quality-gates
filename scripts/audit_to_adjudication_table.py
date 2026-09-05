#!/usr/bin/env python3
"""Extract an explicit audit adjudication table from Markdown."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from validate_audit_adjudication import (
    REQUIRED_HEADERS,
    _lax_lost_decision_rows,
    _parse_tables_verbose,
    validate_text,
)


EXIT_GATE_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 70


class UsageError(ValueError):
    """Raised when a runtime input or output path is invalid."""


def render_table(source_tables: list[tuple[list[str], list[list[str]]]]) -> str:
    """Render one normalized adjudication table from every source table's rows.

    Rows are projected onto the canonical REQUIRED_HEADERS order per source table
    (each table indexes by its own headers), then concatenated in document order.
    """
    lines = [
        "## Audit Adjudication",
        "",
        "| finding | decision | action | verification |",
        "|---|---|---|---|",
    ]
    for headers, rows in source_tables:
        indexes = {name: headers.index(name) for name in REQUIRED_HEADERS}
        for row in rows:
            cells = [clean_cell(row[indexes[name]]) for name in REQUIRED_HEADERS]
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def clean_cell(value: str) -> str:
    # Escape pipes (CF-2 / issue #306) only OUTSIDE backtick code spans, so a shell
    # pipeline keeps its meaning on the round-trip. An outside pipe becomes `\|`,
    # which split_cells decodes back; an in-span pipe is left verbatim — split_cells
    # keeps it in-code, so it neither fractures the row nor gains a stray backslash
    # (a code span does not process escapes). Backtick spans are matched by run
    # length, mirroring split_cells.
    value = value.replace("\n", " ")
    out: list[str] = []
    code_run = 0
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "`":
            run_end = i
            while run_end < len(value) and value[run_end] == "`":
                run_end += 1
            length = run_end - i
            if code_run == 0:
                code_run = length
            elif length == code_run:
                code_run = 0
            out.append("`" * length)
            i = run_end
            continue
        if ch == "|" and code_run == 0:
            out.append(r"\|")
            i += 1
            continue
        out.append(ch)
        i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def convert(text: str) -> str:
    # Normalize first (LAX): _parse_tables_verbose tolerates reordered /
    # extra-column audit output and projects every adjudication table (not just
    # the first — GP-03 / issue #234) onto the canonical column order. Metadata
    # tables (headers without the four required columns) are skipped.
    matching = [
        (headers, rows, dropped)
        for headers, rows, dropped in _parse_tables_verbose(text)
        if set(REQUIRED_HEADERS).issubset(headers)
    ]
    if not matching:
        raise ValueError("no audit adjudication table found")
    # Fail closed on ambiguous / lossy input (CF-3, CF-4 / issue #306). Tolerating
    # reordered / extra columns (decision-2 LAX) is unchanged, but a DUPLICATE
    # required column would silently project only the first occurrence, a
    # cell-count-mismatch row is silently dropped, and a blank-line-orphaned
    # decision row sits in a headerless block the LAX parse never collects (a
    # different mechanism than the cell-count drop — it is not even counted) —
    # each would ship a wrong or an incomplete adjudication table.
    #
    # The orphan check reuses the gate's decision-value-anchored detector (#339)
    # via the LAX-aware `_lax_lost_decision_rows`: a recognized decision row in a
    # HEADER-LESS block the LAX parse discards. Restricting to header-less blocks is
    # what keeps a separator-less table — which `_parse_tables_verbose` collects
    # without a separator (LAX) — from being false-rejected (audit 82b1979f f2). It
    # runs on the INPUT, because render_table's output is always a clean canonical
    # table, so the strict re-validation at the end (validate_text) would never see
    # an input-side orphan.
    orphans = _lax_lost_decision_rows(text)
    if orphans:
        locs = ", ".join(f"line {lineno}" for lineno, _ in orphans)
        raise ValueError(
            f"{len(orphans)} adjudication row(s) orphaned outside a table "
            f"(stray blank line or missing header?): {locs}"
        )
    for headers, _rows, dropped in matching:
        dupes = [h for h in REQUIRED_HEADERS if headers.count(h) > 1]
        if dupes:
            raise ValueError(f"duplicate required column(s): {', '.join(dupes)}")
        if dropped:
            raise ValueError(
                f"{dropped} malformed row(s) dropped (cell count != header width)"
            )
    rendered = render_table([(headers, rows) for headers, rows, _ in matching])
    # Validate the NORMALIZED output against the strict contract: the rendered
    # canonical table must pass full shape + content validation before it ships.
    ok, issues, _ = validate_text(rendered)
    if not ok:
        raise ValueError("; ".join(issues))
    return rendered


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize an explicit audit adjudication table from an audit output markdown file.",
    )
    parser.add_argument("--audit-output", required=True, help="markdown file containing an adjudication table")
    parser.add_argument("--output", required=True, help="path to write normalized markdown table")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        source = Path(args.audit_output).expanduser()
        output = Path(args.output).expanduser()
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise UsageError(f"cannot read audit output {source}: {exc}") from exc
        rendered = convert(text)
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot write output {output}: {exc}") from exc
        print(f"OK: wrote audit adjudication table to {output}")
        return 0
    except UsageError as exc:
        print(f"USAGE_ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_GATE_FAILURE
    except Exception as exc:  # noqa: BLE001 - classify unexpected converter failures
        print(f"INTERNAL_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
