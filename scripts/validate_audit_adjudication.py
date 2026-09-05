#!/usr/bin/env python3
"""Validate a strict audit adjudication table in Markdown.

Single source of truth for adjudication-table validation across AQG: the gate
pipeline imports the module API (`validate_text`, `parse_tables`,
`REQUIRED_HEADERS`), and the aqg-audit-adjudication skill invokes the CLI. It
merges the two former validators:

- shape strictness (from the skill validator, audits 469e66f5 / ae917256):
  pipe-aware cell splitting (escaped pipes + backtick code spans do not
  fracture cells), exactly the four required columns (no missing / extra /
  duplicate), a required separator row, and a hard row-cell-count match.
- content strictness (from the gate validator): empty / placeholder cells are
  rejected, and a `needs-user-decision` row must name both an actor and the
  awaited object. Decision values accept English aliases and Chinese
  (采纳 / 拒绝 / 需要用户决定 …).

Multi-table aware (GP-03 / issue #234): every adjudication table in the
document is validated and rendered, not just the first, so a clean decoy table
cannot mask a broken one after it.

Exit codes:
  0: success — adjudication table parsed, shape valid, content complete
  1: validation failed — missing/extra/duplicate columns, missing separator
     row, row-cell count mismatch, unrecognized decision values, empty or
     placeholder cells, a needs-user-decision row that fails to name who and
     what is awaited, or no data rows
  2: usage error — argparse exits 2 on unknown / malformed args (the path
     argument is optional; omit it to read stdin)
  3: reserved — config or schema error (not currently emitted)
  70: reserved — internal error placeholder for future use
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path


# Ordered tuple is the canonical render/column order (audit_to_adjudication_table
# projects rows onto it); REQUIRED is the set used for header matching.
REQUIRED_HEADERS = ("finding", "decision", "action", "verification")
REQUIRED = frozenset(REQUIRED_HEADERS)

ACCEPTED = {"accepted", "accept", "采纳", "接受"}
REJECTED = {"rejected", "reject", "拒绝", "不采纳"}
NEEDS_USER = {
    "needs-user-decision",
    "needs user decision",
    "user-decision",
    "owner-decision",
    "needs-owner-decision",
    "需要用户决定",
    "需要owner决定",
    "需用户决定",
}

EMPTY_VALUES = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "todo",
    "tbd",
    "pending",
}

ACTOR_MARKERS = {
    "owner",
    "user",
    "admin",
    # `administrator` is the formal form of the same actor as `admin`; it is listed
    # explicitly because #308's whole-word ASCII matching does not fire `admin`
    # inside it (closed-lexicon false-reject, #306). The matcher's `s?` covers the
    # plural. CJK `管理员` needs no counterpart — it is substring-matched.
    "administrator",
    "maintainer",
    "reviewer",
    "human",
    "operator",
    "security",
    "product",
    "用户",
    "负责人",
    "管理员",
    "审计员",
    "人工",
}

# GP-01 (audit 2026-06-03): `什么` ("what") is intentionally NOT a marker — an
# interrogative/vague token must not satisfy "name the concrete what".
OBJECT_MARKERS = {
    "authorization",
    "approval",
    "decision",
    "setting",
    "branch protection",
    "secret",
    "credential",
    "production",
    "deploy",
    "rollback",
    "permission",
    "授权",
    "审批",
    "决定",
    "设置",
    "密钥",
    "凭据",
    "生产",
    "部署",
    "回滚",
    "权限",
}

# CJK markers have no word boundary, so a marker can be a misleading substring of
# a longer compound that names something else (人工 "human/manual" ⊂ 人工智能 "AI").
# Map each such marker to the compounds where it is a false friend; the substring
# match removes them before testing, so the AI phrasing no longer satisfies the
# actor test while a standalone 人工 still does (#306). Seeded with the one
# audit-tracked overlap; extend (with corpus evidence) as more are found.
CJK_FALSE_FRIENDS: dict[str, tuple[str, ...]] = {
    "人工": ("人工智能",),
}


def normalize(cell: str) -> str:
    """Lowercased, emphasis-stripped form for header / decision matching.

    Strips backticks and markdown emphasis (*, _) so a bold/italic/code-wrapped
    decision like a doubled-asterisk "accepted" still matches.
    """
    # Final strip: removing emphasis markers (*, _) can expose inner spaces
    # (e.g. "** accepted **" -> " accepted "), so re-strip before matching.
    return re.sub(r"\s+", " ", cell.strip().strip("`*_").lower()).strip()


def normalize_cell(cell: str) -> str:
    """Whitespace-collapsed, backtick-stripped form that PRESERVES case.

    Used for stored row content (rendered verbatim by the converter and checked
    by `filled`); header/decision comparisons use `normalize` instead.
    """
    return re.sub(r"\s+", " ", cell.strip().strip("`")).strip()


def split_cells(line: str) -> list[str]:
    """Split a markdown table row into raw cell strings.

    Honors backslash-escaped pipes and pipes inside backtick code spans, so a
    shell pipeline or escaped pipe in an action/verification cell does not
    fracture the row into extra cells (audit ae917256 C1). Code spans are
    matched by backtick run length, so a double-backtick span keeps its inner
    pipe; `\\|` is decoded to a bare pipe only OUTSIDE code (a code span does not
    process escapes, so inside one the backslash is preserved) (#306).
    """
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells: list[str] = []
    buf: list[str] = []
    code_run = 0  # length of the backtick run that opened the current span; 0 = outside code
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and code_run == 0 and i + 1 < len(line) and line[i + 1] == "|":
            buf.append("|")  # escaped pipe outside code is literal content, not a delimiter
            i += 2
            continue
        if ch == "`":
            run_end = i
            while run_end < len(line) and line[run_end] == "`":
                run_end += 1
            length = run_end - i
            if code_run == 0:
                code_run = length  # open a span of this backtick run length
            elif length == code_run:
                code_run = 0  # a matching run length closes the span
            # a different-length run inside a span is literal content
            buf.append("`" * length)
            i = run_end
            continue
        if ch == "|" and code_run == 0:
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf))
    return cells


def _is_required_header(line: str) -> bool:
    """True if `line`'s normalized cells are a superset of REQUIRED (a header row).

    The single header-recognition predicate shared by the strict collector
    (`_collect_tables`), the lax parser (`_parse_tables_verbose`), and the
    converter's lost-row mirror (`_lax_lost_decision_rows`), so the three cannot
    drift on what counts as an adjudication header — `_lax_lost_decision_rows`
    must capture exactly what `_parse_tables_verbose` captures (#306, audit
    698fba2c). Superset (not equality): extra columns still read as a header here;
    `_collect_tables` flags them as a shape violation downstream.
    """
    return REQUIRED.issubset({normalize(c) for c in split_cells(line)})


def _looks_like_separator(cells: list[str]) -> bool:
    """True if every cell is a GFM delimiter token (`---` / `:--`), width-agnostic.

    Each cell must contain at least one hyphen — a colon-only cell (`|:|:|`) is
    not a valid GFM delimiter (#306).
    """
    return bool(cells) and all(c and set(c) <= {"-", ":"} and "-" in c for c in cells)


def _is_separator(cells: list[str], width: int) -> bool:
    """True if cells form a markdown separator row (`---` / `:--`) of exactly `width`."""
    return len(cells) == width and _looks_like_separator(cells)


def filled(value: str) -> bool:
    """False if a cell is empty or a placeholder (EMPTY_VALUES / __fill / <...>)."""
    stripped = normalize_cell(value)
    lowered = stripped.lower()
    if lowered in EMPTY_VALUES:
        return False
    if "__fill" in lowered:
        return False
    if re.search(r"\b(todo|tbd)\b", lowered):
        return False
    # Only a WHOLE-cell <...> token is a placeholder; inline angle-bracket
    # content in a real sentence (e.g. an HTML tag) is legitimate (#306).
    if re.fullmatch(r"<[^>]+>", stripped):
        return False
    return True


def _marker_present(markers: set[str], text: str, *, whole_word: bool) -> bool:
    """True if any marker occurs in `text` (already lowercased).

    `whole_word=True` requires an ASCII marker to stand as a whole word
    (optionally pluralized) rather than as a substring of a longer word;
    `whole_word=False` matches substrings. CJK markers are always
    substring-matched (no reliable word boundary between Han characters), but a
    known false-friend compound is stripped first so the marker is not matched
    as a misleading substring of it (CJK_FALSE_FRIENDS; e.g. 人工 ⊂ 人工智能).
    """
    for marker in markers:
        if whole_word and marker.isascii():
            # Boundary on ASCII alphanumerics only — so `product` does not fire
            # inside `production`, yet a marker glued to CJK (`等owner确认`)
            # still matches — with an optional trailing plural `s`.
            if re.search(rf"(?<![a-z0-9]){re.escape(marker)}s?(?![a-z0-9])", text):
                return True
        else:
            # Substring match (CJK markers, or ASCII object markers). For a CJK
            # marker with no word boundary, mask its known false-friend compounds
            # first so `人工` does not match inside `人工智能` (#306). Mask with a
            # NUL sentinel, not "" — deleting the span would fuse the flanking
            # characters into a spurious marker (人 + 人工智能 + 工 → 人工).
            haystack = text
            for compound in CJK_FALSE_FRIENDS.get(marker, ()):
                haystack = haystack.replace(compound, "\x00")
            if marker in haystack:
                return True
    return False


def needs_user_detail(action: str, verification: str) -> bool:
    """True if a needs-user-decision row names both an actor and the object.

    The actor test is whole-word so an object word like `production` cannot
    satisfy it via its embedded `product` (CF-1 / issue #306); the object test
    stays substring so inflected forms (`deployment`, `permissions`) still
    match. The CJK `人工`⊂`人工智能` overlap is disambiguated via
    CJK_FALSE_FRIENDS (#306).
    """
    combined = f"{action} {verification}".lower()
    return _marker_present(ACTOR_MARKERS, combined, whole_word=True) and _marker_present(
        OBJECT_MARKERS, combined, whole_word=False
    )


def decision_class(value: str) -> str | None:
    """Map a decision cell to 'accepted' / 'rejected' / 'needs-user-decision'.

    Returns None for an unrecognized value. Accepts English aliases and Chinese.
    """
    v = normalize(value)
    if v in ACCEPTED:
        return "accepted"
    if v in REJECTED:
        return "rejected"
    if v in NEEDS_USER:
        return "needs-user-decision"
    return None


_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _pipe_blocks(text: str) -> list[list[tuple[int, str]]]:
    """Group consecutive `|`-prefixed lines into blocks of (lineno, stripped_line).

    A non-pipe line (blank or prose) ends the current block. Shared by the strict
    collector and the orphan-row check so both agree on where a table block ends.

    The leading-`|` requirement is intentional: it keeps the false-positive surface
    to lines deliberately started with a pipe (prose almost never does). A GFM table
    written WITHOUT leading pipes is therefore unrecognized — fail-safe (the pure
    case fails loud as "no table"); see `_orphan_decision_rows` Known limitations
    (#306, Owner 2026-06-22, confirmed intended).
    """
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("|"):
            current.append((lineno, stripped))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _strip_closed_fences(text: str) -> str:
    """Blank lines inside CLOSED fenced code blocks (``` / ~~~), preserving line count.

    Only a properly closed fence is stripped: a 3+ backtick/tilde opener (info
    string allowed) and a closer of >= that many of the SAME character with only
    trailing whitespace. An UNCLOSED opener is left in place (its following lines
    are still scanned) so a stray ``` cannot silently hide a dropped decision row
    from the gate — fenced examples authors actually write are closed, so they are
    stripped and not false-flagged (#306, audit bde372f9).
    """
    lines = text.splitlines()
    out = list(lines)
    i = 0
    while i < len(lines):
        opener = _FENCE_OPEN.match(lines[i])  # raw line: GFM allows only 0-3 spaces of indent
        if not opener:
            i += 1
            continue
        marker = opener.group(1)
        closer = re.compile(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$")
        j = i + 1
        while j < len(lines) and not closer.match(lines[j]):
            j += 1
        if j < len(lines):  # matched closer → blank the fence (opener..closer inclusive)
            for k in range(i, j + 1):
                out[k] = ""
            i = j + 1
        else:  # unclosed → leave as-is so it cannot hide following decision rows
            i += 1
    return "\n".join(out)


def _orphan_decision_rows(text: str) -> list[tuple[int, str]]:
    """Pipe rows carrying a decision value that sit OUTSIDE any table region.

    Anchored on the DECISION VALUE (accepted / rejected / needs-user-decision /
    采纳 …), not table shape — so placeholders, captions, separators and metadata
    rows (none of which carry a decision value) are never mistaken for a dropped
    row, and an all-`-` placeholder row (syntactically a separator) cannot let a
    real decision row masquerade as a header. A table region is a header (a
    NON-separator line that does NOT itself carry a decision value, immediately
    followed by a separator OF THE SAME WIDTH) through its data rows OF THAT WIDTH.
    A loose decision-bearing row is one `_collect_tables` drops silently — a stray
    blank line, a headerless block, or a row before the header (#306, 790410fc
    follow-up; audits bde372f9 / 19728422). The width match mirrors the collector
    (a header+separator of mismatched width is not a table, and a wrong-width row
    is not a data row of the table) so the gate's "inside" set tracks what the
    collector actually validates rather than a divergent heuristic.

    Known limitations (audit 19728422, contrived / fail-safe, tracked in #306):
    a real table whose HEADER cell is itself a decision token (e.g. a metadata
    column literally named "accepted") is not recognized as a header, which can
    false-POSITIVE (fails loud, never silent); a dropped row whose decision is only
    paraphrased — not a recognized `decision_class` token — is not flagged (but it
    is not a valid adjudication decision either); blockquote/list-prefixed rows and
    >3-space-indented pipe rows follow the collector's own pre-existing handling.
    A pipe row WITHOUT a leading `|` (`a | b | c` — GFM-legal but non-idiomatic) is
    invisible to `_pipe_blocks`, so this check never sees it: a body whose only
    table lacks leading pipes fails LOUD ("no table" — fail-safe), but a
    no-leading-pipe decision row mixed alongside a valid leading-pipe table is
    silently dropped. Confirmed intended-no-change (#306, Owner 2026-06-22): the
    real producers emit leading-pipe tables, the pure case already fails loud, and
    widening `_pipe_blocks` to no-leading-pipe lines would enlarge the
    false-positive surface to any prose line containing a pipe.
    """
    orphans: list[tuple[int, str]] = []
    for block in _pipe_blocks(_strip_closed_fences(text)):
        cells = [split_cells(ln) for _, ln in block]
        widths = [len(cs) for cs in cells]
        is_sep = [_looks_like_separator([normalize(c) for c in cs]) for cs in cells]
        has_decision = [any(decision_class(c) is not None for c in cs) for cs in cells]

        def _is_header(idx: int) -> bool:
            # header: a non-separator, non-decision line immediately followed by a
            # separator of matching width (GFM; mirrors the collector's contract).
            return (
                not is_sep[idx]
                and not has_decision[idx]
                and idx + 1 < len(block)
                and is_sep[idx + 1]
                and widths[idx + 1] == widths[idx]
            )

        inside = [False] * len(block)
        i = 0
        while i < len(block):
            if _is_header(i):
                width = widths[i]
                inside[i] = inside[i + 1] = True
                j = i + 2
                # absorb only same-width data rows; a wrong-width row ends the table
                while j < len(block) and not _is_header(j) and widths[j] == width:
                    inside[j] = True
                    j += 1
                i = j
            else:
                i += 1

        orphans.extend(
            block[k]
            for k in range(len(block))
            if not inside[k] and not is_sep[k] and has_decision[k]
        )
    return orphans


def _lax_lost_decision_rows(text: str) -> list[tuple[int, str]]:
    """Decision rows the LAX converter parse silently drops (converter-only).

    The strict gate uses `_orphan_decision_rows`, but the converter's LAX
    `_parse_tables_verbose` captures the FIRST required-superset header in a pipe
    block and every line AFTER it — even without a separator row, and wherever the
    header sits in the block. So the rows it silently discards are: every line of a
    HEADER-LESS block (a blank-line / headerless orphan), AND any line BEFORE the
    header inside a header-bearing block. This returns the `_orphan_decision_rows`
    set restricted to exactly those discarded lines (mirror the parser at ROW
    granularity — header onward = captured), so:
      - a separator-less table (header-bearing, header first) is NOT false-failed
        (audit 82b1979f f2);
      - a pre-header stray decision row IS flagged (audit 698fba2c f1) rather than
        silently lost;
      - placeholders / captions / metadata tables / fenced examples are already
        excluded upstream by `_orphan_decision_rows` (decision-value-anchored region
        detection), so the line restriction cannot resurface them.

    Used by audit_to_adjudication_table.convert() to fail closed on a truly lost
    adjudication row (#306 converter sibling). Residual (inherited from
    `_orphan_decision_rows`'s #339 decision-value anchor): a discarded row whose
    decision is unrecognized (not a `decision_class` token) is not flagged — it is
    not a valid adjudication row either, and broadening to it would re-open the
    structural-heuristic surface #339 deliberately avoided. A cell-count-mismatch
    row AFTER the header is captured here but caught by the converter's CF-3
    `dropped` guard, so it is never silently lost.
    """
    captured_linenos: set[int] = set()
    for block in _pipe_blocks(_strip_closed_fences(text)):
        header_idx = next(
            (i for i, (_, line) in enumerate(block) if _is_required_header(line)),
            None,
        )
        if header_idx is not None:
            captured_linenos.update(lineno for lineno, _ in block[header_idx:])
    return [
        (lineno, line)
        for lineno, line in _orphan_decision_rows(text)
        if lineno not in captured_linenos
    ]


def _collect_tables(
    text: str,
) -> tuple[list[tuple[list[str], list[list[str]]]], list[str]]:
    """Find every strict adjudication table and report shape violations.

    Returns (tables, shape_violations) where each table is (headers, rows):
    - headers are the normalized (lowercased) REQUIRED column names in document
      order;
    - rows preserve original cell case (via normalize_cell) for verbatim render
      and content checks.

    Shape strictness (hard violations, table skipped / row dropped):
    extra columns, duplicate columns, missing separator row, row-cell count
    mismatch. Multi-table aware: scans ALL pipe-line blocks, not just the first
    (GP-03), so a clean decoy cannot mask a later broken table.
    """
    blocks = _pipe_blocks(text)

    tables: list[tuple[list[str], list[list[str]]]] = []
    violations: list[str] = []

    for block in blocks:
        for i, (header_lineno, line) in enumerate(block):
            if not _is_required_header(line):
                continue
            header_cells = [normalize(c) for c in split_cells(line)]
            header_set = set(header_cells)

            extras = sorted(header_set - REQUIRED)
            if extras:
                violations.append(
                    f"header line {header_lineno}: extra columns {extras} "
                    f"(table must have exactly {sorted(REQUIRED)})"
                )
                continue
            if len(header_cells) != len(REQUIRED):
                violations.append(
                    f"header line {header_lineno}: duplicate column names "
                    f"in {header_cells} (table must have exactly {sorted(REQUIRED)})"
                )
                continue

            rest = block[i + 1 :]
            if not rest or not _is_separator(
                [normalize(c) for c in split_cells(rest[0][1])], len(header_cells)
            ):
                violations.append(
                    f"header line {header_lineno}: missing markdown separator "
                    f"row immediately after the header"
                )
                continue

            rows: list[list[str]] = []
            for row_lineno, row_line in rest[1:]:
                raw_cells = split_cells(row_line)
                if _is_separator([normalize(c) for c in raw_cells], len(header_cells)):
                    continue
                if len(raw_cells) != len(header_cells):
                    violations.append(
                        f"row line {row_lineno}: cell count {len(raw_cells)} "
                        f"!= header count {len(header_cells)}"
                    )
                    continue
                rows.append([normalize_cell(c) for c in raw_cells])
            tables.append((header_cells, rows))
            break  # one header per block

    return tables, violations


def _parse_tables_verbose(
    text: str,
) -> list[tuple[list[str], list[list[str]], int]]:
    """LAX extraction that also reports dropped rows. Backs parse_tables.

    Same superset/projection semantics as parse_tables, but each tuple carries a
    third element: the count of non-separator data rows whose cell count did not
    match the header width (silently skipped by the LAX parser). The converter
    uses this count to fail closed instead of shipping a table missing a row
    (CF-3 / issue #306).
    """
    raw_lines = text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in raw_lines:
        stripped = raw.strip()
        if stripped.startswith("|"):
            current.append(stripped)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    parsed: list[tuple[list[str], list[list[str]], int]] = []
    for block in blocks:
        for idx, line in enumerate(block):
            if not _is_required_header(line):
                continue
            headers = [normalize(c) for c in split_cells(line)]
            rows: list[list[str]] = []
            dropped = 0
            for row_line in block[idx + 1 :]:
                cells = split_cells(row_line)
                if _is_separator([normalize(c) for c in cells], len(headers)):
                    continue
                if len(cells) == len(headers):
                    rows.append([normalize_cell(c) for c in cells])
                else:
                    dropped += 1
            parsed.append((headers, rows, dropped))
            break
    return parsed


def parse_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """LAX extraction for normalization (NOT validation).

    Returns every table whose header is a SUPERSET of REQUIRED, as
    (headers, rows): headers keep their original order and include any extra
    columns; rows are aligned to the header width (a row whose cell count does
    not match the header is skipped, not reported). The converter projects each
    row onto REQUIRED_HEADERS and drops extras, so it tolerates reordered /
    extra-column audit output (audit 3f0f4e86 f1).

    This is deliberately distinct from validate_text, which is STRICT (exactly
    REQUIRED, hard-fails extras / mismatches). Normalization tolerates dirty
    input; validation does not. The converter validates its *rendered output*
    with validate_text, so the strict contract still gates what ships.
    """
    return [(headers, rows) for headers, rows, _ in _parse_tables_verbose(text)]


def validate_text(text: str) -> tuple[bool, list[str], dict[str, int]]:
    """Validate all adjudication tables: shape + content.

    Returns (ok, issues, counts). counts tallies accepted / rejected /
    needs-user-decision across every table.
    """
    tables, shape_violations = _collect_tables(text)
    counts = {"accepted": 0, "rejected": 0, "needs-user-decision": 0}

    # A decision-bearing pipe row outside any table region (a stray blank line
    # split the table, a headerless block, or a row above the header) would be
    # silently dropped — flag each so the gate fails instead of passing (#306).
    shape_violations = shape_violations + [
        f"line {lineno}: adjudication row not inside a table — a decision row "
        f"outside a header+separator table is silently dropped (stray blank line "
        f"or missing header?): {line}"
        for lineno, line in _orphan_decision_rows(text)
    ]

    if not tables and not shape_violations:
        return False, ["no table with finding/decision/action/verification headers"], counts

    issues: list[str] = list(shape_violations)
    multi = len(tables) > 1

    for table_num, (headers, rows) in enumerate(tables, start=1):
        prefix = f"table {table_num} " if multi else ""
        if not rows:
            issues.append(f"{prefix}audit adjudication table has no data rows")
            continue
        indexes = {name: headers.index(name) for name in REQUIRED_HEADERS}
        for row_num, row in enumerate(rows, start=1):
            finding = row[indexes["finding"]]
            decision_raw = row[indexes["decision"]]
            action = row[indexes["action"]]
            verification = row[indexes["verification"]]

            if not filled(finding):
                issues.append(f"{prefix}row {row_num}: empty finding")
            cls = decision_class(decision_raw)
            if cls is None:
                issues.append(
                    f"{prefix}row {row_num}: decision must be accepted/rejected/needs-user-decision"
                )
            else:
                counts[cls] += 1
            if not filled(action):
                issues.append(f"{prefix}row {row_num}: action is empty or placeholder")
            if not filled(verification):
                issues.append(f"{prefix}row {row_num}: verification is empty or placeholder")
            if cls == "needs-user-decision" and not needs_user_detail(action, verification):
                issues.append(
                    f"{prefix}row {row_num}: needs-user-decision must name who and what is awaited"
                )

    return not issues, issues, counts


def read_input(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    return sys.stdin.read()


def self_test() -> int:
    valid = """# Review

| finding | decision | action | verification |
|---|---|---|---|
| stale gate | accepted | add strict script | `python3 scripts/check.py` exits 0 |
| broad rewrite | rejected | no action because scope is too broad | diff remains unchanged |
| branch protection | needs-user-decision | wait for Owner branch protection approval | Owner approval recorded before change |
"""
    # content-strictness fixtures (from the gate validator)
    empty_action = valid.replace("add strict script", "TODO", 1)
    vague_user = valid.replace("wait for Owner branch protection approval", "ask someone", 1)
    vague_user = vague_user.replace("Owner approval recorded before change", "record outcome", 1)
    # shape-strictness fixtures (from the skill validator)
    extra_col = valid.replace(
        "| finding | decision | action | verification |",
        "| finding | decision | action | verification | extra |",
        1,
    ).replace("|---|---|---|---|", "|---|---|---|---|---|", 1)
    no_separator = """| finding | decision | action | verification |
| stale gate | accepted | do thing | check passes |
"""
    # alias / Chinese decision must now PASS (merged from skill validator)
    chinese_ok = valid.replace("accepted", "采纳", 1)

    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        for name, text, want_ok in [
            ("valid.md", valid, True),
            ("chinese.md", chinese_ok, True),
            ("empty.md", empty_action, False),
            ("vague.md", vague_user, False),
            ("extra-col.md", extra_col, False),
            ("no-separator.md", no_separator, False),
            ("missing.md", "no table\n", False),
        ]:
            path = tmp / name
            path.write_text(text, encoding="utf-8")
            ok, issues, _ = validate_text(path.read_text(encoding="utf-8"))
            assert ok is want_ok, (name, ok, issues)

    print("OK: validate_audit_adjudication self-test passed")
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
    parser.add_argument("--self-test", action="store_true", help="run offline markdown fixture self-test")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    ok, issues, counts = validate_text(read_input(args.path))
    if ok:
        total = counts["accepted"] + counts["rejected"] + counts["needs-user-decision"]
        print(
            f"OK: audit adjudication table; {total} rows; "
            f"accepted={counts['accepted']} "
            f"rejected={counts['rejected']} "
            f"needs-user-decision={counts['needs-user-decision']}"
        )
        return 0

    print("FAIL: audit adjudication table is incomplete")
    for issue in issues:
        print(f"- {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
