# AQG Project Ledger — neutral contract (contracts/ledger/)

> **What this is**: the **executable contract** of the AQG Project Ledger — the **same** schema + validation + paths + project_id algorithm **shared by** the AQG inbox consumer and the EAF exporter. Pure stdlib (zero third-party dependencies, DesignSpec OQ-10).
>
> **Source of truth**: the schema, validators, and conformance fixtures in THIS directory. They are the machine-verifiable form of the contract, and both sides — the AQG inbox consumer and the EAF exporter — are checked against them.
>
> **Contract changes require an AQG↔EAF mutual agreement — never change silently** (bump schema_version + sync both sides).

## Files

| File | Role | Who uses it |
|---|---|---|
| `incoming-event.schema.json` | JSON Schema (draft-07) for IncomingEvent — machine-readable contract doc | Both sides (+ anyone wanting to validate with a jsonschema library) |
| `conformance.py` | **Pure stdlib** validator `validate_incoming_event(event)->(ok, errors)`, mirrors the schema, accumulates all errors | Both sides (exporter self-check before push / consumer validation on receipt) |
| `project_id.py` | repo → `project_id` normalization (aggregation primary key, constant for the same repo) | Both sides (must compute the same value) |
| `paths.py` | `<AQG_DATA>`/inbox path resolution + `write_incoming_event()` atomic write | exporter writes / consumer reads paths |
| `fixtures/valid|invalid/*.json` | golden samples (21 valid + 40 invalid), covering every constraint | conformance tests + regression on both sides |

Tests: `tests/ledger/` (313 of them, pure-stdlib pytest).

## AQG consumer-side modules (not shared contract, implemented in this repo)

The contract files above are shared by both sides; below is the **AQG-side** read/write implementation — EAF does **not** import these; it only needs the contract above to stay stable (the coupling surface is the contract alone).

| File | Role |
|---|---|
| `store.py` | inbox consumer: scan `_inbox/` → validate → sort → cross-batch idempotent dedup → append + assign `ledger_seq`. StoredEvent = IncomingEvent + `recorded_at` + `ledger_seq` |
| `seq.py` | `producer_seq` ordering — the consumer's in-batch ordering and the projection watermark **share the same order** (single source of truth, prevents drift between the two sides) |
| `projection.py` | StoredEvents → `ProjectView` (§5.6 aggregation + §5.3.1 defect state machine / per-(defect,source) watermark / terminal-state rules). Read-boundary defense: skip bad lines with a warning, never raise |
| `render.py` | `ProjectView` → human-facing report: **html by default** (§5.8) / markdown / text / json. Canonical English (§5.7, translation is done by the render-layer LLM, not in the script); HTML-escapes all dynamic text (git text captured by hooks is untrusted) |

`projection` + `render` are wired together by the **`aqg-project-status`** skill (resolve project_id → load events.jsonl → project → render; non-English requests are translated by the skill-layer LLM).

## handoff §7 "items to align between the two sides" — the finalized answers for this contract

### 1. Exact `<AQG_DATA>` path
`<AQG_DATA>` = `$XDG_DATA_HOME/aqg`, or `~/.aqg` if unset (**aligned with wip_save's `_default_wip_dir`**).
- inbox (producer writes): `<AQG_DATA>/ledger/_inbox/`
- AQG-managed (producer does not touch): `.../_inbox/processed/`, `.../_inbox/failed/`

Provided by `paths.ledger_inbox_dir()` — **both sides import it**, do not hardcode it separately.
Inbox filenames are `<percent-encoded event_id>.json`: `paths.py` preserves
`[A-Za-z0-9._-]` and encodes other bytes such as `:` as `%3A` and `%` as `%25`,
keeping filenames injective and Windows-safe. Producers call
`write_incoming_event()` rather than hand-building filenames.

### 2. `project_id` normalization algorithm
`project_id.project_id_from_repo(repo_path)`:
- has a git remote origin → `owner/repo` (**lowercased**, so case variants of a clone converge to the same id);
- no remote → `local:<sha256(repo_toplevel_abspath)[:16]>` (the path is only hashed, never leaked).

The pure logic lives in `normalize_remote_url` / `project_id_from_remote_or_path` (both sides import the same copy, guaranteeing consistency).

### 3. Current `schema_version` value
**`"1.1"`** (`conformance.SCHEMA_VERSION` constant + schema `enum`). 1.1 = 1.0 + the two kinds `milestone`/`decision` (a **strict superset** — progress/defect/handoff are unchanged), so a 1.1 consumer is **backward-compatible** and accepts both 1.0 and 1.1 (rollout-safe; DesignSpec a4 §5.6). `milestone`/`decision` must declare 1.1 (capability handshake — 1.0 has no such semantics). The consumer routes unsupported versions to `failed/` for upgrade-and-replay rather than crashing; changes require mutual agreement first.

### 4. JSON Schema + golden fixtures + conformance check
All in this directory (delivering on OQ-12). **Exporter self-check**: `write_incoming_event(event)` runs `validate_incoming_event` first by default and raises on failure (a malformed event never reaches the inbox).

## How the two sides use it

**EAF exporter** (writes):
```python
import sys; sys.path.insert(0, "<aqg_repo>/contracts")   # or install as a package
from ledger.project_id import project_id_from_repo
from ledger.paths import write_incoming_event

event = {
    "schema_version": "1.0",
    "event_id": f"eaf:{run_id}:{producer_seq}",   # producer_seq is unique per event (N1)
    "project": project_id_from_repo(target_repo_path),
    "source": "eaf",
    "source_ref": {"run_id": run_id, "work_packet_id": wp_id},
    "kind": "progress",                            # | defect | handoff | milestone | decision (1.1)
    "payload": {"phase_event": "completed", "title": "..."},
    "occurred_at": iso8601_now(),
}
write_incoming_event(event)   # validate → encode filename → tmp → fsync → atomic rename
```

**AQG inbox consumer** (reads, outside this contract — AQG v1 implementation): scan `_inbox/` → `validate_incoming_event` → sort by `(source, producer_seq)` → cross-batch `event_id` idempotent dedup → append + assign `ledger_seq` → move to `processed/`; bad files move to `failed/`.

## Contract invariants (do not violate)
- Send an **IncomingEvent**, **do not send** `recorded_at`/`ledger_seq` (StoredEvent-only, generated by AQG; conformance rejects them).
- A defect payload has **no `status` field** (status is projected by AQG; conformance rejects it).
- `event_id = <source>:<run_or_session_id>:<producer_seq>`, with **producer_seq unique per LedgerEvent** (multiple events in the same transition do not collide, N1); a retry of the same business action reuses the **same** event_id (idempotency). The filesystem name is an encoded representation, not a different id.
- Text fields are **canonical English**.
- Data is per-machine local, never uploaded to the cloud.
