#!/usr/bin/env python3
"""Incident record CLI + INDEX.md generator (Wave 3 Layer 3 incident index v0).

Implements ENGINEERING_FRAMEWORK.md §2 (review-time analyzer: incident markdown index) + §5 (Layer 3).

Two subcommands:
    record   - Fill template + write to docs/incidents/<date>-<slug>.md
    index    - Scan docs/incidents/ -> generate docs/incidents/INDEX.md

CLI:
    # Record (LLM mode, JSON via stdin)
    echo '{"slug":"...","title":"...",...}' | python3 scripts/aqg_incident_index.py record --json

    # Record (args mode)
    python3 scripts/aqg_incident_index.py record \\
        --slug short-slug --title "Short title" --severity P3 \\
        --detection-source manual --impact-scope dev --actor claude \\
        --summary "..." --root-cause "..." --resolution "..." \\
        --boundaries "..." [--followups "item1,item2"] \\
        [--audit-id 8hex] [--pr-url https://github.com/...] \\
        [--date YYYY-MM-DD] [--force]

    # Index
    python3 scripts/aqg_incident_index.py index \\
        [--dir docs/incidents] [--output docs/incidents/INDEX.md]

Exit codes:
    0 OK
    1 redaction or write fail / parse error
    2 usage error
    3 file exists (record without --force)

No third-party dependencies; stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXIT_OK = 0
EXIT_REDACTION_FAIL = 1
EXIT_USAGE = 2
EXIT_FILE_EXISTS = 3

INCIDENT_DIR_REL = "docs/incidents"
INDEX_FILE_NAME = "INDEX.md"

# a2 audit #3: filename whitelist for indexer (date-slug or date-slug-aN.md)
INCIDENT_FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9._-]{0,79}(?:-a\d{1,3})?\.md$"
)


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


# ===== Record building =====


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _build_record_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Build record dict from argparse args (record subcommand args mode)."""
    followups: list[str] = []
    if args.followups:
        followups = [s.strip() for s in args.followups.split(",") if s.strip()]
    return {
        "schema_version": 1,
        "slug": args.slug,
        "date": args.date or _today_utc(),
        "actor": args.actor,
        "title": args.title,
        "severity": args.severity,
        "detection_source": args.detection_source,
        "impact_scope": args.impact_scope,
        "summary": args.summary,
        "root_cause": args.root_cause,
        "resolution": args.resolution,
        "followups": followups,
        "boundaries": args.boundaries,
        "audit_id": args.audit_id or "",
        "pr_url": args.pr_url or "",
        "marker": "auto-generated-by-aqg_incident_index",
    }


def _render_markdown(record: dict[str, Any]) -> str:
    """Render record dict to markdown matching templates/incident_record_template.md."""
    followups = record.get("followups") or []
    if followups:
        followup_lines = "\n".join(f"- {item}" for item in followups)
    else:
        followup_lines = "- (none)"

    audit_id = record.get("audit_id") or ""
    pr_url = record.get("pr_url") or ""

    return (
        "<!--\n"
        "AQG_INCIDENT_RECORD\n"
        f"schema_version: {record['schema_version']}\n"
        f"slug: {record['slug']}\n"
        f"date: {record['date']}\n"
        f"actor: {record['actor']}\n"
        f"severity: {record['severity']}\n"
        f"detection_source: {record['detection_source']}\n"
        f"impact_scope: {record['impact_scope']}\n"
        f"audit_id: {audit_id}\n"
        f"pr_url: {pr_url}\n"
        f"marker: {record.get('marker', 'auto-generated-by-aqg_incident_index')}\n"
        "-->\n\n"
        f"# Incident Record: {record['title']}\n\n"
        f"- date: {record['date']}\n"
        f"- severity: {record['severity']}\n"
        f"- detection source: {record['detection_source']}\n"
        f"- impact scope: {record['impact_scope']}\n\n"
        "## Summary\n\n"
        f"{record['summary']}\n\n"
        "## Root Cause\n\n"
        f"{record['root_cause']}\n\n"
        "## Resolution\n\n"
        f"{record['resolution']}\n\n"
        "## Followups\n\n"
        f"{followup_lines}\n\n"
        "## Boundaries\n\n"
        f"{record['boundaries']}\n"
    )


# ===== record subcommand =====


def _atomic_write(path: Path, body: str) -> None:
    """Write text atomically (DR-05/06): a crash mid-write must not leave a
    truncated incident record / INDEX.md. A unique same-dir tempfile (mkstemp =
    O_CREAT|O_EXCL, no name collision / no symlink-follow) is written then
    os.replace'd (atomic on the same filesystem)."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _cmd_record(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root()

    # Build record from JSON stdin or args
    if args.json:
        try:
            record = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"ERROR: invalid JSON on stdin: {exc}", file=sys.stderr)
            return EXIT_USAGE
        # Fill defaults that JSON may omit
        record.setdefault("schema_version", 1)
        record.setdefault("date", _today_utc())
        record.setdefault("marker", "auto-generated-by-aqg_incident_index")
        record.setdefault("followups", [])
        record.setdefault("audit_id", "")
        record.setdefault("pr_url", "")
    else:
        # All args mode requires the named flags
        required_args = (
            "slug", "title", "severity", "detection_source", "impact_scope",
            "actor", "summary", "root_cause", "resolution", "boundaries",
        )
        missing = [a for a in required_args if not getattr(args, a, None)]
        if missing:
            print(
                f"ERROR: missing required args: {', '.join('--' + a.replace('_','-') for a in missing)}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        record = _build_record_from_args(args)

    # Redaction guard (validate schema + redact patterns)
    sys.path.insert(0, str(repo_root / "scripts"))
    from _incident_redaction import (  # noqa: E402
        IncidentRedactionError,
        assert_safe_incident_record,
    )
    try:
        assert_safe_incident_record(record)
    except IncidentRedactionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_REDACTION_FAIL

    # Write to docs/incidents/<date>-<slug>.md
    target_dir = repo_root / INCIDENT_DIR_REL
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{record['date']}-{record['slug']}.md"

    body = _render_markdown(record)

    # a2 audit #4: atomic exclusive create (O_EXCL) instead of exists-then-write race.
    # If --force, plain write_text (truncate). Else, try open('x') and on
    # FileExistsError advance to next -aN.md suffix.
    if args.force:
        try:
            _atomic_write(target_path, body)
        except OSError as exc:
            print(f"ERROR: write failed: {exc}", file=sys.stderr)
            return EXIT_REDACTION_FAIL
    else:
        suffix_n = 0  # 0 = base filename, then -a1.md, -a2.md ...
        while True:
            if suffix_n == 0:
                candidate = target_dir / f"{record['date']}-{record['slug']}.md"
            else:
                candidate = target_dir / f"{record['date']}-{record['slug']}-a{suffix_n}.md"
            try:
                with open(candidate, "x", encoding="utf-8") as fh:
                    fh.write(body)
                target_path = candidate
                break
            except FileExistsError:
                suffix_n += 1
                if suffix_n > 100:
                    print(
                        f"ERROR: too many suffixed files for {record['slug']} "
                        f"on {record['date']} (>100)",
                        file=sys.stderr,
                    )
                    return EXIT_FILE_EXISTS
            except OSError as exc:
                print(f"ERROR: write failed: {exc}", file=sys.stderr)
                return EXIT_REDACTION_FAIL

    print(f"OK: wrote {target_path.relative_to(repo_root)}")
    return EXIT_OK


# ===== index subcommand =====


# Match the AQG_INCIDENT_RECORD HTML metadata block at the top of incident docs.
_METADATA_BLOCK_RE = re.compile(
    r"<!--\s*\n"
    r"AQG_INCIDENT_RECORD\s*\n"
    r"(?P<body>(?:.*?\n)*?)"
    r"-->",
    re.MULTILINE,
)
_METADATA_LINE_RE = re.compile(r"^\s*([a-z_]+)\s*:\s*(.*?)\s*$")


def _parse_metadata(text: str) -> dict[str, str] | None:
    """Parse AQG_INCIDENT_RECORD HTML metadata block. Returns None if missing."""
    m = _METADATA_BLOCK_RE.search(text)
    if not m:
        return None
    metadata: dict[str, str] = {}
    for line in m.group("body").splitlines():
        line = line.strip()
        if not line:
            continue
        lm = _METADATA_LINE_RE.match(line)
        if lm:
            metadata[lm.group(1)] = lm.group(2)
    return metadata


def _extract_title(text: str) -> str:
    """Extract '# Incident Record: <title>' from markdown body."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# Incident Record:"):
            return line.split(":", 1)[1].strip()
        if line.startswith("# "):
            # fall back to any H1 if marker variant
            return line[2:].strip()
    return "(untitled)"


def _scan_incidents(incidents_dir: Path) -> list[dict[str, str]]:
    """Scan incidents_dir for markdown files; return list of metadata dicts.

    Skips INDEX.md, README.md, files starting with '_' (sample / docs), and
    any filename that does not match INCIDENT_FILENAME_RE whitelist
    (a2 audit #3 — filename whitelist defends against link-injection in INDEX.md).

    Also schema-validates parsed metadata via check_incident_record;
    files with malformed metadata (e.g. injected enum values, bad date)
    are skipped silently — they do not appear in the index.
    """
    records: list[dict[str, str]] = []
    if not incidents_dir.is_dir():
        return records
    # Lazy import to avoid circular when used as a library
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _incident_redaction import check_incident_record  # noqa: E402
    from _redaction_common import scan_leaks  # noqa: E402

    for path in sorted(incidents_dir.glob("*.md")):
        name = path.name
        if name in (INDEX_FILE_NAME, "README.md"):
            continue
        if name.startswith("_"):
            continue
        # a2 audit #3: filename must match the canonical incident pattern.
        if not INCIDENT_FILENAME_RE.match(name):
            continue
        # INC-TITLE-LEAK sibling (audit 42209122): the filename is rendered as the
        # INDEX.md link target/label; the charset whitelist still admits a lowercase
        # token-shaped slug (e.g. ghp_<20 lowercase>), so leak-scan it before indexing.
        _fn_violations: list[str] = []
        scan_leaks("_filename", name, _fn_violations, multiline=False)
        if _fn_violations:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        metadata = _parse_metadata(text)
        if metadata is None:
            # Not an AQG incident record (no metadata block) — skip silently
            continue

        # a2 audit #3: validate metadata against schema before indexing.
        # Build a minimal record dict for the schema check; missing required
        # body fields (summary/root_cause/...) are not in the metadata block,
        # so we provide safe placeholder strings ONLY for the validator —
        # the validator's enum/regex/date checks on metadata fields are the
        # primary defense; the placeholder fills body-only required fields.
        # INC-TITLE-LEAK: validate the REAL H1 title (rendered verbatim into the
        # git-committed INDEX.md, md-cell-escaped only), not a placeholder — the
        # record-write guard assert_safe_incident_record does not run on this
        # re-scan path, so a secret in the H1 would otherwise reach INDEX.md.
        real_title = _extract_title(text)
        validation_input = {
            "schema_version": _safe_int(metadata.get("schema_version")),
            "slug": metadata.get("slug", ""),
            "date": metadata.get("date", ""),
            "actor": metadata.get("actor", ""),
            "title": real_title,
            "severity": metadata.get("severity", ""),
            "detection_source": metadata.get("detection_source", ""),
            "impact_scope": metadata.get("impact_scope", ""),
            "summary": "placeholder summary",
            "root_cause": "placeholder",
            "resolution": "placeholder",
            "followups": [],
            "boundaries": "placeholder",
        }
        if metadata.get("audit_id"):
            validation_input["audit_id"] = metadata["audit_id"]
        if metadata.get("pr_url"):
            validation_input["pr_url"] = metadata["pr_url"]
        if metadata.get("marker"):
            validation_input["marker"] = metadata["marker"]
        result = check_incident_record(validation_input)
        if not result.is_safe:
            # Bad metadata fields (bad enum, malformed date, etc.) — skip
            continue

        metadata["_filename"] = name
        metadata["_title"] = real_title
        records.append(metadata)
    return records


def _safe_int(value: Any) -> Any:
    """Best-effort cast to int for metadata-string values; non-numeric kept as-is
    so the schema validator surfaces a clear violation."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return value


def _md_cell_escape(value: str) -> str:
    """Escape a string for safe inclusion in a markdown table cell.

    a2 audit #3: escape pipe (|) and bracket (][) which can break table
    columns or inject inline links; also escape backtick to prevent code
    span hijack and backslash to keep the escapes literal.
    """
    if value is None:
        return ""
    out = str(value)
    # Order matters: backslash first so subsequent escapes are not double-escaped
    out = out.replace("\\", "\\\\")
    out = out.replace("|", "\\|")
    out = out.replace("`", "\\`")
    out = out.replace("[", "\\[").replace("]", "\\]")
    return out


def _render_index(records: list[dict[str, str]]) -> str:
    """Render INDEX.md table sorted by date desc then slug."""
    sorted_records = sorted(
        records,
        key=lambda r: (r.get("date", ""), r.get("slug", "")),
        reverse=True,
    )
    header = (
        "# Incident Index\n\n"
        f"Generated by `scripts/aqg_incident_index.py index` "
        f"({len(sorted_records)} incident{'s' if len(sorted_records) != 1 else ''}).\n\n"
    )
    if not sorted_records:
        return header + "_No incidents recorded yet._\n"

    lines = [
        "| date | severity | scope | source | title | file |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted_records:
        # a2 audit #3: every cell escaped; filename URL-encoded for the link target.
        # _filename was already filename-whitelisted by _scan_incidents (INCIDENT_FILENAME_RE),
        # but URL-encode preserves correct target if a future regex relaxation introduces special chars.
        fname = r.get("_filename", "")
        link_target = urllib.parse.quote(fname, safe="-_.")
        link_label = _md_cell_escape(fname)
        lines.append(
            "| {date} | {sev} | {scope} | {src} | {title} | [{label}]({target}) |".format(
                date=_md_cell_escape(r.get("date", "")),
                sev=_md_cell_escape(r.get("severity", "")),
                scope=_md_cell_escape(r.get("impact_scope", "")),
                src=_md_cell_escape(r.get("detection_source", "")),
                title=_md_cell_escape(r.get("_title", "")),
                label=link_label,
                target=link_target,
            )
        )
    return header + "\n".join(lines) + "\n"


def _cmd_index(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root()

    # a2 audit #5: relative --dir is resolved against repo_root (not cwd) for
    # consistency with --output; absolute paths are used as-is. If the directory
    # doesn't exist we fail clearly rather than silently producing an empty index.
    incidents_dir = Path(args.dir) if args.dir else repo_root / INCIDENT_DIR_REL
    if not incidents_dir.is_absolute():
        incidents_dir = repo_root / incidents_dir
    if not incidents_dir.is_dir():
        print(
            f"ERROR: incidents directory does not exist: {incidents_dir}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    output_path = (
        Path(args.output)
        if args.output
        else incidents_dir / INDEX_FILE_NAME
    )
    if not output_path.is_absolute():
        output_path = repo_root / output_path

    records = _scan_incidents(incidents_dir)
    body = _render_index(records)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_write(output_path, body)
    except OSError as exc:
        print(f"ERROR: write failed: {exc}", file=sys.stderr)
        return EXIT_REDACTION_FAIL

    try:
        display_path = output_path.relative_to(repo_root)
    except ValueError:
        # --output may point outside repo_root (e.g. /tmp in tests)
        display_path = output_path
    print(
        f"OK: wrote {display_path} "
        f"({len(records)} incident{'s' if len(records) != 1 else ''})"
    )
    return EXIT_OK


# ===== CLI =====


def _add_record_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "record",
        help="Create a new incident record under docs/incidents/",
    )
    p.add_argument("--json", action="store_true", help="Read record dict from stdin as JSON")
    p.add_argument("--slug", help="short slug, [a-z0-9][a-z0-9._-]{0,79}")
    p.add_argument("--title", help="short title (max 100 chars, single-line)")
    p.add_argument(
        "--severity", choices=("P1", "P2", "P3", "P4"), help="incident severity"
    )
    p.add_argument(
        "--detection-source",
        choices=("monitoring", "user_report", "scheduled_check", "audit", "manual"),
        help="how the incident was detected",
    )
    p.add_argument(
        "--impact-scope",
        choices=("production", "staging", "internal", "dev"),
        help="impact scope",
    )
    p.add_argument("--actor", help="who recorded (claude / codex / human / ci-bot / ...)")
    p.add_argument("--summary", help="single-line incident summary")
    p.add_argument("--root-cause", help="root cause prose")
    p.add_argument("--resolution", help="resolution prose")
    p.add_argument(
        "--followups",
        help="comma-separated followup items (each <300 chars, no newlines)",
    )
    p.add_argument("--boundaries", help="boundaries prose")
    p.add_argument("--audit-id", help="optional 8-hex audit id")
    p.add_argument("--pr-url", help="optional GitHub PR URL")
    p.add_argument("--date", help="override date (YYYY-MM-DD), default today UTC")
    p.add_argument("--force", action="store_true", help="overwrite existing file")
    p.set_defaults(func=_cmd_record)


def _add_index_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "index",
        help="Scan docs/incidents/*.md and generate INDEX.md",
    )
    p.add_argument(
        "--dir",
        help="incidents directory (default: docs/incidents/ from repo root)",
    )
    p.add_argument(
        "--output",
        help="output file (default: <dir>/INDEX.md)",
    )
    p.set_defaults(func=_cmd_index)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aqg_incident_index",
        description="AQG incident record + index CLI (Wave 3 Layer 3 v0).",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    _add_record_parser(subparsers)
    _add_index_parser(subparsers)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
