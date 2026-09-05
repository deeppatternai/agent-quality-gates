#!/usr/bin/env python3
"""Pure wrapper-overlay core for the SKILL.md-from-source design (PR-1).

Lightweight source-overlay design; round-2 dual-audit converged.

The source `skills/<X>/SKILL.md` is the hand-edited, marker-free truth. The
Claude wrapper `agent-packs/claude-code/skills/<X>/SKILL.md` is GENERATED: a
verbatim copy of the source with each host-divergent span replaced via the
sidecar's `host_overrides["claude"]` (an ordered list of {anchor, replacement}).
No markers, no template engine, no placeholder/escape grammar.

Two public functions, both PURE (no filesystem I/O — the generator owns reads
and writes, mirroring `_skill_template_schema.check_skill_template`):

    is_managed_skill(sidecar) -> bool
        THE single shared migration predicate. True iff `wrapper_generated` is
        the literal bool True (strict, not merely truthy). `regen --all`,
        `aqg_skill_validator._validate_cross_cutting`, and `check_fixture_mix.py`
        all call this one helper so the three tools can never disagree about
        managed vs legacy (spec §4, round-2 gpt-f3).

    build_wrapper(source_text, sidecar) -> str
        The source text with each claude override applied by str.replace, in
        list order. Fail-closed (ValueError naming the offending anchor) when an
        anchor is ambiguous / stale / inside the frontmatter, or the overrides
        are malformed. Returns text ending in exactly one '\\n' (the shipped
        convention; normalized/asserted).

Idempotency note: `regen` is idempotent because build_wrapper is a PURE function
that always reads the UNCHANGED source — NOT because of any output/on-disk state
(spec §3, round-2 gemini-f4). Calling build_wrapper twice on the same inputs
yields identical output; re-running it on its own output is NOT a supported
operation (the source, not the wrapper, is the input).

stdlib-only (imports nothing beyond `from __future__`).
"""

from __future__ import annotations

from typing import Any


MANAGED_FLAG = "wrapper_generated"
HOST_OVERRIDES_KEY = "host_overrides"
CLAUDE_HOST = "claude"


def is_managed_skill(sidecar: Any) -> bool:
    """Return True iff the sidecar opts into wrapper generation.

    Strict: only the literal boolean True qualifies — `1`, `"true"`, or any
    other truthy value does NOT (so a mis-typed flag fails closed as legacy
    rather than silently entering the managed regime). The migration predicate
    is intentionally one tiny function shared by every consumer (spec §4).
    """
    if not isinstance(sidecar, dict):
        return False
    return sidecar.get(MANAGED_FLAG) is True


def _frontmatter_body_split(source_text: str) -> int:
    """Return the index where the body begins (one past the closing `---`).

    The frontmatter is the leading `---\\n` ... `\\n---` block. Overrides are
    body-only, so anchors falling before this index are rejected by
    build_wrapper. Returns 0 when the text has no leading frontmatter delimiter
    (then the whole text is body — no anchor can be "inside frontmatter").
    """
    if not source_text.startswith("---\n"):
        return 0
    # Closing delimiter: a line that is exactly `---`, after the opening one.
    # Search for "\n---\n" (closing line followed by a blank/newline) first; if
    # the file ends right at the closing delimiter, accept "\n---" at EOF too.
    close = source_text.find("\n---\n", 4)
    if close != -1:
        return close + len("\n---\n")
    if source_text.endswith("\n---"):
        return len(source_text)
    # Opening delimiter with no closing one: treat as no usable frontmatter
    # boundary (the whole text is body). build_wrapper still applies overrides;
    # validate_agent_pack / the schema guard catch a malformed frontmatter
    # elsewhere — this helper only locates the body for the anchor check.
    return 0


def _validate_override_shape(sidecar: dict) -> list:
    """Return the claude override list, validating shape. Fail-closed.

    Raises ValueError if `host_overrides` is present-but-malformed: it must be
    {"claude": [ {"anchor": str, "replacement": str}, ... ]}. An absent
    `host_overrides`, or an absent `claude` key, yields an empty list (a managed
    skill with no host divergence → verbatim copy; spec §4 round-2 gemini-f2).
    """
    raw = sidecar.get(HOST_OVERRIDES_KEY)
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ValueError(
            f"{HOST_OVERRIDES_KEY!r} must be a mapping "
            f'{{"{CLAUDE_HOST}": [...]}}, got {type(raw).__name__}'
        )
    overrides = raw.get(CLAUDE_HOST)
    if overrides is None:
        return []
    if not isinstance(overrides, list):
        raise ValueError(
            f"{HOST_OVERRIDES_KEY}.{CLAUDE_HOST} must be a list of "
            f"{{anchor, replacement}} objects, got {type(overrides).__name__}"
        )
    for i, item in enumerate(overrides):
        if not isinstance(item, dict):
            raise ValueError(
                f"{HOST_OVERRIDES_KEY}.{CLAUDE_HOST}[{i}] must be an object "
                f"with str 'anchor' + 'replacement', got {type(item).__name__}"
            )
        anchor = item.get("anchor")
        replacement = item.get("replacement")
        if not isinstance(anchor, str) or not anchor:
            raise ValueError(
                f"{HOST_OVERRIDES_KEY}.{CLAUDE_HOST}[{i}].anchor must be a "
                f"non-empty str, got {anchor!r}"
            )
        if not isinstance(replacement, str):
            raise ValueError(
                f"{HOST_OVERRIDES_KEY}.{CLAUDE_HOST}[{i}].replacement must be a "
                f"str, got {replacement!r}"
            )
    return overrides


def _resolve_anchor_spans(
    source_text: str, overrides: list, body_start: int
) -> list:
    """Validate every anchor against the source and return the sorted
    (start, end, replacement, anchor) spans for span-based assembly. Fail-closed.

    Returning spans (rather than only validating) lets build_wrapper assemble the
    output by slicing the ORIGINAL source around these spans — so a replacement's
    text is never re-scanned by a later anchor (audit 9c540100 f2 / gemini-f1).

    Checks per spec §3:
      - each anchor occurs exactly once (0 = stale/typo; >=2 = ambiguous);
      - no anchor lies (even partially) inside the YAML frontmatter;
      - no anchor is a substring of another anchor (ambiguous apply order);
      - no two anchors' single occurrences overlap (ambiguous apply order).
    """
    spans: list = []  # (start, end, replacement, anchor) per single occurrence
    for item in overrides:
        anchor = item["anchor"]
        count = source_text.count(anchor)
        if count != 1:
            raise ValueError(
                f"anchor occurs {count} time(s) in source, must occur exactly "
                f"once (0 = stale/typo; >=2 = ambiguous): {anchor!r}"
            )
        start = source_text.index(anchor)
        end = start + len(anchor)
        if start < body_start:
            raise ValueError(
                f"anchor lies within the YAML frontmatter (overrides are "
                f"body-only): {anchor!r}"
            )
        spans.append((start, end, item["replacement"], anchor))

    # Substring-of-another: an anchor contained in another anchor is ambiguous
    # (which span does the contained text belong to?).
    for i, a in enumerate(overrides):
        for j, b in enumerate(overrides):
            if i == j:
                continue
            if a["anchor"] in b["anchor"]:
                raise ValueError(
                    f"anchor is a substring of another anchor (ambiguous apply "
                    f"order): {a['anchor']!r} within {b['anchor']!r}"
                )

    # Overlapping occurrences in the source: two anchors whose single matched
    # spans intersect cannot both be applied deterministically.
    spans.sort(key=lambda s: s[0])
    for k in range(1, len(spans)):
        prev_end, prev_anchor = spans[k - 1][1], spans[k - 1][3]
        cur_start, cur_anchor = spans[k][0], spans[k][3]
        if cur_start < prev_end:
            raise ValueError(
                f"anchors overlap in the source (ambiguous apply order): "
                f"{prev_anchor!r} and {cur_anchor!r}"
            )
    return spans


def build_wrapper(source_text: str, sidecar: Any) -> str:
    """Return the Claude wrapper: source_text with claude overrides applied.

    Pure function (no I/O). Determinism: overrides apply in `host_overrides.
    claude` list order; the result is normalized to end in exactly one '\\n'.

    Fail-closed:
      - `is_managed_skill(sidecar)` must be True first (callers gate on it);
        otherwise raises ValueError (a non-managed skill has no generated
        wrapper — the source IS the only file).
      - malformed `host_overrides` shape → ValueError (see _validate_override_shape);
      - any ambiguous / stale / frontmatter anchor → ValueError (see _check_anchors).

    A managed skill with an empty/absent claude list → a verbatim copy of the
    source (valid; spec §4 round-2 gemini-f2).
    """
    if not isinstance(sidecar, dict):
        raise ValueError(
            f"sidecar must be a mapping, got {type(sidecar).__name__}"
        )
    if not is_managed_skill(sidecar):
        raise ValueError(
            f"build_wrapper called on a non-managed skill "
            f"({MANAGED_FLAG} is not True); callers must gate on "
            f"is_managed_skill() first"
        )

    overrides = _validate_override_shape(sidecar)
    body_start = _frontmatter_body_split(source_text)
    spans = _resolve_anchor_spans(source_text, overrides, body_start)

    # Span-based assembly: slice the ORIGINAL source around the validated,
    # non-overlapping spans (in source order) and splice in each replacement.
    # str.replace on the evolving output is intentionally NOT used — it would
    # let a replacement's text be re-matched by a later anchor (audit 9c540100
    # f2 / gemini-f1). Here replacement text is never re-scanned.
    parts: list = []
    prev = 0
    for start, end, replacement, _anchor in spans:
        parts.append(source_text[prev:start])
        parts.append(replacement)
        prev = end
    parts.append(source_text[prev:])
    result = "".join(parts)

    # Assert (not silently normalize) the shipped one-trailing-newline convention
    # (spec §3; audit 9c540100 f4). A 0- or 2+-newline EOF means the source or a
    # replacement violates the convention — fail closed so it is fixed at source,
    # keeping the wrapper a faithful copy.
    if not result.endswith("\n") or result.endswith("\n\n"):
        raise ValueError(
            "generated wrapper must end in exactly one newline (shipped SKILL.md "
            "convention); the source or a replacement has a different trailing-"
            "newline count — fix it at the source"
        )
    return result
