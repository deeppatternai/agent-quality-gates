"""AQG_DATA / ledger inbox path resolution + atomic event write (DesignSpec §7.1).

<AQG_DATA> mirrors wip_save's _default_wip_dir convention: $XDG_DATA_HOME/aqg,
fallback ~/.aqg. The ledger inbox is <AQG_DATA>/ledger/_inbox/. The EAF exporter
calls write_incoming_event() to atomically drop an event (tmp → fsync → rename)
so the AQG consumer never reads a half file. AQG owns processed/ + failed/
(producers don't touch them). Per-machine local; not synced/uploaded.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

# event_id is guaranteed path-segment-safe by the contract (conformance restricts
# it to [A-Za-z0-9._:-]), but ':' is illegal in Windows filenames. Store IDs as
# percent-encoded path segments instead of relying on platform-specific names:
# [A-Za-z0-9._-] stays literal, every other byte is %HH (uppercase hex). This is
# injective because '%' itself is encoded as %25, so an encoded ':' (%3A) cannot
# collide with a literal "%3A" sequence (audit gpt-f5 previously rejected lossy
# replacement because 'a/b' and 'a_b' collided).
_SAFE_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SAFE_ENCODED_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._%:-]+$")
_LITERAL_SEGMENT_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)


def aqg_data_dir() -> Path:
    """<AQG_DATA> — $XDG_DATA_HOME/aqg, fallback ~/.aqg (mirrors wip_save)."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "aqg"
    return Path.home() / ".aqg"


def ledger_dir() -> Path:
    return aqg_data_dir() / "ledger"


def ledger_inbox_dir() -> Path:
    """Where producers drop <encoded-event_id>.json (the EAF exporter writes here)."""
    return ledger_dir() / "_inbox"


def ledger_processed_dir() -> Path:
    """AQG-managed: successfully consumed events. Producers do not touch."""
    return ledger_inbox_dir() / "processed"


def ledger_failed_dir() -> Path:
    """AQG-managed dead-letter: unparseable / unsupported schema_version."""
    return ledger_inbox_dir() / "failed"


def ledger_consume_lock() -> Path:
    """Advisory single-consumer lock file (one drainer at a time)."""
    return ledger_dir() / ".consume.lock"


def ledger_hook_state_dir() -> Path:
    """Per-(session,project) capture-anchor state for the v2 hook writer:
    <AQG_DATA>/ledger/_hook_state/<session_token>.json. An OPTIMIZATION only —
    the idempotent event_id + store cross-batch dedup are the correctness net
    (sketch §4 / §7). Not a producer/consumer contract surface."""
    return ledger_dir() / "_hook_state"


def project_events_path(project_id: str) -> Path:
    """Per-project append-only event log: <AQG_DATA>/ledger/<project_id>/events.jsonl.

    project_id may contain '/' (e.g. 'owner/repo'); it is preserved as the
    directory separator. Each segment is percent-encoded with the same injective
    rule as event_id filenames, so 'local:<hash>' is stored as
    'local%3A<hash>'.

    Containment backstop (audit b5381a7d gpt-f1 + gemini-kf1 + o3-f1; workflow
    A1/A3): conformance is the primary gate, but a caller that bypasses it (e.g.
    the read-side aqg_project_status passing an unvalidated --project-id) must
    still never escape the ledger root. A project_id whose '..'/absolute segments
    resolve outside ledger_dir() raises ValueError rather than reading/writing an
    attacker-chosen path.
    """
    base = ledger_dir()
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id is required")
    raw_parts = project_id.split("/")
    if project_id.startswith(("/", "\\")) or any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError(f"project_id {project_id!r} escapes the ledger root {base.resolve()}")
    encoded_parts = [_encode_path_segment(part) for part in raw_parts]
    candidate = base.joinpath(*encoded_parts, "events.jsonl")
    base_resolved = base.resolve()
    if base_resolved not in candidate.resolve().parents:
        raise ValueError(f"project_id {project_id!r} escapes the ledger root {base_resolved}")
    return candidate


def _encode_path_segment(value: str) -> str:
    """Percent-encode one filesystem path segment using UTF-8 bytes.

    The literal set is deliberately smaller than the event/project contracts:
    [A-Za-z0-9._-] is cross-platform filename-safe; ':' and '%' are encoded.
    """
    if not isinstance(value, str) or value == "":
        raise ValueError("path segment must be a non-empty string")
    out: list[str] = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if ch in _LITERAL_SEGMENT_CHARS:
            out.append(ch)
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def _decode_path_segment(value: str) -> str:
    """Decode one percent-encoded path segment."""
    if not isinstance(value, str) or value == "":
        raise ValueError("path segment must be a non-empty string")
    if not _SAFE_ENCODED_SEGMENT_RE.match(value):
        raise ValueError(f"encoded path segment has unsafe characters: {value!r}")
    data = bytearray()
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "%":
            esc = value[i + 1:i + 3]
            if len(esc) != 2 or not all(c in "0123456789ABCDEFabcdef" for c in esc):
                raise ValueError(f"invalid percent escape in path segment: {value!r}")
            data.append(int(esc, 16))
            i += 3
            continue
        data.extend(ch.encode("utf-8"))
        i += 1
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"percent-decoded path segment is not UTF-8: {value!r}") from exc
    if "/" in decoded or "\\" in decoded or decoded in (".", ".."):
        raise ValueError(f"decoded path segment is not filesystem-safe: {decoded!r}")
    return decoded


def event_id_from_filename(filename: str) -> str:
    """Return the event_id encoded in '<encoded-event-id>.json'."""
    if not isinstance(filename, str) or not filename.endswith(".json"):
        raise ValueError(f"inbox filename must end with .json: {filename!r}")
    event_id = _decode_path_segment(filename[:-5])
    if not _SAFE_EVENT_ID_RE.match(event_id):
        raise ValueError(f"inbox filename decodes to invalid event_id: {filename!r}")
    return event_id


def inbox_event_filename(event_id: str) -> str:
    if not isinstance(event_id, str) or not _SAFE_EVENT_ID_RE.match(event_id):
        raise ValueError(
            f"event_id is not filename-safe: {event_id!r} (the contract restricts it "
            f"to [A-Za-z0-9._:-]); refusing a lossy/colliding inbox filename"
        )
    return _encode_path_segment(event_id)


def _safe_filename(event_id: str) -> str:
    """Backward-compatible alias for older tests/callers."""
    return inbox_event_filename(event_id)


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a create/rename within it is durable across a crash.

    A file fsync alone does NOT persist the new directory entry on POSIX, so a
    power loss after os.replace could drop the just-delivered event (audit
    b5381a7d gpt-f5 + o3-f4). Best-effort: some platforms/filesystems disallow
    directory fsync (e.g. certain Windows / network mounts) — swallow that rather
    than fail an otherwise-successful write."""
    try:
        dfd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def write_incoming_event(
    event: dict,
    *,
    inbox_dir: "Path | None" = None,
    validate: bool = True,
) -> Path:
    """Atomically write one IncomingEvent into the inbox as <encoded-event_id>.json.

    tmp → fsync → atomic os.replace, mirroring wip_save._write_snapshot_atomic,
    so the AQG consumer never reads a partial file. With validate=True (default)
    the event is conformance-checked first — a producer can't drop a malformed
    event. Repeated writes of the same event_id overwrite to ONE file
    (file-level idempotency; AQG also dedups cross-batch by event_id). Returns
    the final path.
    """
    if validate:
        from ledger.conformance import validate_incoming_event

        ok, errors = validate_incoming_event(event)
        if not ok:
            raise ValueError("IncomingEvent failed conformance: " + "; ".join(errors))

    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        # With validate=False a caller could pass an event lacking event_id; fail
        # with a clear error instead of KeyError (audit b5381a7d o3-f5; A11).
        raise ValueError("event_id is required to write an IncomingEvent")
    target_dir = Path(inbox_dir) if inbox_dir is not None else ledger_inbox_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    final = target_dir / f"{inbox_event_filename(event_id)}.json"
    data = json.dumps(event, ensure_ascii=False, indent=2).encode("utf-8")

    fd, tmp = tempfile.mkstemp(prefix=".tmp-ledger-", suffix=".json", dir=str(target_dir))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)  # atomic rename within the same dir
        _fsync_dir(target_dir)  # persist the rename (audit b5381a7d gpt-f5 + o3-f4)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final
