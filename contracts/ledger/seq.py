"""producer_seq ordering — the SINGLE source of truth shared by the inbox
consumer's batch sort (store.py) AND the projection's per-source watermark
(projection.py).

Both sides MUST order producer_seq identically: the consumer assigns ledger_seq
in (source, producer_seq) order, and the projection detects late/out-of-order
defect events by comparing producer_seq against the SAME ordering. If the two
diverged, the watermark could drop an event the consumer treated as in-order (or
keep one it treated as stale). One implementation here makes that impossible.

producer_seq is the third ':'-segment of event_id
('<source>:<run_or_session_id>:<producer_seq>'; DesignSpec §5.1.1). The contract
(N1) allows three forms: a bare integer 'N', a UUID, or a compound
'<transition>.<kind>.<index>' (e.g. '7.defect.2'). Segment-wise comparison
orders '7.defect.2' < '7.defect.10' (the '10' segment compares as int 10, not
lexical '1' < '2').

A UUID producer_seq has no monotonic meaning, so its ordering here is
deterministic but NOT semantically earlier/later — a producer that wants the
projection's stale-event protection must emit a monotonic seq, not a UUID (the
documented escape hatch in §5.1.1). Pure stdlib.
"""
from __future__ import annotations

import re

# Split a producer_seq into maximal numeric / non-numeric runs so a compound seq
# ('7.defect.10') sorts by segment VALUE, not lexically.
_SEQ_SEGMENT_RE = re.compile(r"\d+|\D+")

# A digit run longer than this is ordered as TEXT rather than fed to int(): it
# cannot be a real monotonic counter, Python's int(str) raises above ~4300 digits
# (a manually-edited events.jsonl line could carry one), and converting a huge int
# is needless work (audit b5381a7d gpt-f3). The original-string tiebreaker below
# keeps such runs distinct and deterministically ordered without int().
_MAX_NUMERIC_SEG = 18

# A UUID producer_seq in CANONICAL HYPHENATED 8-4-4-4-12 form. This is §5.1.1's
# escape hatch — it guarantees id-uniqueness but has NO monotonic order, so the
# projection MUST NOT apply watermark stale-rejection to it (a deterministic-but-
# meaningless sort key would drop ~half of a producer's events as "stale").
#
# We require the hyphens deliberately: a bare 32-hex string is ambiguous with a
# 32-digit decimal integer (all-digits IS hex) or a hex-encoded monotonic id
# (e.g. a hex ULID), so matching it would wrongly DISABLE watermark protection
# for a legitimately monotonic seq (audit dfdcae4d: gpt-f3 + gemini-f3). Integer
# 'N' and compound '<transition>.<kind>.<index>' seqs are monotonic and are NOT
# matched. A producer using the UUID escape hatch must emit the canonical
# hyphenated form, which str(uuid.uuid4()) produces by default.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_uuid(seq: str) -> bool:
    """True when producer_seq is a canonical hyphenated UUID (no monotonic order —
    see _UUID_RE). The projection skips watermark stale-rejection for such seqs
    (§5.1.1); everything else (integer, compound, 32-hex) is treated as monotonic."""
    return isinstance(seq, str) and bool(_UUID_RE.match(seq))


def producer_seq(event_id: str) -> str:
    """Tail of '<source>:<run_or_session_id>:<producer_seq>' (everything after
    the 2nd ':'; the tail itself may contain ':'). Returns '' when event_id has
    fewer than three ':'-separated segments — callers sort such ids first/equal
    rather than crashing (conformance already guarantees the 3-segment shape for
    accepted events)."""
    parts = event_id.split(":", 2)
    return parts[2] if len(parts) == 3 else ""


def seq_sort_tuple(seq: str) -> tuple:
    """Segment-wise sort key: '7.defect.2' < '7.defect.10'. Each segment is
    (is_text, int_or_0, text) so numeric and text segments order deterministically
    and never compare int against str.

    The trailing original-text component is what makes two producer_seqs that
    normalize to the SAME int but differ as strings ('1' vs '01') compare as
    DISTINCT keys. Without it the projection's `seq_key <= watermark` check would
    treat the second as stale and SILENTLY DROP a legitimately-new defect event
    (audit b5381a7d gpt-f4 + gemini-kf2 + o3-f3; workflow A4).

    A digit run is fed to int() only when it is ASCII digits within
    _MAX_NUMERIC_SEG; otherwise (overlong, or a unicode digit that `\\d` matched
    but int() / the contract alphabet would not accept) it is ordered as text so
    a corrupt stored line can never raise inside the sort/watermark (gpt-f3)."""
    out: list[tuple] = []
    for seg in _SEQ_SEGMENT_RE.findall(seq):
        if seg.isascii() and seg.isdigit() and len(seg) <= _MAX_NUMERIC_SEG:
            out.append((0, int(seg), seg))
        else:
            out.append((1, 0, seg))
    return tuple(out)
