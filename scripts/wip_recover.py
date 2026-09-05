#!/usr/bin/env python3
"""WIP snapshot recoverer — Claude Code SessionStart hook handler (Wave 1 P0 #7).

Triggered by Claude Code on SessionStart event. Scans WIP dir, prunes >7-day-old
snapshots (including corrupt JSON via mtime fallback), prints human-readable recovery
message to **stdout** (which Claude sees as system context).

Default filters by current cwd hash (gemini #2 accepted: prevent cross-project leakage).
--all flag overrides for cross-project view.

Implemented per the triple-audit's 14 accepted findings (audit_id 656913cb):
- Default filter by cwd_sha256_first8 (gemini #2)
- _scan_wip_dir returns [(path, snapshot_dict_or_None)] so prune can delete by path (gpt-5.5 #5)
- Single _default_wip_dir helper shared with wip_save (gpt-5.5 #6)
- schema_version=1 only; everything else skipped + stderr warning (gpt-5.5 #7)
- corrupt JSON pruned via file mtime fallback (o3 #6)
- subprocess timeout=2s (this module does not actually call subprocess; file IO only)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION_ACCEPTED = 1
DEFAULT_MAX_AGE_DAYS = 7

# Post-impl dual-audit gpt-5.5 #4 accepted: guard against unbounded recover
MAX_SNAPSHOT_FILE_BYTES = 64 * 1024  # > 64KB treated as suspicious/corrupt, skip
MAX_DISPLAY_SNAPSHOTS = 8  # don't let SessionStart context flood


# ===== Path resolver (shared with wip_save) =====


def _default_wip_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and xdg.strip():
        return Path(xdg).expanduser() / "aqg" / "wip"
    return Path.home() / ".aqg" / "wip"


def _sha256_first8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


# ===== Scan + parse =====


@dataclass(frozen=True)
class ScannedSnapshot:
    path: Path
    snapshot: dict | None  # None ⇒ corrupt / unreadable
    mtime_iso: str  # for prune fallback


def _scan_wip_dir(wip_dir: Path) -> list[ScannedSnapshot]:
    """Read all *.json files; valid → snapshot dict, corrupt → None.

    Per-file mtime always recorded for prune fallback (o3 #6).
    """
    out: list[ScannedSnapshot] = []
    if not wip_dir.is_dir():
        return out
    try:
        entries = sorted(wip_dir.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            stat = entry.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            mtime_iso = mtime.isoformat()
            file_size = int(stat.st_size)
        except OSError:
            mtime_iso = ""
            file_size = 0
        # Post-impl dual-audit gpt-5.5 #4 accepted: stat-first, skip oversized file
        # to guard against OOM (legit snapshot should be < 1KB)
        if file_size > MAX_SNAPSHOT_FILE_BYTES:
            print(
                f"[wip_recover] skip oversized {_redact_log_path(entry)} "
                f"({file_size} bytes > {MAX_SNAPSHOT_FILE_BYTES})",
                file=sys.stderr,
            )
            # still record path/mtime so prune can delete it
            out.append(ScannedSnapshot(path=entry, snapshot=None, mtime_iso=mtime_iso))
            continue
        snapshot: dict | None
        try:
            with entry.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                snapshot = data
            else:
                snapshot = None
        except (OSError, ValueError, json.JSONDecodeError):
            snapshot = None
        out.append(ScannedSnapshot(path=entry, snapshot=snapshot, mtime_iso=mtime_iso))
    return out


def _redact_log_path(path: Path) -> str:
    """Replace $HOME prefix with ~ for log readability."""
    try:
        home = Path.home()
        rel = path.resolve().relative_to(home.resolve())
        return f"~/{rel}"
    except (ValueError, OSError):
        return path.name


# ===== Prune =====


def _is_too_old(iso_str: str, *, max_age_days: int, now: datetime | None = None) -> bool:
    """Parse ISO datetime; if parse fails or > max_age_days old, return True."""
    if not iso_str:
        return True  # empty / missing → treat as expired (fail-safe)
    try:
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - ts) > timedelta(days=max_age_days)


def _prune_old(
    scanned: list[ScannedSnapshot],
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> tuple[list[ScannedSnapshot], int]:
    """Delete files whose snapshot.saved_at_iso (or mtime fallback) > max_age_days.

    Returns (alive_remaining, pruned_count).
    """
    alive: list[ScannedSnapshot] = []
    pruned = 0
    for entry in scanned:
        # Prefer snapshot iso; fallback file mtime (corrupt JSON)
        iso = entry.snapshot.get("saved_at_iso") if entry.snapshot else None
        if not (isinstance(iso, str) and iso):
            iso = entry.mtime_iso
        if _is_too_old(iso, max_age_days=max_age_days, now=now):
            try:
                entry.path.unlink()
                pruned += 1
            except OSError as exc:
                print(f"[wip_recover] prune failed {entry.path}: {exc}", file=sys.stderr)
                # still treat as dead (don't show old)
        else:
            alive.append(entry)
    return alive, pruned


# ===== Filter =====


def _filter_by_cwd(
    alive: list[ScannedSnapshot], *, cwd_hash: str
) -> list[ScannedSnapshot]:
    """Keep only snapshots whose cwd_sha256_first8 matches current cwd."""
    out: list[ScannedSnapshot] = []
    for entry in alive:
        if entry.snapshot is None:
            continue
        if entry.snapshot.get("cwd_sha256_first8") == cwd_hash:
            out.append(entry)
    return out


def _filter_by_schema(
    alive: list[ScannedSnapshot],
) -> list[ScannedSnapshot]:
    """Drop snapshots whose schema_version is not in accepted set; warn on stderr."""
    out: list[ScannedSnapshot] = []
    for entry in alive:
        if entry.snapshot is None:
            continue
        sv = entry.snapshot.get("schema_version")
        if sv != SCHEMA_VERSION_ACCEPTED:
            print(
                f"[wip_recover] skip {entry.path.name}: schema_version={sv!r} "
                f"(accepted: {SCHEMA_VERSION_ACCEPTED})",
                file=sys.stderr,
            )
            continue
        out.append(entry)
    return out


def _filter_by_redaction(
    alive: list[ScannedSnapshot],
) -> list[ScannedSnapshot]:
    """Drop snapshots that fail _wip_redaction.check_wip_snapshot.

    Post-impl dual-audit gpt-5.5 #1 accepted: when recover reads a snapshot off
    disk it only checks schema_version, not _wip_redaction; an old / malicious /
    corrupt v1 snapshot could inject any string into SessionStart stdout (the
    context Claude sees). Add a belt-and-suspenders second layer of gatekeeping
    here: run a redaction check on each loaded snapshot, and skip any that fail.
    """
    try:
        from _wip_redaction import check_wip_snapshot
    except ImportError as exc:
        print(f"[wip_recover] _wip_redaction unavailable: {exc}", file=sys.stderr)
        return alive  # graceful: don't block recovery on missing module
    out: list[ScannedSnapshot] = []
    for entry in alive:
        if entry.snapshot is None:
            continue
        result = check_wip_snapshot(entry.snapshot)
        if not result.is_safe:
            # Post-impl gpt-5.5 #1: log field names (from the violation), don't echo raw value.
            # WB-04: guard the lazy aqg_doctor import + field-extraction (parity with
            # the _wip_redaction import above) so a missing/broken aqg_doctor — or a
            # raise from _extract_field_from_violation on a malformed violation string
            # — degrades to a generic message instead of crashing the SessionStart
            # recovery path. The skip decision is already made (result.is_safe False),
            # so this purely-cosmetic formatting must never abort recovery (audit
            # 8c0cb0f6 f1: broaden ImportError → Exception).
            try:
                from aqg_doctor import _extract_field_from_violation
                fields = sorted({_extract_field_from_violation(v) for v in result.violations})
                detail = f"in fields: {', '.join(fields)}"
            except Exception:  # aqg: top-level boundary — diagnostic formatting only
                detail = f"({len(result.violations)} violation(s))"
            print(
                f"[wip_recover] skip {entry.path.name}: redaction check failed {detail}",
                file=sys.stderr,
            )
            continue
        out.append(entry)
    return out


# ===== Format recovery message =====


def _format_recovery_message(
    snapshots: list[dict], *, cwd_hash: str, all_projects: bool
) -> str:
    """Build human-readable stdout message; '' if no snapshots.

    Post-impl dual-audit gpt-5.5 #4 accepted: cap at MAX_DISPLAY_SNAPSHOTS,
    summarize the rest as "+N more" to prevent SessionStart context flood.
    """
    if not snapshots:
        return ""
    total = len(snapshots)
    truncated = snapshots[:MAX_DISPLAY_SNAPSHOTS]
    omitted = total - len(truncated)
    scope = "across all projects" if all_projects else f"in current project (cwd hash {cwd_hash})"
    lines = [
        "=== AQG WIP Recovery ===",
        f"Found {total} unfinalized session(s) {scope}:",
        "",
    ]
    for snap in truncated:
        sid = snap.get("session_id", "?")
        saved = snap.get("saved_at_iso", "?")
        cwd_status = snap.get("cwd_status") or {}
        gbs = cwd_status.get("git_branch_status", "?")
        is_default = cwd_status.get("git_is_default_branch")
        branch_label = (
            "default-branch" if is_default else "non-default-branch" if is_default is False else "?"
        )
        dirty = cwd_status.get("git_dirty")
        modified = cwd_status.get("modified_files_count", "?")
        untracked = cwd_status.get("untracked_files_count", "?")
        ahead = cwd_status.get("git_ahead_count", "?")
        behind = cwd_status.get("git_behind_count", "?")
        todo = snap.get("todo_state") or {}
        in_progress = todo.get("in_progress_count", "?")
        items_count = todo.get("items_count", "?")
        pr_count = snap.get("recent_open_pr_count")

        lines.append(f"- session {sid} (saved {saved})")
        lines.append(
            f"  branch: {gbs}/{branch_label}  dirty={dirty}  modified={modified} "
            f"untracked={untracked}  ahead/behind={ahead}/{behind}"
        )
        lines.append(f"  todos: {items_count} total, {in_progress} in_progress")
        if pr_count is not None:
            lines.append(f"  open_PRs: {pr_count}")
    if omitted > 0:
        lines.append("")
        lines.append(f"... and {omitted} more snapshot(s) omitted (showing newest {MAX_DISPLAY_SNAPSHOTS})")
    lines.append("")
    lines.append(
        "If resuming this work, ack and continue. If finalized in another session,"
    )
    lines.append("  rm <path>  (paths in stderr above)")
    lines.append("========================")
    return "\n".join(lines)


# ===== Main =====


def main(
    argv: list[str] | None = None,
    *,
    wip_dir: Path | None = None,
    cwd: Path | None = None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="AQG WIP snapshot recoverer (SessionStart hook).")
    parser.add_argument("--all", action="store_true", help="show snapshots from all projects (not just current cwd)")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    args = parser.parse_args(argv)

    target_dir = wip_dir or _default_wip_dir()
    if cwd is None:
        # cwd resolution priority: AQG_HOOK_PROJECT_DIR (from bash wrapper /
        # CLAUDE_PROJECT_DIR) > Path.cwd() fallback.
        env_cwd = os.environ.get("AQG_HOOK_PROJECT_DIR", "").strip()
        if env_cwd:
            try:
                env_path = Path(env_cwd).expanduser()
                if env_path.is_dir():
                    cwd = env_path
            except (OSError, ValueError):
                pass
        if cwd is None:
            cwd = Path.cwd()
    cwd_path = cwd
    cwd_hash = _sha256_first8(str(cwd_path.resolve()))

    # 1. Scan
    try:
        scanned = _scan_wip_dir(target_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[wip_recover] scan failed: {exc}", file=sys.stderr)
        return 0

    # 2. Prune old (including corrupt JSON via mtime fallback)
    alive, pruned = _prune_old(scanned, max_age_days=args.max_age_days, now=now)
    if pruned:
        print(f"[wip_recover] pruned {pruned} old snapshot(s)", file=sys.stderr)

    # 3. Filter schema (drop unknown versions + warn)
    alive = _filter_by_schema(alive)

    # 4. Filter by redaction (post-impl dual-audit gpt-5.5 #1: guard against inject)
    alive = _filter_by_redaction(alive)

    # 5. Filter cwd (default unless --all)
    if not args.all:
        alive = _filter_by_cwd(alive, cwd_hash=cwd_hash)

    # 6. stderr emit paths for user reference (post-impl dual-audit gpt-5.5 #5: redact)
    for entry in alive:
        print(f"[wip_recover] alive snapshot: {_redact_log_path(entry.path)}", file=sys.stderr)

    # 6. Format + print to stdout (SessionStart allows stdout for Claude to see)
    snapshots = [e.snapshot for e in alive if e.snapshot is not None]
    msg = _format_recovery_message(snapshots, cwd_hash=cwd_hash, all_projects=args.all)
    if msg:
        print(msg)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
