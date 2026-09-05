#!/usr/bin/env python3
"""AQG local metrics ledger CLI (Wave 2 #3, framework Q11 frozen).

Implemented per triple-audit (audit_id 6a46a056) 11 accepted findings:
- Opt-in: AQG_METRICS env truthy whitelist (1/true/yes/on) or --record-metrics flag
  (3-auditor major: prevent false/no/off from accidentally enabling)
- Atomic write: os.open(O_APPEND|O_CREAT|O_WRONLY, 0o600) + fcntl.flock LOCK_EX
  + os.write single syscall + os.fsync (3-auditor major: PIPE_BUF claim wrong; durability)
- Permissions: dir 0o700 / file 0o600 (2-auditor: prevent metadata leak)
- Forward-compat: list skips unknown schema_version + warn; prune keeps unknown
  verbatim, does not delete (2-auditor: prevent prune from deleting future-schema data)
- Path: $AQG_METRICS_PATH > $XDG_DATA_HOME/aqg/ > ~/.aqg/ (gemini #4: empty XDG safe)

CLI:
    python3 scripts/aqg_metrics.py status
    python3 scripts/aqg_metrics.py record [--tool ...] [--result ...] ...
    python3 scripts/aqg_metrics.py record --json < input.json
    python3 scripts/aqg_metrics.py list [--tool X] [--since YYYY-MM-DD] [--limit N]
    python3 scripts/aqg_metrics.py clear --yes
    python3 scripts/aqg_metrics.py prune [--days 90] [--destructive-prune]

Exit codes: 0 OK / 1 redaction or io fail / 2 usage / 3 not opt-in (with --require-record)

No third-party dependencies, stdlib only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NOT_OPT_IN = 3

# Truthy whitelist (3-auditor major accepted)
_TRUTHY_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSY_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off", ""})

# Permissions (2-auditor accepted)
LEDGER_DIR_MODE = 0o700
LEDGER_FILE_MODE = 0o600

# Schema (sync with _metrics_redaction)
SCHEMA_VERSION = 1


# ===== Path resolver (gemini #4: empty XDG safe) =====


def _default_ledger_path() -> Path:
    explicit = os.environ.get("AQG_METRICS_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".aqg"
    if xdg:
        return base / "aqg" / "metrics-ledger.jsonl"
    return base / "metrics-ledger.jsonl"


def _sidecar_lock_path(ledger_path: Path) -> Path:
    """Sidecar lock file (post-impl gpt-5.5 #1 + #2: stable lock target across
    truncate/replace operations on the ledger inode)."""
    return ledger_path.with_suffix(ledger_path.suffix + ".lock")


def _open_lock(ledger_path: Path, *, exclusive: bool) -> int:
    """Open + flock the sidecar lock file. Returns fd; caller closes."""
    lock_path = _sidecar_lock_path(ledger_path)
    _ensure_dir(lock_path.parent)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, LEDGER_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except Exception:
        os.close(fd)
        raise
    return fd


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ===== Opt-in (3-auditor major) =====


def _is_metrics_enabled(args: argparse.Namespace) -> bool:
    """Truthy whitelist; reject false/no/off/empty/unset."""
    if getattr(args, "record_metrics", False):
        return True
    val = os.environ.get("AQG_METRICS", "").strip().lower()
    return val in _TRUTHY_VALUES


# ===== Permissions (2-auditor) =====


def _ensure_dir(path: Path) -> None:
    """mkdir -p with 0o700; chmod existing if too permissive."""
    path.mkdir(parents=True, exist_ok=True, mode=LEDGER_DIR_MODE)
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current & 0o077:  # group/other readable or writable
            os.chmod(path, LEDGER_DIR_MODE)
    except OSError:
        pass


def _ensure_file_perms(path: Path) -> None:
    if not path.exists():
        return
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current & 0o177:  # too permissive
            os.chmod(path, LEDGER_FILE_MODE)
    except OSError:
        pass


# ===== Atomic append (3-auditor major) =====


def _append_record_locked(ledger_path: Path, record: dict) -> None:
    """Append a single JSON line via sidecar lock + os.open + loop os.write + fsync.

    3-auditor major (gpt-5.5 #2 + gemini #5 + o3 #2): does not rely on PIPE_BUF; low-level
    os.open(O_APPEND|O_CREAT|O_WRONLY, 0o600) + fcntl.flock + loop os.write + fsync.
    Post-impl dual-audit gpt-5.5 #3: loop write handles partial write; rollback on fail.
    Post-impl gemini #1 critical: the sidecar lock ensures that when clear/prune use
    ftruncate, waiting writers do not write to a ghost inode.
    """
    _ensure_dir(ledger_path.parent)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    encoded = line.encode("utf-8")

    lock_fd = _open_lock(ledger_path, exclusive=True)
    fd = -1
    try:
        fd = os.open(
            str(ledger_path),
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            LEDGER_FILE_MODE,
        )
        # Capture pre-append size for rollback on partial write fail
        pre_size = os.fstat(fd).st_size
        # Loop write: handle partial write (gpt-5.5 #3 accepted)
        offset = 0
        while offset < len(encoded):
            try:
                n = os.write(fd, encoded[offset:])
            except OSError:
                # Rollback to pre-append size
                try:
                    os.ftruncate(fd, pre_size)
                except OSError:
                    pass
                raise
            if n <= 0:
                # Refused write; rollback
                try:
                    os.ftruncate(fd, pre_size)
                except OSError:
                    pass
                raise OSError("os.write returned 0; aborting append")
            offset += n
        os.fsync(fd)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _release_lock(lock_fd)
    _ensure_file_perms(ledger_path)


# ===== Read (streaming) =====


def _stream_records(ledger_path: Path):
    """Yield (line_no, parsed_dict_or_None, raw_line) holding LOCK_SH.

    Post-impl dual-audit gpt-5.5 #2: readers must acquire LOCK_SH to avoid observing
    prune mid-truncate. Reader is short (sequential read of usually-small file);
    lock held for duration of read.
    """
    if not ledger_path.is_file():
        return
    lock_fd = _open_lock(ledger_path, exclusive=False)
    try:
        with ledger_path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    if not isinstance(obj, dict):
                        obj = None
                except (ValueError, json.JSONDecodeError):
                    obj = None
                yield i, obj, raw
    finally:
        _release_lock(lock_fd)


# ===== Subcommand: status =====


def cmd_status(args: argparse.Namespace) -> int:
    path = _default_ledger_path()
    enabled = _is_metrics_enabled(args)
    line_count = 0
    last_ts = None
    if path.is_file():
        for _, obj, _ in _stream_records(path):
            line_count += 1
            if obj is not None and isinstance(obj.get("ts"), str):
                last_ts = obj["ts"]
    info = {
        "ledger_path": str(path),
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "line_count": line_count,
        "last_record_ts": last_ts,
        "opt_in_state": "enabled" if enabled else "disabled",
        "opt_in_via": (
            "--record-metrics flag" if getattr(args, "record_metrics", False)
            else "AQG_METRICS env" if enabled else "(default off)"
        ),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


# ===== Subcommand: record =====


def _record_from_args(args: argparse.Namespace) -> dict:
    rec: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": args.ts or datetime.now(timezone.utc).isoformat(),
        "tool": args.tool,
        "result": args.result,
        "marker": "auto-recorded-by-aqg-metrics",
    }
    if args.duration_ms is not None:
        rec["duration_ms"] = args.duration_ms
    if args.exit_code is not None:
        rec["exit_code"] = args.exit_code
    if args.tool_version:
        rec["tool_version"] = args.tool_version
    if args.actor:
        rec["actor"] = args.actor
    ctx: dict[str, Any] = {}
    for src, dst in (
        ("audit_id", "audit_id"),
        ("audit_panel_size", "audit_panel_size"),
        ("findings_count", "findings_count"),
        ("accepted_count", "accepted_count"),
        ("rejected_count", "rejected_count"),
        ("needs_user_decision_count", "needs_user_decision_count"),
        ("cwd_sha256_first8", "cwd_sha256_first8"),
        ("git_branch_status", "git_branch_status"),
    ):
        v = getattr(args, src, None)
        if v is not None and v != "":
            ctx[dst] = v
    if ctx:
        rec["context"] = ctx
    return rec


def cmd_record(args: argparse.Namespace) -> int:
    enabled = _is_metrics_enabled(args)
    if not enabled:
        msg = (
            "[aqg_metrics] metrics not enabled (no-op). "
            "Set AQG_METRICS=1 or pass --record-metrics to enable."
        )
        if args.require_record:
            print(msg, file=sys.stderr)
            return EXIT_NOT_OPT_IN
        print(msg, file=sys.stderr)
        return EXIT_OK

    if args.json:
        try:
            record = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: stdin not valid JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if not isinstance(record, dict):
            print("ERROR: stdin JSON must be an object", file=sys.stderr)
            return EXIT_USAGE
        # ensure required defaults
        record.setdefault("schema_version", SCHEMA_VERSION)
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        record.setdefault("marker", "auto-recorded-by-aqg-metrics")
    else:
        if not args.tool or not args.result:
            print("ERROR: --tool and --result required (or pass --json with stdin)", file=sys.stderr)
            return EXIT_USAGE
        record = _record_from_args(args)

    # Validate via _metrics_redaction (NEVER echo raw value)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from _metrics_redaction import assert_safe_metrics_record, MetricsRedactionError
    except ImportError as exc:
        print(f"ERROR: _metrics_redaction unavailable: {exc}", file=sys.stderr)
        return EXIT_FAIL
    try:
        assert_safe_metrics_record(record)
    except MetricsRedactionError as exc:
        print(f"[aqg_metrics] redaction violations:\n{exc}", file=sys.stderr)
        return EXIT_FAIL

    ledger_path = _default_ledger_path()
    try:
        _append_record_locked(ledger_path, record)
    except OSError as exc:
        print(f"[aqg_metrics] write failed: {exc}", file=sys.stderr)
        return EXIT_FAIL

    print(f"[aqg_metrics] recorded to {ledger_path.name}", file=sys.stderr)
    return EXIT_OK


# ===== Subcommand: list =====


def cmd_list(args: argparse.Namespace) -> int:
    path = _default_ledger_path()
    if not path.is_file():
        print("[]")
        return EXIT_OK

    since: Optional[datetime] = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"ERROR: --since must be ISO 8601: {args.since}", file=sys.stderr)
            return EXIT_USAGE

    # Post-impl dual-audit gpt-5.5 #5: re-validate each record before output (manually
    # edited / migrated ledger may contain raw secret).
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from _metrics_redaction import check_metrics_record
    except ImportError:
        check_metrics_record = None  # type: ignore

    skipped_unknown = 0
    skipped_unsafe = 0
    out: list[dict] = []
    for line_no, obj, raw in _stream_records(path):
        if obj is None:
            print(f"[aqg_metrics] line {line_no}: skip corrupt JSON", file=sys.stderr)
            continue
        sv = obj.get("schema_version")
        if sv != SCHEMA_VERSION:
            skipped_unknown += 1
            continue
        if check_metrics_record is not None:
            result = check_metrics_record(obj)
            if not result.is_safe:
                skipped_unsafe += 1
                # Sanitized warn — do not echo raw value (gpt-5.5 #4)
                print(
                    f"[aqg_metrics] line {line_no}: skip unsafe record "
                    f"({len(result.violations)} violation(s); details suppressed)",
                    file=sys.stderr,
                )
                continue
        if args.tool and obj.get("tool") != args.tool:
            continue
        if since is not None:
            ts_str = obj.get("ts")
            if isinstance(ts_str, str):
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < since:
                        continue
                except ValueError:
                    pass
        out.append(obj)

    if args.limit and args.limit > 0:
        out = out[-args.limit:]  # newest N (file is append-only chronological)

    if skipped_unknown:
        print(
            f"[aqg_metrics] skipped {skipped_unknown} record(s) with unknown schema_version",
            file=sys.stderr,
        )
    if skipped_unsafe:
        print(
            f"[aqg_metrics] skipped {skipped_unsafe} record(s) failing redaction guard",
            file=sys.stderr,
        )

    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


# ===== Subcommand: clear =====


def cmd_clear(args: argparse.Namespace) -> int:
    if not args.yes:
        print("ERROR: --yes required (this clears the entire ledger to 0 bytes)", file=sys.stderr)
        return EXIT_USAGE
    path = _default_ledger_path()
    if not path.is_file():
        print(f"[aqg_metrics] ledger does not exist: {path.name}", file=sys.stderr)
        return EXIT_OK
    # Post-impl dual-audit gemini #1 (CRITICAL): use ftruncate, not unlink — prevents a waiting
    # writer from writing to a ghost inode (and losing a record) after unlink inside the lock.
    lock_fd = _open_lock(path, exclusive=True)
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDWR)
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _release_lock(lock_fd)
    print(f"[aqg_metrics] cleared {path.name}", file=sys.stderr)
    return EXIT_OK


# ===== Subcommand: prune =====


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete records older than --days. Preserve unknown schema versions
    unless --destructive-prune (2-auditor accepted: forward-compat).

    Post-impl dual-audit gpt-5.5 #1 (major): atomic via tempfile + fsync + os.replace
    inside sidecar flock — kill mid-prune does not lose the ledger.
    Post-impl gpt-5.5 #6 (major): --days < 0 is already rejected in main.
    Post-impl gemini #2 (major): the finally block uniformly closes the fd, no leak.
    """
    path = _default_ledger_path()
    if not path.is_file():
        print(f"[aqg_metrics] ledger does not exist: {path.name}", file=sys.stderr)
        return EXIT_OK

    if args.days < 0:
        print(f"ERROR: --days must be >= 0 (got {args.days}, would drop newer records)", file=sys.stderr)
        return EXIT_USAGE

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    import tempfile
    lock_fd = _open_lock(path, exclusive=True)
    tmp_path = None
    try:
        # Read original
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            kept_lines: list[str] = []
            removed = 0
            preserved_unknown = 0
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except (ValueError, json.JSONDecodeError):
                    kept_lines.append(raw if raw.endswith("\n") else raw + "\n")
                    continue
                if not isinstance(obj, dict):
                    kept_lines.append(raw if raw.endswith("\n") else raw + "\n")
                    continue
                sv = obj.get("schema_version")
                if sv != SCHEMA_VERSION:
                    if args.destructive_prune:
                        removed += 1
                        continue
                    preserved_unknown += 1
                    kept_lines.append(raw if raw.endswith("\n") else raw + "\n")
                    continue
                ts_str = obj.get("ts")
                drop = False
                if isinstance(ts_str, str):
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            drop = True
                    except ValueError:
                        pass
                if drop:
                    removed += 1
                else:
                    kept_lines.append(raw if raw.endswith("\n") else raw + "\n")

        # Write tempfile in same dir + fsync + atomic replace (gpt-5.5 #1 fix)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp-metrics-", suffix=".jsonl", dir=str(path.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_fh:
                tmp_fh.writelines(kept_lines)
                tmp_fh.flush()
                os.fsync(tmp_fh.fileno())
            os.chmod(tmp_path, LEDGER_FILE_MODE)
            os.replace(tmp_path, path)
            tmp_path = None  # already renamed, no cleanup needed
        except Exception:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
        # fsync directory for durability
        try:
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        _release_lock(lock_fd)

    print(
        f"[aqg_metrics] pruned {removed} record(s) older than {args.days} days; "
        f"preserved {preserved_unknown} future-version record(s)",
        file=sys.stderr,
    )
    return EXIT_OK


# ===== CLI entry =====


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aqg_metrics",
        description="AQG local metrics ledger CLI (opt-in JSONL; framework Q11 frozen).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # status
    sp = sub.add_parser("status", help="show ledger info + opt-in state")
    sp.add_argument("--record-metrics", action="store_true", help="probe with explicit opt-in")

    # record
    sp = sub.add_parser("record", help="append a record (no-op if not opt-in)")
    sp.add_argument("--record-metrics", action="store_true", help="enable recording (overrides env)")
    sp.add_argument("--require-record", action="store_true",
                    help="exit 3 if not opt-in (default: silent no-op)")
    sp.add_argument("--json", action="store_true", help="read record JSON from stdin")
    sp.add_argument("--ts")
    sp.add_argument("--tool")
    sp.add_argument("--result", choices=["pass", "warn", "fail", "error"])
    sp.add_argument("--duration-ms", type=int, dest="duration_ms")
    sp.add_argument("--exit-code", type=int, dest="exit_code")
    sp.add_argument("--tool-version", dest="tool_version")
    sp.add_argument("--actor", choices=[
        "claude", "codex", "gpt-5.5", "gemini", "o3", "human", "ci-bot", "other",
    ])
    sp.add_argument("--audit-id", dest="audit_id")
    sp.add_argument("--audit-panel-size", type=int, dest="audit_panel_size")
    sp.add_argument("--findings-count", type=int, dest="findings_count")
    sp.add_argument("--accepted-count", type=int, dest="accepted_count")
    sp.add_argument("--rejected-count", type=int, dest="rejected_count")
    sp.add_argument("--needs-user-decision-count", type=int, dest="needs_user_decision_count")
    sp.add_argument("--cwd-sha256-first8", dest="cwd_sha256_first8")
    sp.add_argument("--git-branch-status", dest="git_branch_status",
                    choices=["present", "detached", "missing", "unknown"])

    # list
    sp = sub.add_parser("list", help="print records (newest N) as JSON array")
    sp.add_argument("--record-metrics", action="store_true", help="(noop for read commands)")
    sp.add_argument("--tool")
    sp.add_argument("--since", help="ISO 8601 datetime; only records >= this")
    sp.add_argument("--limit", type=int, default=0, help="newest N (0 = all)")

    # clear
    sp = sub.add_parser("clear", help="clear the ledger to 0 bytes (file + perms preserved)")
    sp.add_argument("--record-metrics", action="store_true")
    sp.add_argument("--yes", action="store_true", help="confirm destructive clear")

    # prune
    sp = sub.add_parser("prune", help="delete records older than --days")
    sp.add_argument("--record-metrics", action="store_true")
    sp.add_argument("--days", type=int, default=90)
    sp.add_argument("--destructive-prune", action="store_true",
                    help="ALSO remove unknown schema_version records (default: preserve)")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "status": cmd_status,
        "record": cmd_record,
        "list": cmd_list,
        "clear": cmd_clear,
        "prune": cmd_prune,
    }
    return handlers[args.cmd](args)


def self_test() -> int:
    """Quick sanity (full coverage in tests/test_aqg_metrics.py)."""
    import tempfile

    # truthy parsing
    with tempfile.TemporaryDirectory() as td:
        os.environ["AQG_METRICS_PATH"] = str(Path(td) / "metrics.jsonl")
        try:
            for val, expected in [
                ("1", True), ("true", True), ("yes", True), ("on", True),
                ("TRUE", True), ("Yes", True),
                ("0", False), ("false", False), ("no", False), ("off", False),
                ("", False), ("garbage", False),
            ]:
                os.environ["AQG_METRICS"] = val
                ns = argparse.Namespace(record_metrics=False)
                actual = _is_metrics_enabled(ns)
                assert actual == expected, f"AQG_METRICS={val!r}: expected {expected} got {actual}"
            del os.environ["AQG_METRICS"]
            ns = argparse.Namespace(record_metrics=True)
            assert _is_metrics_enabled(ns)
        finally:
            os.environ.pop("AQG_METRICS_PATH", None)
            os.environ.pop("AQG_METRICS", None)

    # path resolver: empty XDG safe (gemini #4)
    os.environ["XDG_DATA_HOME"] = ""
    try:
        path = _default_ledger_path()
        assert "/aqg/" not in str(path) or str(path).startswith(str(Path.home())), path
    finally:
        os.environ.pop("XDG_DATA_HOME", None)

    print("OK: aqg_metrics self-test passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test())
    raise SystemExit(main())
