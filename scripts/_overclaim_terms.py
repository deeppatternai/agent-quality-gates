#!/usr/bin/env python3
"""Banned causal-claim terms for the outward-narrative overclaim gate (WS-4 item 4).

Single source of truth for the D4×D5 constraint (plan §4 D4×D5 + line 211): until
WS-7's authorized empirical benchmark produces data, AQG's outward narrative must
NOT assert CAUSAL quality outcomes ("fewer bugs", "higher quality", "proven to
reduce…"). It may only describe MECHANISMS ("deterministic gates", "evidence
ledger", "anti-forgetting"). This upgrades that rule from README discipline to a
mechanical check (plan R1-Cluster I): WS-0/G0-⑥ scans README + outward docs for
these terms; a hit fails closed.

Design (hardened after audit 6d0db06d, 4/4 convergent):
- HONEST NEGATIONS PASS. `scan_text` drops any match preceded by a negation token
  ("not proven to reduce", "does not reduce bugs", "无法减少缺陷"). This is the
  load-bearing precision guarantee for a fail-closed gate — it must not block
  AQG's own disclaimer copy.
- Terms are OBJECT-ANCHORED (a defect/quality noun must be present) so legitimate
  non-quality reductions pass: "40% fewer open PRs", "reduce merge conflicts",
  "improved quality gates" (mechanism) are NOT flagged.
- Bare words like "proven" / "guarantee" / "reliable" are never banned on their
  own — only the CAUSAL quality construction.
- Latin matches case-insensitively (incl. inside CJK, so "更少的 BUG" is caught).
- RESIDUAL RISK (honest): this is a known-terms net — necessary, not sufficient. A
  novel wording can slip past; it feeds WS-0/G0-⑥ + a human moat sign-off, it is
  not a proof of honesty. New phrasings are added as caught.
- SCOPE (which files) is the caller's job (scan_overclaim.py), which also excludes
  append-only history (LOG.md/CHANGELOG…) per plan R2-4.
"""
from __future__ import annotations

import re
from typing import Pattern

_I = re.IGNORECASE

# A match is dropped when the text BEFORE it on the same line ends with a negation
# token (within a short window). Keeps honest disclaimers ("not proven to reduce")
# out of a fail-closed gate.
_NEG_EN = re.compile(
    r"(?:\b(?:not|no|never|cannot|without|nor|isn't|aren't|wasn't|weren't|doesn't|"
    r"don't|didn't|won't|can't|couldn't|wouldn't|shouldn't)\b|n['’]t\b)[\s\w,'’-]{0,28}$",
    _I,
)
_NEG_ZH = re.compile(r"(?:不能|无法|并未|并不|没有|没|未|不|无)[^。！？!?\n]{0,8}$")

# id -> (compiled regex, rationale). Latin terms are case-insensitive; the regexes
# are phrase- and object-anchored so honest negations / unrelated uses pass.
CAUSAL_CLAIM_TERMS: "dict[str, tuple[Pattern[str], str]]" = {
    # --- English: fewer/less <defect-noun> ---
    "fewer_defects": (
        re.compile(r"\bfewer\s+(?:bugs|defects|errors|issues|mistakes|regressions|failures|vulnerabilities)\b", _I),
        "causal defect-reduction claim — object-anchored to a defect noun",
    ),
    "less_defects": (
        re.compile(r"\bless\s+(?:bugs|defects|errors)\b", _I),
        "causal defect-reduction claim ('less bugs' marketing form)",
    ),
    "num_fewer": (
        re.compile(r"\b\d+(?:\.\d+)?\s*(?:x|%|times|percent)\s+(?:fewer|less)\s+"
                   r"(?:bugs|defects|errors|issues|regressions|failures)\b", _I),
        "quantified defect-reduction claim (e.g. '2x fewer bugs', '40% fewer defects')",
    ),
    # --- English: <reduce-verb> ... <defect-noun> ---
    "reduce_defects": (
        re.compile(r"\b(?:reduc\w*|cut\w*|decreas\w*|eliminat\w*|prevent\w*|slash\w*|lower\w*)\s+"
                   r"(?:(?:the|your|its|all|overall)\s+)?(?:number\s+of\s+)?"
                   r"(?:bugs|defects|errors|regressions|failures|vulnerabilities)\b", _I),
        "causal defect-reduction claim (verb + defect noun; non-defect objects pass)",
    ),
    "reduction_in": (
        re.compile(r"\b(?:\d+(?:\.\d+)?\s*%?\s*)?reduction\s+in\s+(?:bugs|defects|errors|regressions)\b", _I),
        "quantified defect-reduction claim ('40% reduction in defects')",
    ),
    "catch_more": (
        re.compile(r"\b(?:catch(?:es)?|find(?:s)?|surfac(?:es?|ing)?|detect(?:s)?)\s+more\s+"
                   r"(?:bugs|defects|errors)\b", _I),
        "causal 'catches more bugs' claim",
    ),
    # --- English: higher/improved quality (OUTCOME, not mechanism) ---
    "higher_quality": (
        re.compile(r"\b(?:higher|better|improved|improv(?:es?|ing)|boosts?|raises?|increas(?:es?|ing))\s+"
                   r"(?:code\s+)?quality\b(?!\s*(?:gate|gates|bar|standard|standards|signal|feedback|"
                   r"evidence|assurance|control|of\s+life))", _I),
        "causal quality-improvement claim (mechanism nouns like 'quality gate' excluded)",
    ),
    "reliability_outcome": (
        re.compile(r"\b(?:improv(?:es?|ing)|boosts?|increas(?:es?|ing)|greater|higher)\s+"
                   r"(?:code\s+)?reliab(?:ility|le)\b", _I),
        "causal reliability-improvement claim",
    ),
    # --- English: proof / guarantee constructions (bare words are allowed) ---
    "proven_to": (
        re.compile(r"\bproven\s+to\s+(?:reduce|improve|lower|prevent|increase|deliver|cut|catch\s+more)\b", _I),
        "causal proof claim without published evidence",
    ),
    "battle_tested_to": (
        re.compile(r"\bbattle[- ]tested\s+to\s+(?:reduce|prevent|catch)\b", _I),
        "causal 'battle-tested to reduce' claim",
    ),
    "guarantees_quality": (
        re.compile(r"\bguarantee[sd]?\b[^.\n]{0,30}\b(?:quality|fewer|reliab)", _I),
        "quality guarantee claim (honest negations excluded by the negation guard)",
    ),
    # --- Chinese (zh-CN twin narrative); _I so embedded 'BUG' matches ---
    "zh_fewer_defects": (
        re.compile(r"更少\s*(?:的)?\s*(?:缺陷|bug)", _I),
        "更少缺陷 — 因果减陷结论",
    ),
    "zh_defects_fewer": (
        re.compile(r"(?:缺陷|bug)\s*更少", _I),
        "缺陷更少 — 因果减陷结论",
    ),
    "zh_reduce_defects": (
        re.compile(r"(?:减少|降低|减低|消除)\s*(?:了)?\s*(?:代码)?\s*(?:缺陷|bug|错误率)", _I),
        "减少缺陷 — 因果减陷结论（错误率而非裸'错误'，减少噪音）",
    ),
    "zh_higher_quality": (
        re.compile(r"(?:更高|更好|提高|提升|改善)[^。\n]{0,4}?质量|质量\s*(?:更高|更好|提升)"),
        "更高/提升质量 — 因果提质结论",
    ),
    "zh_more_reliable": (
        re.compile(r"更\s*可靠|可靠性\s*(?:更高|提升|提高)|(?:提高|提升)\s*(?:了)?\s*(?:代码|系统)?\s*可靠性"),
        "更可靠 — 因果提可靠性结论",
    ),
    "zh_proven_more": (
        re.compile(r"证明\s*(?:了)?\s*更\s*(?:少|高|好|可靠)"),
        "证明更… — 无据因果结论（不含'更快'=性能非质量）",
    ),
}


def _is_negated(prefix: str) -> bool:
    """True if the text preceding a match ends with a negation token."""
    return bool(_NEG_EN.search(prefix) or _NEG_ZH.search(prefix))


def scan_text(text: str) -> "list[tuple[str, int, str, str]]":
    """Return hits as (term_id, line_no, matched_text, line_text). Pure — no IO.

    Matches preceded by a negation token on the same line are dropped (honest
    disclaimers pass). line_no is 1-based; empty list = clean.
    """
    hits: list[tuple[str, int, str, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for term_id, (rx, _rationale) in CAUSAL_CLAIM_TERMS.items():
            for m in rx.finditer(line):
                if _is_negated(line[: m.start()]):
                    continue
                hits.append((term_id, i, m.group(0), line.strip()))
                break  # one hit per term per line is enough for the gate
    return hits
