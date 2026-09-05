"""Injection-proof translation of a ProjectView business report (DesignSpec §4.2).

The report renders in canonical English (render.py). To let it FOLLOW a target
language without letting the (untrusted) translation LLM fabricate or tamper with
FACTS, translation is split so the LLM only ever sees producer free-text:

  1. collect_segments(view) → the producer free-text "dict" (titles, summaries,
     key outcomes, decision questions / options / rationale). Numbers, %, dates,
     status, the health light, ids, AND the number-bearing size/wall-clock
     estimates are NOT in the dict — they stay locked in the structured view.
  2. The caller's translator (the calling session's LLM, treated as HOSTILE) maps
     each segment → its localization. It never sees a STRUCTURED authoritative field
     (it may see a fact a producer wrote into prose — see the security model).
  3. scan_segment(original, translated) — a supplementary integrity guard on the
     digits/dates/% that appear INCIDENTALLY inside producer prose (see the
     security model below).
  4. apply_segments + render — the facts are injected by render from the UNCHANGED
     structured fields; only the free-text is swapped. md/html sanitization is thus
     INHERITED from render (`_h` / `_md`).

SECURITY MODEL (scope honest after audits c6050ee7 + 02944902).
PRIMARY, ROBUST guarantee: the AUTHORITATIVE facts — milestone status, completion
% / bar, health light, coverage, all dates — are rendered by render() from the
UNCHANGED structured view fields; they are NEVER in a segment, so a translation
CANNOT fabricate them (worst case is a clean fall-back to English). The only
render-locked status display is the structured badge / bar / date; verified by the
`test_translate_locked_facts_byte_identical` cross-render assertion.

SECONDARY, BEST-EFFORT guard (`scan_segment`): an integrity check on the incidental
digits/%/dates a producer happened to write inside prose — the translation's
number/date token MULTISET must EQUAL the original's and its '%' count must match
(add / alter / drop → reject), and it may not introduce an English status keyword.
Inputs are first stripped of Unicode FORMAT chars (Cf: zero-width, bidi controls /
isolates, the tag block) + extra invisibles + the "No" number forms, then NFKC-
folded — so a fullwidth '１００％', a 'comple‌ted' or 'comple͏ted' token SPLIT, or a
'10⁵' superscript spoof cannot change the token multiset undetected; the English-
keyword check uses ASCII-letter boundaries so a CJK-adjacent 'completed' is caught.

NOT GUARANTEED (the scan validates fact TOKENS, never MEANING): a hostile translator
fully controls the SEMANTIC CONTENT of every exposed prose segment (title, summary,
key_outcome, question, rationale, options) — it can localize "on track" as the
opposite. If a producer writes status/completion NARRATIVE into prose ("shipped at
100% on 2026-06-01"), only the structured badge/bar/date is render-locked; the
prose retelling is translator-controllable (the % / date TOKENS are token-checked,
but the words around them are not). Further documented residuals, all low-impact
because the authoritative metric is render-locked: pure permutation of two numbers
within one segment, spelled-out / target-language quantities (e.g. a spelled-out "one hundred percent"), localized
NON-ISO dates (degrade to permutable numbers), and homoglyph-spoofed status words.
A Trojan-Source bidi VISUAL reorder of in-prose numbers is likewise a residual: the
controls are stripped for the token check (so they cannot hide a token CHANGE) but
the rendered glyphs may display reordered — left in because flagging introduced bidi
would break legitimate RTL (Arabic/Hebrew) localization, and the affected numbers
are non-authoritative prose (output bidi/display handling is render.py's domain).
These are untrusted producer text, never an authoritative metric, and are NOT
claimed to be caught here.

Fail-closed: a non-list/tuple return, a shape mismatch, a raising translator, or
ANY scan violation → deliver the English canonical render (DesignSpec §4.2 "reject
delivery, fall back to English canonical"). The deterministic security gate runs here, in-process,
regardless of who produced the translation — it never trusts the translator. Pure
stdlib.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from typing import Callable, Sequence

from ledger.projection import ProjectView
from ledger.render import render

# A translator maps (segments, target_lang) → one localization per segment, in
# order. The calling session supplies an LLM-backed one; it is treated as hostile.
Translator = Callable[[Sequence[str], str], Sequence[str]]

# Producer free-text fields that may be localized — PURE PROSE only. The
# number-bearing estimate fields (estimated_size "~2 person-weeks", actual_wallclock
# "14 minutes") and the short label/id-ish fields (gate, phase_label) are LOCKED
# (rendered in canonical English) so a unit/number swap has no surface here. Ids,
# status, dates, seqs, sources, the blocks id-ref are likewise locked facts the LLM
# never sees (audit c6050ee7 — keep number-heavy fields out of the dict).
_TRANSLATABLE_MILESTONE = ("title", "summary", "key_outcome")
_TRANSLATABLE_DECISION = ("question", "rationale")   # options handled per-element

# Distinctive English status / progress keywords matched at WORD BOUNDARIES (so
# "unblocked" does not match "blocked" — audit c6050ee7 qwen-f1). A faithful
# localization to another language does not ADD these; a lazy English-keyword
# injection ("… completed") does. Subset-vs-original (a keyword already in the
# source segment is allowed). This is a best-effort tripwire, NOT a complete
# cross-language status guard — the authoritative status is render-locked.
_STATUS_KEYWORDS = frozenset({
    "completed", "cancelled", "canceled", "planned", "started", "done",
    "in progress", "blocked", "on track", "at risk", "needs attention",
    "overdue", "stale",
})

# Invisibles that are NOT Unicode category Cf (so the category sweep in _normalize
# misses them) but still split a keyword / digit run while rendering nothing: the
# combining grapheme joiner, Hangul fillers, the Mongolian vowel separator, and the
# variation selectors (audit 02944902 — the category-Cf sweep already covers
# zero-width spaces, the bidi isolates U+2066-2069 = Trojan Source, and the
# U+E0000-E007F tag block).
_EXTRA_INVISIBLE = (
    frozenset("\u034f\u115f\u1160\u17b4\u17b5\u180e\u3164\uffa0")
    | frozenset(chr(c) for c in range(0xFE00, 0xFE10))        # variation selectors VS1-16
    | frozenset(chr(c) for c in range(0xE0100, 0xE01F0))      # variation selectors supplement
)
# A signed number run; the optional sign catches a '-100' → '100' polarity flip
# (audit c6050ee7 gemini-f3 / qwen-f2).
_NUM_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Strip ONLY a comma in a valid thousands group (comma + exactly three digits + a
# boundary) so '1,000'->'1000' but '1,2,3' is NOT merged into '123' (which would let
# an attacker regroup comma-separated numbers past the multiset check — audit
# 02944902 gpt-f1 / gemini-f2 / grok-f1 / qwen-f3).
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")


@dataclass(frozen=True)
class TranslatedReport:
    """The outcome of a translate_report call. On a no-op or a rejected (fell_back)
    translation, `report` is the English canonical render — always deliverable."""
    report: str
    language: str
    translated: bool                 # a validated translation was applied
    fell_back: bool                  # a violation/mismatch forced English canonical
    violations: "tuple[str, ...]"    # why it fell back (empty on success / no-op)


def collect_segments(view: ProjectView) -> "tuple[str, ...]":
    """The ordered, de-duplicated producer free-text — the ONLY thing sent to the
    translator. Facts/ids/dates/status are never collected."""
    seen: "dict[str, None]" = {}
    for m in view.milestones:
        for f in _TRANSLATABLE_MILESTONE:
            _add(seen, getattr(m, f))
    for d in view.decisions:
        for f in _TRANSLATABLE_DECISION:
            _add(seen, getattr(d, f))
        for opt in d.options:
            _add(seen, opt)
    return tuple(seen)


def _add(seen: "dict[str, None]", value) -> None:
    if isinstance(value, str) and value and value not in seen:
        seen[value] = None


def apply_segments(view: ProjectView, mapping: "dict[str, str]") -> ProjectView:
    """Return an immutable copy of `view` with each translatable free-text field
    replaced by its localization (mapping.get(value, value)). Locked fields are
    copied verbatim — facts cannot change here."""
    def tr(value):
        return mapping.get(value, value) if isinstance(value, str) and value else value

    milestones = tuple(
        replace(m, **{f: tr(getattr(m, f)) for f in _TRANSLATABLE_MILESTONE})
        for m in view.milestones
    )
    decisions = tuple(
        replace(
            d,
            **{f: tr(getattr(d, f)) for f in _TRANSLATABLE_DECISION},
            options=tuple(tr(o) for o in d.options),
        )
        for d in view.decisions
    )
    return replace(view, milestones=milestones, decisions=decisions)


def _normalize(text: str) -> str:
    """Canonicalize before fact extraction. Drop (a) every Unicode FORMAT char
    (category Cf: zero-width spaces, the bidi embeddings/isolates incl. U+2066-2069
    Trojan-Source, the U+E0000-E007F tag block, BOM) and the extra non-Cf invisibles,
    and (b) the "No" number forms (superscripts/subscripts/fractions) that NFKC would
    otherwise fold into spoof digits ('10⁵' → '105'); THEN NFKC-fold so a fullwidth
    '１００％' folds to '100%' (audit 02944902 gemini-f1 / claude-f2)."""
    cleaned = "".join(
        c for c in text
        if unicodedata.category(c) not in ("Cf", "No")
        and c not in _EXTRA_INVISIBLE
    )
    return unicodedata.normalize("NFKC", cleaned)


def _profile(text: str) -> dict:
    """Fact fingerprint of a string. Numbers and dates are MULTISETS (Counter) so a
    permutation/removal is detectable, not just an addition; '%' is counted; status
    keywords are word-boundary matched. ASCII thousands separators are dropped so
    '1,000' folds to '1000'."""
    norm = _normalize(text)
    low = norm.lower()
    return {
        "nums": Counter(_NUM_RE.findall(_THOUSANDS_RE.sub("", norm))),
        "dates": Counter(_DATE_RE.findall(norm)),
        "pct": norm.count("%"),
        "keywords": frozenset(
            k for k in _STATUS_KEYWORDS
            if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", low)
        ),
    }


def scan_segment(original: str, translated: str) -> "list[str]":
    """Prose fact-token guard (DesignSpec §4.2.4b). The translation's number/date
    token multiset must EQUAL the original's and its '%' count must match — so a
    fact cannot be added, altered, OR dropped — and it may not introduce an English
    status keyword. Returns the list of violations (empty = clean). See the module
    SECURITY MODEL for what this does and does NOT claim to cover."""
    o, t = _profile(original), _profile(translated)
    violations: "list[str]" = []
    if t["nums"] != o["nums"]:
        violations.append(
            f"number tokens changed ({sorted(o['nums'].elements())} → {sorted(t['nums'].elements())})"
        )
    if t["dates"] != o["dates"]:
        violations.append(
            f"date tokens changed ({sorted(o['dates'].elements())} → {sorted(t['dates'].elements())})"
        )
    if t["pct"] != o["pct"]:
        violations.append(f"'%' count changed ({o['pct']} → {t['pct']})")
    extra_kw = t["keywords"] - o["keywords"]
    if extra_kw:
        violations.append(f"introduced status keyword(s) {sorted(extra_kw)}")
    return violations


def translate_report(
    view: ProjectView,
    fmt: str = "html",
    *,
    target_lang: str,
    translator: Translator,
    now=None,
    source_lang: str = "en",
) -> TranslatedReport:
    """Render `view` in `fmt`, localized to `target_lang` via `translator`, with the
    §4.2 injection-proof guard. Falls back to the English canonical render (never
    raises, never emits a poisoned report) on any shape mismatch, translator error,
    or fact-token violation."""
    canonical = render(view, fmt, now=now)
    if not target_lang or target_lang.strip().lower() == source_lang.strip().lower():
        return TranslatedReport(canonical, target_lang, translated=False, fell_back=False, violations=())

    segments = collect_segments(view)
    if not segments:
        return TranslatedReport(canonical, target_lang, translated=False, fell_back=False, violations=())

    try:
        raw = translator(tuple(segments), target_lang)
    except Exception as exc:  # aqg: top-level boundary — a hostile/broken translator must fail closed
        return _reject(canonical, target_lang, [f"translator raised {type(exc).__name__}"])

    # Require a concrete list/tuple — a bare str / dict / generator would be coerced
    # by list() into chars / keys / unbounded materialization, slipping a corrupt
    # (if fact-free) result past as a "success" (audit c6050ee7 claude-f4 / gpt-f3).
    if not isinstance(raw, (list, tuple)):
        return _reject(canonical, target_lang, [f"translator returned {type(raw).__name__}, not a list/tuple"])
    translations = list(raw)
    if len(translations) != len(segments) or not all(isinstance(t, str) for t in translations):
        return _reject(canonical, target_lang, ["translator returned the wrong shape (count/type)"])

    violations: "list[str]" = []
    for original, translated in zip(segments, translations):
        for v in scan_segment(original, translated):
            violations.append(f"{original!r}: {v}")
    if violations:
        return _reject(canonical, target_lang, violations)

    translated_view = apply_segments(view, dict(zip(segments, translations)))
    report = render(translated_view, fmt, now=now)
    return TranslatedReport(report, target_lang, translated=True, fell_back=False, violations=())


def _reject(canonical: str, lang: str, violations: "list[str]") -> TranslatedReport:
    return TranslatedReport(canonical, lang, translated=False, fell_back=True, violations=tuple(violations))
