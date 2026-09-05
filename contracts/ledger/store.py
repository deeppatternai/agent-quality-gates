"""AQG-side ledger store: inbox consumer + append-only events.jsonl.

Built ON TOP of the neutral contract (conformance / paths / project_id). This
is the AQG consumer the contract README defers — NOT part of the shared
producer/consumer boundary.

Consume flow (DesignSpec §7.1 + §5.1.1):

    acquire single-consumer lock (skip if held)
    batch-scan _inbox/*.json
      → parse + conformance-validate
          · poison data (bad JSON/UTF-8/schema) → _inbox/failed/  (dead-letter)
          · transient I/O error reading a file  → deferred (left in inbox)
      → SORT batch by (source, producer_seq)   (never trust readdir order)
      → per project: repair torn log tail, dedup by event_id
          · any uncertain log state (repair/read/append I/O error) → defer the
            event AND poison the project for this pass (no event appends after a
            possibly-torn line, no ledger_seq guessed)
      → append StoredEvent (= IncomingEvent + recorded_at + ledger_seq)
      → move source file to _inbox/processed/

`ledger_seq` is a per-project monotonic counter (events.jsonl is per project_id),
assigned at append time AFTER sorting — the consumer-authoritative order the
projection replays (occurred_at is display-only; see DesignSpec §5.1.1).

Single-consumer: one AQG process drains the inbox, enforced by an advisory
flock. Machine-local v1; no distributed/NFS locking (§10).

Pure stdlib. recorded_at is injected by the caller (clock passed in) so this
module stays deterministic + testable; callers pass datetime.now(timezone.utc).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None

from ledger.conformance import validate_incoming_event
from ledger.paths import (
    _fsync_dir,
    event_id_from_filename,
    ledger_consume_lock,
    ledger_failed_dir,
    ledger_inbox_dir,
    ledger_processed_dir,
    project_events_path,
)
from ledger.seq import producer_seq, seq_sort_tuple


class ConsumeReport:
    """Outcome of one drain pass. Plain data; never raised."""

    def __init__(self) -> None:
        self.appended: list[str] = []      # event_ids newly stored
        self.deduped: list[str] = []       # event_ids already stored (skipped)
        self.failed: list[tuple[str, str]] = []  # (filename, reason) → failed/
        # deferred = LEFT IN INBOX for the next pass (NOT dead-lettered): a
        # transient I/O error (read/repair/append) made the outcome uncertain,
        # so retry rather than drop (audit 874863cc C1/C2, def7e984 R2).
        self.deferred: list[tuple[str, str]] = []  # (filename, reason)
        self.skipped_locked: bool = False  # another consumer holds the lock

    def as_dict(self) -> dict:
        return {
            "appended": list(self.appended),
            "deduped": list(self.deduped),
            "failed": [{"file": f, "reason": r} for f, r in self.failed],
            "deferred": [{"file": f, "reason": r} for f, r in self.deferred],
            "appended_count": len(self.appended),
            "deduped_count": len(self.deduped),
            "failed_count": len(self.failed),
            "deferred_count": len(self.deferred),
            "skipped_locked": self.skipped_locked,
        }


def _read_json(path: Path) -> "tuple[dict | None, str | None, bool]":
    """Return (event, reason, is_io_error). Never raises.

    is_io_error distinguishes a TRANSIENT read failure (OSError — EMFILE / NFS
    drop) from POISON DATA (bad UTF-8 / bad JSON / not an object). The caller
    defers transient I/O errors (leave in inbox, retry) but dead-letters poison
    (audit def7e984 R2). A non-UTF-8 .json is poison, not transient (C3).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, "invalid UTF-8", False
    except OSError as exc:
        return None, f"unreadable: {type(exc).__name__}", True
    try:
        obj = json.loads(text)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"invalid JSON: {exc}", False
    if not isinstance(obj, dict):
        return None, "top-level JSON is not an object", False
    return obj, None, False


def _sort_key(event: dict) -> tuple:
    """(source, producer_seq) ordering within a batch. The projection replays by
    ledger_seq, so the order we assign here IS the authoritative order. Uses the
    shared ledger.seq helpers so the consumer's sort and the projection's
    watermark order producer_seq identically."""
    source = event.get("source") or ""
    return (source, seq_sort_tuple(producer_seq(event.get("event_id", ""))))


# How many trailing bytes _repair_log_tail inspects. A single StoredEvent line
# is well under this; corruption is always the LAST interrupted write, so only
# the tail can be torn. Reading just this chunk keeps repair O(1) memory even as
# the log grows to GBs (audit 874863cc C5).
_REPAIR_TAIL_BYTES = 65536


def _repair_log_tail(events_path: Path) -> bool:
    """Truncate a torn/corrupt trailing line before reading the log.

    An append-only JSONL line is a complete JSON object ending in '\\n'. A crash
    mid-append can leave EITHER an unterminated tail (no '\\n') OR a complete-
    but-unparseable line. Both make `_existing_event_ids` skip the line, so its
    event_id is absent from dedup and the still-present inbox file would be
    appended AGAIN → a duplicate logical event.

    We inspect only the final `_REPAIR_TAIL_BYTES` (C5: never read the whole file
    — it grows unbounded). We ONLY ever truncate to a PROVEN newline boundary
    inside that window. If no safe boundary can be established (the whole window
    is one unterminated run — e.g. a torn tail longer than the window), we return
    False so the caller defers rather than truncating to an arbitrary offset and
    fusing the next append onto a garbage prefix (audit def7e984 R1).

    Returns True on a known-good outcome (clean file, or torn tail truncated to a
    real boundary), False if the project's log must NOT be appended this pass.
    Never raises.
    """
    try:
        size = events_path.stat().st_size
    except FileNotFoundError:
        return True  # no log yet — clean by definition
    except OSError:
        return False
    if size == 0:
        return True

    start = max(0, size - _REPAIR_TAIL_BYTES)
    try:
        with events_path.open("rb") as fh:
            fh.seek(start)
            window = fh.read()
    except OSError:
        return False

    # Establish `base` = file offset where our first WHOLE line begins.
    if start == 0:
        base = 0
        scan = window
    else:
        first_nl = window.find(b"\n")
        if first_nl == -1:
            # The last _REPAIR_TAIL_BYTES contain no newline → we cannot prove a
            # safe truncation boundary within the window (this is a torn tail
            # longer than the window, or a pathological huge line). Truncating to
            # `start` would leave a partial line and fuse the next append onto it.
            # Defer instead — do NOT corrupt the log (audit def7e984 R1).
            return False
        base = start + first_nl + 1
        scan = window[first_nl + 1:]

    # Walk whole lines in `scan`; good_end tracks the last file offset that ends
    # a well-formed JSON-object line.
    good_end = base
    pos = 0
    n = len(scan)
    corrupt_with_data_after = False
    while pos < n:
        nl = scan.find(b"\n", pos)
        if nl == -1:
            break  # trailing bytes, no newline → torn tail (safe: truncate good_end)
        line = scan[pos:nl].strip()
        if not line:
            good_end = base + nl + 1
            pos = nl + 1
            continue
        try:
            bad = not isinstance(json.loads(line), dict)
        except (ValueError, json.JSONDecodeError):
            bad = True
        if bad:
            # A COMPLETE (newline-terminated) line that does not parse is NOT a
            # normal torn append (a torn append leaves an UNterminated tail — the
            # nl == -1 branch). If valid bytes follow it, truncating to good_end
            # would DELETE events recorded AFTER the corruption (whose inbox files
            # were already moved to processed/, so they'd never be re-appended) —
            # refuse and defer for manual intervention rather than lose data
            # (audit b5381a7d gemini-nf1; workflow A6). If nothing follows (the
            # corrupt line is itself the tail), it is safe to drop just it.
            corrupt_with_data_after = scan[nl + 1:].strip() != b""
            break
        good_end = base + nl + 1
        pos = nl + 1

    if corrupt_with_data_after:
        return False  # mid-log corruption — never auto-delete the valid tail
    if good_end >= size:
        return True  # tail is clean — leave byte-identical
    return _truncate_to(events_path, good_end)


def _truncate_to(events_path: Path, length: int) -> bool:
    """Truncate events_path to `length` bytes, fsync'd. Returns ok."""
    try:
        with events_path.open("rb+") as fh:
            fh.truncate(length)
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


def _existing_event_ids(events_path: Path) -> "tuple[set[str], int, bool]":
    """Scan events.jsonl → (stored event_ids, max ledger_seq, ok).

    `ok` distinguishes "no log yet" (ok=True, empty — safe to start at seq 1)
    from "log exists but could not be read" (ok=False — caller MUST NOT append,
    or it would reset ledger_seq to 1 and duplicate; audit 874863cc C2). A
    malformed individual line is still skipped defensively (it does not flip ok).
    """
    ids: set[str] = set()
    max_seq = 0
    if not events_path.is_file():
        return ids, max_seq, True  # genuinely no history — clean start
    try:
        with events_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                eid = rec.get("event_id")
                if isinstance(eid, str):
                    ids.add(eid)
                seq = rec.get("ledger_seq")
                # bool is an int subclass: a corrupt/hand-edited line with
                # "ledger_seq": true must NOT seed max_seq=1 and risk seq reuse
                # (audit b5381a7d o3-f6; projection._event_ok already guards this).
                if isinstance(seq, int) and not isinstance(seq, bool) and seq > max_seq:
                    max_seq = seq
    except OSError:
        return set(), 0, False  # exists but unreadable — do NOT treat as empty
    return ids, max_seq, True


def _append_line(events_path: Path, record: dict) -> None:
    """Append one StoredEvent as a JSONL line, fsync'd (durable).

    A torn append (crash mid-write) is repaired by `_repair_log_tail` on the
    next drain — there is nothing to roll back here.
    """
    parent = events_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    is_new = not events_path.exists()
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    if is_new:
        # persist the new file's directory entry — a file fsync alone does not
        # make a freshly-created path durable on POSIX (audit b5381a7d gpt-f5).
        _fsync_dir(parent)


def _try_lock_fd(fd: int) -> bool:
    """Acquire a nonblocking exclusive consume lock on POSIX or Windows."""
    if _fcntl is not None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    if _msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    return False


def _unlock_fd(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


def _move(src: Path, dest_dir: Path) -> None:
    """Move src into dest_dir, suffixing on name collision.

    processed/ and failed/ are forensic trails. A re-dropped event_id reuses the
    same filename; os.replace would silently clobber the prior artifact, losing
    "delivered N times" history. On collision suffix .dup-N; if every suffix to
    the cap is taken, fall back to a guaranteed-unique mkstemp name so the file
    is ALWAYS evicted from the inbox — leaving it there would re-process it every
    pass forever (audit 874863cc C6 poison loop).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        stem, suffix = src.stem, src.suffix
        dest = None
        for n in range(2, 1000):
            cand = dest_dir / f"{stem}.dup-{n}{suffix}"
            if not cand.exists():
                dest = cand
                break
        if dest is None:
            try:
                fd, unique = tempfile.mkstemp(
                    prefix=f"{stem}.dup-", suffix=suffix, dir=str(dest_dir)
                )
                os.close(fd)
                dest = Path(unique)
            except OSError:
                return  # cannot create even a temp name — give up (best-effort)
    try:
        os.replace(src, dest)
    except OSError:
        pass


def consume_inbox(
    *,
    recorded_at: str,
    inbox: Path | None = None,
    processed: Path | None = None,
    failed: Path | None = None,
    events_path_for=None,
    lock_path: Path | None = None,
) -> ConsumeReport:
    """Drain the inbox once: validate → sort → dedup → append → move.

    Args:
        recorded_at: ISO8601 string stamped onto every StoredEvent this pass
            (caller passes datetime.now(timezone.utc).isoformat() — injected so
            the store stays deterministic + testable).
        inbox / processed / failed: dir overrides (default: paths.py).
        events_path_for: callable project_id → Path for the events log
            (default: paths.project_events_path). Overridable for tests.
        lock_path: advisory single-consumer lock file (default:
            paths.ledger_consume_lock). Overridable for tests.

    Returns a ConsumeReport. Never raises on per-file problems — poison data
    goes to failed/, transient I/O errors are deferred (left in inbox), and the
    drain continues (DesignSpec §7.1). If the consumer lock cannot be acquired,
    returns immediately with skipped_locked=True (fail-closed: never drain
    unlocked, which would risk double-assigning ledger_seq).
    """
    box = inbox if inbox is not None else ledger_inbox_dir()
    done = processed if processed is not None else ledger_processed_dir()
    bad = failed if failed is not None else ledger_failed_dir()
    events_path_for = events_path_for or project_events_path
    lk = lock_path if lock_path is not None else ledger_consume_lock()

    report = ConsumeReport()

    # Advisory single-consumer lock. Use the platform stdlib lock primitive
    # (fcntl on POSIX, msvcrt on Windows). Fail-closed: any failure to acquire
    # skips the drain rather than risk a concurrent double-drain.
    try:
        lk.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lk, os.O_RDWR | os.O_CREAT, 0o600)
        os.write(lock_fd, b"\0")
    except OSError:
        report.skipped_locked = True
        return report
    if not _try_lock_fd(lock_fd):
        os.close(lock_fd)
        report.skipped_locked = True
        return report
    try:
        return _drain_locked(box, done, bad, events_path_for, recorded_at, report)
    finally:
        try:
            _unlock_fd(lock_fd)
        finally:
            os.close(lock_fd)


def _drain_locked(box, done, bad, events_path_for, recorded_at, report) -> ConsumeReport:
    """Inner drain, run while holding the consumer lock."""
    try:
        names = sorted(os.listdir(box))
    except OSError:
        return report  # no inbox yet → nothing to do

    # 1. parse + validate; collect (path, event) for the good ones. Poison data
    #    dead-letters; a transient read I/O error defers (audit def7e984 R2).
    batch: list[tuple[Path, dict]] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = box / name
        if not path.is_file():
            continue
        try:
            event_id_from_filename(name)
        except ValueError as exc:
            report.failed.append((name, str(exc)))
            _move(path, bad)
            continue
        event, reason, is_io = _read_json(path)
        if event is None:
            if is_io:
                report.deferred.append((name, reason or "read I/O error"))
            else:
                report.failed.append((name, reason or "unreadable"))
                _move(path, bad)
            continue
        ok, errors = validate_incoming_event(event)
        if not ok:
            report.failed.append((name, "; ".join(errors)))
            _move(path, bad)
            continue
        batch.append((path, event))

    # 2. SORT by (source, producer_seq) — never trust readdir order.
    batch.sort(key=lambda pe: _sort_key(pe[1]))

    # 3+4. per project: repair torn tail, dedup vs already-stored, append in
    #      sorted order. ledger_seq is read once per project then advanced in mem.
    #      A project whose log state turns UNCERTAIN this pass (repair / read /
    #      append I/O error) is POISONED: its remaining events are left in the
    #      inbox (deferred) for a clean retry, never appended with a guessed seq
    #      (audit 874863cc C1/C2).
    project_state: dict[str, tuple[set[str], int]] = {}
    poisoned: set[str] = set()
    for path, event in batch:
        project = event["project"]
        eid = event["event_id"]
        events_path = events_path_for(project)

        if project in poisoned:
            report.deferred.append((path.name, "project log state uncertain this pass"))
            continue  # leave in inbox — retry next drain

        if project not in project_state:
            if not _repair_log_tail(events_path):
                poisoned.add(project)
                report.deferred.append((path.name, "log tail repair failed/unsafe"))
                continue
            ids, max_seq, ok = _existing_event_ids(events_path)
            if not ok:
                poisoned.add(project)
                report.deferred.append((path.name, "existing log unreadable"))
                continue
            project_state[project] = (ids, max_seq)

        seen_ids, max_seq = project_state[project]

        if eid in seen_ids:
            report.deduped.append(eid)
            _move(path, done)
            continue

        next_seq = max_seq + 1
        stored = dict(event)
        stored["recorded_at"] = recorded_at
        stored["ledger_seq"] = next_seq
        try:
            _append_line(events_path, stored)
        except OSError as exc:
            # Ambiguous: the line may be partially written. Drop cached state so
            # the NEXT pass re-repairs (truncating any torn tail) before assigning
            # more seqs; poison the project for the rest of THIS batch so a later
            # same-project event can't append after a torn line; leave the source
            # in the inbox for retry — this is an I/O failure, not poison data, so
            # it must NOT be dead-lettered (audit 874863cc C1).
            report.deferred.append((path.name, f"append failed: {type(exc).__name__}"))
            project_state.pop(project, None)
            poisoned.add(project)
            continue
        seen_ids.add(eid)
        project_state[project] = (seen_ids, next_seq)
        report.appended.append(eid)
        _move(path, done)

    return report
