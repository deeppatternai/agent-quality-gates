#!/usr/bin/env python3
"""aqg-session-handoff — generic cross-session handoff (development source).

Two subcommands (mirroring the dual modes of aqg-multi-review / aqg-evidence-closeout):

- `new`      → print the 8-section markdown skeleton (each slot a `<FILL: ...>` sentinel);
               Done-so-far is **prefilled** from the `.aqg/current_ledger.md`
               construction ledger (graceful no-op when missing).
- `validate` → read the filled-in handoff from **STDIN**, gate it rule-by-rule R1-R8, exit 0/1.

Mandatory workflow (stated in SKILL.md): `new → fill by hand → validate → paste`. validate is
the pre-paste gate (whoever runs it gets blocked; "running it" relies on discipline — the skill
is a read-only CLI and does not enforce it technically).

impl-audit d7a8899c fixes: A (violation does not echo back the raw secret) / B+F (FILL sentinel) /
C (exact-contract parse) / D (cross-section none-token) / E (R8 relaxed) / G (input-size soft gate).
boundary_class: read-only — prints to stdout / reads from stdin; never writes the filesystem.

Exit codes:
  0: success — `new` prints the skeleton OR `validate` gate passes
  1: validation failure — `validate` hits any gate violation (R1-R8)
  2: usage error — argparse propagates exit 2 on bad args
  3: reserved — config/schema error (not currently emitted)
  70: reserved — internal error placeholder
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# ---- Section contract (a3 §3.4) ------------------------------------------

# (number, name, kind) — kind ∈ {"required", "none-able"}
SECTIONS: list[tuple[str, str, str]] = [
    ("1", "Goal", "required"),
    ("2", "Environment", "required"),
    ("3", "Done so far", "required"),
    ("4", "Current Working State", "none-able"),
    ("5", "Next", "required"),
    ("6", "Discipline traps", "none-able"),
    ("7", "First step", "required"),
    ("8", "Open decisions", "none-able"),
]

SECTION_NAMES: list[str] = [name for _, name, _ in SECTIONS]
REQUIRED_SECTIONS: frozenset = frozenset(n for _, n, k in SECTIONS if k == "required")
NONE_ABLE_SECTIONS: frozenset = frozenset(n for _, n, k in SECTIONS if k == "none-able")

# Minimal/degraded handoff (design absorption — CTX-exhausted path): the 3 sections a
# cold-start reader needs FIRST — mission, in-progress stop-point, first action. Numbers
# are kept from the full contract so parse/validate round-trip unchanged.
MINIMAL_SECTIONS: list[str] = ["Goal", "Current Working State", "First step"]

# Soft length advisory (design self-check 12): handoffs are paste-prompts; exceeding
# this is a redundancy smell. NON-failing — surfaces a warning, never a violation
# (complex tasks may legitimately exceed; the nudge is to run the deletion test).
SOFT_MAX_LINES = 150

# Continuity footer (Owner 2026-06-11): `new` appends this after section 8 so the
# handoff carries the AQG discipline FORWARD — invoke the skills rather than manual
# equivalents, but do not treat the footer itself as a request to create another handoff.
# A separately observed trigger must come first; only then does the next session use this
# skill again (breaking the freestyle-degradation chain). validate strips the footer at
# the sentinel before section parsing; the secret scan still runs on the full text.
HANDOFF_FOOTER_SENTINEL = "<!-- AQG-CONTINUITY-FOOTER -->"
HANDOFF_FOOTER = (
    f"\n{HANDOFF_FOOTER_SENTINEL}\n"
    "---\n"
    "**Continuity discipline (AQG · do not delete this block)**\n"
    "- **This continuity footer is not a handoff request or trigger.** A pasted handoff is "
    "continuation context. Only after a separate observable trigger (an explicit user request for a handoff, "
    "a host PreCompact/stop warning, or visible CTX use >=95%) should you create another handoff; "
    "when that happens, use `aqg-session-handoff new` + `validate`; don't freestyle the md.\n"
    "- Don't just read — act: as the first step, directly **invoke** the `aqg-startup-preflight` "
    "skill (inside the **work repo**, not cwd); don't treat a manual `git status` / `gh api` as the "
    "equivalent. Do the work through `aqg-code-construction`; close out with `aqg-evidence-closeout`.\n"
)
# The footer body that follows the sentinel — what `validate` recognises and peels off
# section 8. Derived from HANDOFF_FOOTER so it can never drift out of sync (a hand-kept
# copy would silently stop matching the moment the footer text is edited).
_FOOTER_TEMPLATE = HANDOFF_FOOTER.split(HANDOFF_FOOTER_SENTINEL, 1)[1]
# (num, name) pairs that count as real section boundaries (impl-audit C: a body
# line like `## 2. ran tests` is NOT a contract header, so it does not mis-split).
_CONTRACT_HEADERS: frozenset = frozenset((num, name) for num, name, _ in SECTIONS)

# Per-section explicit none-tokens (a3 §3.5 R5 — section-specific, normalized).
# `worktree clean` is meaningful ONLY for Current Working State (F2).
NONE_TOKENS: dict = {
    "Current Working State": frozenset({"none", "worktree clean"}),
    "Discipline traps": frozenset({"none", "n/a"}),
    "Open decisions": frozenset({"none", "n/a"}),
}
# Union of all none-tokens — required sections may NOT be solely one of these (R4/F14),
# and a none-able section may NOT use another section's none-token (impl-audit D).
ALL_NONE_TOKENS: frozenset = frozenset().union(*NONE_TOKENS.values())

# First-step placeholder blacklist (a3 §3.5 R6) — whole-section normalized equals,
# NOT substring (self-review: avoid killing "none of the tests pass, rerun X").
FIRST_STEP_BLACKLIST: frozenset = frozenset({
    "继续之前的活", "接着做", "continue where left off", "continue", "tbd",
    "同上", "见上文", "none", "n/a",
})

# One-line fill hints per section (`new` output — each is wrapped in `<FILL: ...>`).
SECTION_HINTS: dict = {
    "Goal": "one-line goal for this thread",
    "Environment": "repo/branch/language-version gotchas/authorization constraints/key commands; secret-free",
    "Done so far": (
        "what's done + verification evidence + durable state (PR/issue/doc) + audit state "
        "(audit id or an explicit `no audit`); if a construction ledger was prefilled, add on top of it"
    ),
    "Current Working State": (
        "uncommitted/dirty files, failing tests, half-broken intermediate state, "
        "running background processes (shell ID + kill command) / dev server + port / open worktrees; "
        "if all clean, put 'worktree clean' or 'none'; "
        "indent multi-line commands/intermediate code by 4 spaces, don't use triple-backtick fences (they break the paste copy-box)"
    ),
    "Next": "remaining/next steps, priority-ordered; indent multi-line commands by 4 spaces, don't use triple-backtick fences",
    "Discipline traps": "the CI/discipline traps on this thread; 'none' if none",
    "First step": "the next relay's first concrete action (usually run preflight to confirm worktree/state before acting); cannot be a placeholder like 'continue where left off'; indent multi-line commands by 4 spaces, don't use triple-backtick fences (they break the paste copy-box)",
    "Open decisions": "points awaiting a ruling + name the actor (Owner/a specific name/@user); 'none' if none",
}

# Header format = single source of truth for new↔validate round-trip (a3 §3.4/F6).
HEADER_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$", re.MULTILINE)

# Fill-slot sentinel (a3 §3.5 R2). Distinctive `<FILL: ...>` marker so R2 detects
# unfilled slots of ANY length (impl-audit B: the long Done-so-far slot) WITHOUT
# false-positiving on legitimate angle-bracket content like `git checkout <branch>`
# or `<jeff@x.com>` (impl-audit F).
PLACEHOLDER_RE = re.compile(r"<FILL:[^>\n]*>")

# Max stdin bytes before validate skips the secret scan (impl-audit G — handoffs
# are small paste-prompts; an oversized input is abnormal + a ReDoS surface).
MAX_INPUT_BYTES = 256_000

# Actor presence tokens (a3 §3.5 R7) — structural existence check, NOT semantics.
_ACTOR_LITERALS: tuple = ("owner", "the maintainer")
_AT_NAME_RE = re.compile(r"@\w+")
_ROLE_PREFIX_RE = re.compile(r"^\s*-?\s*[A-Z][A-Za-z0-9_-]+:")

# Done-so-far audit-state token (a3 §3.5 R8 / impl-audit E) — broad label presence
# (any mention of audit / 审), NOT a literal `audit_id` (avoids false R8 on
# "audit id: 123" / "audit: x" / "审计 done").
_AUDIT_PRESENT_RE = re.compile(r"audit|审", re.IGNORECASE)


# ---- Secret bank (copied verbatim from aqg-evidence-closeout, fail-closed) ---
# Source of truth: skills/aqg-evidence-closeout/scripts/aqg_closeout.py:142-191
# (audit 6529b5d4 C4). Kept local so the gate has no cross-skill import dependency.

_LOCAL_SECRET_PATTERNS: list = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "AWS temporary key"),
    (re.compile(r"gh[posur]_[A-Za-z0-9_]{36,}"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "GitHub fine-grained PAT"),
    (re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{24,}"), "Stripe API key"),
    (re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}"), "OpenAI project key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI API key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "PEM private key",
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=+/-]{10,}"
        ),
        "JWT",
    ),
]


def redact(text: str) -> str:
    """Mask any secret pattern with `[REDACTED]` before it is echoed.

    impl-audit A / round-2 F2: the gate must NEVER print a raw secret, even via a
    rule (R2/R4/R5/R6/R7) that interpolates user-supplied section content into its
    violation message. Every such interpolation routes through here first.
    """
    out = text
    for pattern, _ in _LOCAL_SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def scan_secrets(text: str) -> list:
    """Return sorted unique [(lineno, type)] for secret hits (1-indexed).

    Scans the WHOLE text per pattern (not per-line) so multi-line PEM blocks are
    caught; maps each match start back to its line number. NEVER returns the
    matched secret text — only (lineno, type) — so the gate cannot re-leak it
    (a3 §3.5 R3 / round-2 F2). The `[REDACTED:<type>]` markers that closeout may
    have left in prefilled content are NOT secret-shaped, so they do not
    self-match (F12).
    """
    hits = set()
    for pattern, name in _LOCAL_SECRET_PATTERNS:
        for m in pattern.finditer(text):
            lineno = text.count("\n", 0, m.start()) + 1
            hits.add((lineno, name))
    return sorted(hits)


# ---- Text helpers --------------------------------------------------------

def _normalize(s: str) -> str:
    """Trim + case-fold for whole-section token equality (a3 §3.5 match semantics)."""
    return s.strip().lower()


def _consume_footer_template(tail: str) -> str | None:
    """Peel a whitespace-insensitive leading match of the continuity-footer body.

    `tail` is everything after the LAST footer sentinel. If it begins with the footer
    template — ignoring whitespace, so a round-trip that re-wraps the footer's long
    bullet lines still matches (the exact-suffix `endswith()` this replaced broke on any
    inserted newline, leaving the whole footer inside section 8 → spurious R5/R7) —
    return the remainder AFTER the template (usually ""; non-empty only when content was
    appended below the footer). Return None when `tail` is NOT a footer body (the last
    sentinel was quoted in section text, or the footer was hand-edited past a re-wrap),
    so the caller declines to strip. O(len(tail)) char scan — no regex / ReDoS.
    """
    i, n = 0, len(tail)
    for ch in _FOOTER_TEMPLATE:
        if ch.isspace():
            continue  # template whitespace is not required to appear in `tail`
        while i < n and tail[i].isspace():
            i += 1     # skip any (re-wrapped) whitespace in `tail`
        if i >= n or tail[i] != ch:
            return None
        i += 1
    return tail[i:]


def parse_sections(text: str) -> tuple:
    """Return (sections, ordered_names) from a rendered handoff.

    Only `## N. Name` headers whose (number, name) is in the contract count as
    boundaries (impl-audit C: a body line like `## 2. ran tests` is NOT a contract
    header, so it does not mis-split). Preamble before the first contract header
    (e.g. the minimal banner) is skipped (R1 / F6). First body wins on a duplicate
    name; the duplicate still surfaces in `ordered_names` so R1 can reject it.
    """
    boundaries = [
        m for m in HEADER_RE.finditer(text)
        if (m.group(1), m.group(2).strip()) in _CONTRACT_HEADERS
    ]
    sections: dict = {}
    ordered: list = []
    for i, m in enumerate(boundaries):
        name = m.group(2).strip()
        start = m.end()
        end = boundaries[i + 1].start() if i + 1 < len(boundaries) else len(text)
        ordered.append(name)
        if name not in sections:  # first body wins (duplicate flagged by R1)
            sections[name] = text[start:end].strip()
    return sections, ordered


def _line_has_actor(line: str) -> bool:
    """Structural actor-token presence (a3 §3.5 R7) — not semantic correctness."""
    low = line.lower()
    if any(a in low for a in _ACTOR_LITERALS):
        return True
    if _AT_NAME_RE.search(line):
        return True
    if _ROLE_PREFIX_RE.search(line):
        return True
    return False


# ---- new mode ------------------------------------------------------------

def _fill_slot(hint: str) -> str:
    """Render a fill slot with the distinctive sentinel (R2 detects it)."""
    return f"<FILL: {hint}>"


def _prefill_done_so_far(repo: Path) -> str:
    """Done-so-far prefill from construction ledger (a3 §3.2/§3.7, C4/F8).

    PREFILL not a full closeout-evidence package: surface the ledger's task/created
    header (redacted, for staleness judgment per C4) + leave a `<FILL: ...>` slot
    for the agent to add fresh verification / durable state / audit state. Graceful:
    missing ledger → plain slot, never errors (F4/F8). The slot is ALWAYS emitted so
    R2 catches an unfilled Done-so-far regardless of ledger (impl-audit B).
    """
    slot = _fill_slot(SECTION_HINTS["Done so far"])
    ledger = repo / ".aqg" / "current_ledger.md"
    try:
        if not ledger.is_file():
            return slot
        head = ledger.read_text(encoding="utf-8", errors="replace").splitlines()[:25]
    except OSError:
        return slot
    task = created = None
    for line in head:
        m = re.search(r"task[_\s-]*(?:slug|name)?\s*[:=]\s*(.+)", line, re.IGNORECASE)
        if m and task is None:
            task = m.group(1).strip()
        m = re.search(r"created[_\s-]*(?:at)?\s*[:=]\s*(.+)", line, re.IGNORECASE)
        if m and created is None:
            created = m.group(1).strip()
    if task is None and created is None:
        return f"construction ledger present at `.aqg/current_ledger.md` — review its 6-step/objection as the basis. {slot}"
    surfaced = redact(f"construction ledger prefill — task: {task or '?'} (created: {created or '?'})")
    # NB (V3): keep audit/审 OUT of this non-slot prefix so the boilerplate cannot
    # self-satisfy R8; the audit prompt lives inside the FILL slot (R2 catches unfilled).
    return f"{surfaced} — after judging freshness, fill this session's verification / durable state / review conclusion in the slot below. {slot}"


def render_skeleton(repo: Path) -> str:
    """Render the 8-section skeleton (a3 §3.4 Exact Output Template)."""
    lines: list = []
    for num, name, _ in SECTIONS:
        lines.append(f"## {num}. {name}")
        if name == "Done so far":
            lines.append(_prefill_done_so_far(repo))
        else:
            lines.append(_fill_slot(SECTION_HINTS[name]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n" + HANDOFF_FOOTER


# Minimal-mode banner + focused per-section hints (design §4 degraded path).
_MINIMAL_BANNER = (
    "> ⚠ Minimal handoff generated under CTX pressure (degraded) — before the first step, the "
    "next relay should collect git status / test state itself to verify. With budget, use `new` "
    "without --minimal for the full 8 sections."
)
_MINIMAL_HINTS: dict = {
    "Goal": "one-line goal for this thread + done criteria",
    "Current Working State": "🟡 in-progress items + exact stop-point (file:line/error/half-done state) + one-line current strategy/assumption",
    "First step": "the next relay's first concrete action (a single command/single file operation); cannot be a placeholder like 'continue where left off'",
}


def render_minimal_skeleton() -> str:
    """Render the 3-section degraded skeleton for the CTX-exhausted path (design §4).

    Mission + in-progress stop-point + first action — the minimum a cold reader needs
    to act. Skips collection + full self-check, but the author must still secret-scan
    (validate --minimal keeps R2/R3). Section numbers kept from the full contract.
    """
    lines: list = []
    lines.append(_MINIMAL_BANNER)
    lines.append("")
    for num, name, _ in SECTIONS:
        if name not in MINIMAL_SECTIONS:
            continue
        lines.append(f"## {num}. {name}")
        lines.append(_fill_slot(_MINIMAL_HINTS[name]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n" + HANDOFF_FOOTER


def cmd_new(args: argparse.Namespace) -> int:
    repo = Path(args.repo or ".").resolve()
    minimal = getattr(args, "minimal", False)
    skeleton = render_minimal_skeleton() if minimal else render_skeleton(repo)
    if args.json:
        print(json.dumps(
            {"mode": "new", "minimal": minimal, "skeleton": skeleton,
             "sections": MINIMAL_SECTIONS if minimal else SECTION_NAMES},
            indent=2, ensure_ascii=False,
        ))
        return 0
    print(skeleton, end="")
    return 0


# ---- validate mode (gate: R1-R8) -----------------------------------------

def _has_code_fence(text: str) -> bool:
    """True if `text` has a Markdown code-fence line (0-3 leading spaces then a
    triple-backtick run). Such a fence in a handoff body breaks a single paste
    copy-box: the inner fence closes the outer one, so the next reader cannot
    select the whole handoff at once. The copy-safe form is 4-space-indented code.
    """
    for ln in text.splitlines():
        stripped = ln.lstrip(" ")
        if len(ln) - len(stripped) <= 3 and stripped.startswith("```"):
            return True
    return False


def validate_handoff(text: str, minimal: bool = False) -> dict:
    """Gate a filled handoff (a3 §3.5 R1-R8). Return {valid, violations, warnings}.

    Structural-present + light token heuristics only (C2 — no semantic 'specificity'
    judgment). Rules grouped: (b) generic structure R1/R2/R3, (c) per-section value
    R4-R8. Every message that echoes user content routes through redact() first
    (impl-audit A). `minimal=True` gates the degraded 3-section handoff (design §4):
    R1 expects MINIMAL_SECTIONS; R2/R3 + per-section rules on the present sections still
    apply (the secret scan is never skipped). `warnings` is non-failing advice.
    """
    expected = MINIMAL_SECTIONS if minimal else SECTION_NAMES
    # impl-audit G/V2: oversized input — bail BEFORE any parse/regex/redact runs
    # (not just the secret scan). cmd_validate also bounds the stdin read.
    if len(text) > MAX_INPUT_BYTES:
        return {"valid": False, "warnings": [], "violations": [
            f"R3 input too large ({len(text)}B > {MAX_INPUT_BYTES}B) — shorten the handoff and rerun (all rules skipped)"
        ]}

    violations: list = []
    warnings: list = []
    # Peel the continuity footer off the body before section parsing so its `---` /
    # title / discipline bullets never get counted as section-8 decision lines (spurious R5/R7).
    # Anchor on the LAST sentinel — a body that quotes the sentinel still keeps its real
    # footer at the end (audit 2c4654ce f1(a)) — and match the footer body
    # whitespace-insensitively: a round-trip re-wraps the footer's long bullet lines and
    # the exact `endswith()` this replaced broke on any inserted newline, leaving the whole
    # footer inside section 8 (the R7 false-positive this fixes). Content appended BELOW the
    # footer is folded back into the body, NOT dropped, so the section rules still see it
    # (audit 2c4654ce f1(b): no unvalidated tail — a folded contract-header dup trips R1,
    # a folded plain / non-contract line trips R7). R2/R3 below scan the full original text.
    # Known edge (rare, strictly better than the old code, NOT a regression): residue that
    # itself contains the sentinel makes rpartition anchor PAST the real footer → no strip
    # → R7 may false-positive on the real footer. The old endswith() did this for ALL
    # residue; this does it only for residue-with-sentinel (audit 1444cdb8 gpt-5.5 f1).
    head, sentinel, tail = text.rpartition(HANDOFF_FOOTER_SENTINEL)
    if sentinel:
        residue = _consume_footer_template(tail)
        body_text = head + residue if residue is not None else text
    else:
        body_text = text
    sections, ordered = parse_sections(body_text)

    # Soft length advisory (design self-check 12) — non-failing nudge to run the deletion
    # test; complex tasks may legitimately exceed. Count author content (footer excluded).
    n_lines = len(body_text.splitlines())
    if n_lines > SOFT_MAX_LINES:
        warnings.append(
            f"handoff {n_lines} lines > soft max {SOFT_MAX_LINES} — run the deletion test to confirm it's not redundant "
            "(genuinely complex tasks may exceed)"
        )

    # Copy-box gate (BLOCKING): a triple-backtick fence in the body breaks the single
    # paste box — the inner fence closes the outer one, so the next reader cannot select
    # the whole handoff at once (the recurring "section truncated" report). The skeleton
    # hint + SKILL.md fill-in guidance steer the author to 4-space-indent up front; this fails
    # CLOSED so a fenced handoff cannot pass validate and be pasted broken (a non-blocking
    # warning let it through and the break kept recurring). The footer is peeled off
    # body_text above, so its own formatting never trips this.
    if _has_code_fence(body_text):
        violations.append(
            "body contains a triple-backtick fenced code block — it breaks the paste copy-box (the inner "
            "fence closes the outer box early, so the whole handoff can't be copied at once); switch "
            "multi-line commands/code to 4-space indent and rerun validate"
        )

    # R1 — exactly the contract headers, in order, no duplicate/extra (impl-audit C;
    # MINIMAL_SECTIONS when minimal=True).
    if ordered != expected:
        missing = [n for n in expected if n not in sections]
        if missing:
            violations.append(f"R1 missing section header: {', '.join(missing)}")
        else:
            violations.append(
                f"R1 section headers don't match the contract (must be exactly {len(expected)} sections, in order, no duplicates); got order: {ordered}"
            )

    # R2 — unfilled `<FILL: ...>` sentinel residue (redacted; impl-audit A/B/F).
    spans = sorted({m.group(0) for m in PLACEHOLDER_RE.finditer(text)})
    if spans:
        violations.append(f"R2 unfilled placeholder residue: {', '.join(redact(s) for s in spans[:5])}")

    # R3 — secret scan (lineno + type ONLY, never the raw secret; F2).
    for lineno, name in scan_secrets(text):
        violations.append(
            f"R3 secret detected line {lineno}: [REDACTED:{name}] — remove it or switch to an env-var reference"
        )

    # (c) per-section value rules.
    for name, body in sections.items():
        norm = _normalize(body)
        if name in REQUIRED_SECTIONS:
            # R4 — required = real content: non-empty, not solely a none-token.
            if not body:
                violations.append(f"R4 required section `{name}` is empty")
            elif norm in ALL_NONE_TOKENS:
                violations.append(f"R4 required section `{name}` cannot be just a none-token (`{redact(body.strip())}`)")
        elif name in NONE_ABLE_SECTIONS:
            # R5 — real content OR THIS section's none-token; blank / wrong-token → fail.
            allowed = NONE_TOKENS.get(name, frozenset({"none"}))
            if not body:
                violations.append(f"R5 none-able section `{name}` is blank (forgot to fill? put an explicit none)")
            elif norm in ALL_NONE_TOKENS and norm not in allowed:
                violations.append(
                    f"R5 `{name}` used a none-token not valid for this section (`{redact(body.strip())}`); this section accepts {sorted(allowed)}"
                )

        # R6 — First step blacklist (whole-section normalized equals).
        if name == "First step" and norm in FIRST_STEP_BLACKLIST:
            violations.append(f"R6 First step is a placeholder/vague phrase (`{redact(body.strip())}`) — write a concrete action")

        # R7 — Open decisions actor. Count ONLY; NEVER echo line content — a line
        # could carry a multi-line PEM block that per-line redact() cannot mask (V1).
        if name == "Open decisions" and body:
            if norm not in NONE_TOKENS["Open decisions"]:
                bad = [ln for ln in body.splitlines() if ln.strip() and not _line_has_actor(ln)]
                if bad:
                    violations.append(
                        f"R7 Open decisions: {len(bad)} line(s) don't name an actor (each must include Owner/a specific name/@user)"
                    )

        # R8 — Done-so-far audit token (broad label presence OR explicit no-audit; impl-audit E).
        if name == "Done so far" and body and not _AUDIT_PRESENT_RE.search(body):
            violations.append("R8 Done so far is missing audit state (mention `audit` / `审`, or an explicit `no audit` / `无 audit`)")

    return {"valid": not violations, "violations": violations, "warnings": warnings}


def cmd_validate(args: argparse.Namespace) -> int:
    minimal = getattr(args, "minimal", False)
    # Bounded read (V2): cap memory at MAX_INPUT_BYTES+1; the +1 lets
    # validate_handoff detect (and early-reject) an oversized input.
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    text = raw.decode("utf-8", errors="replace")
    result = validate_handoff(text, minimal=minimal)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["valid"] else 1
    # Non-failing advisories (soft length …) print regardless of pass/fail.
    for w in result.get("warnings", []):
        print(f"  ⚠ {w}", file=sys.stderr)
    if result["valid"]:
        n = len(MINIMAL_SECTIONS) if minimal else len(SECTION_NAMES)
        print(f"✓ handoff valid — {n} sections present, no placeholder/secret residue, gates pass.")
        return 0
    print("✗ handoff INVALID — fix before paste:", file=sys.stderr)
    for v in result["violations"]:
        print(f"  - {v}", file=sys.stderr)
    return 1


# ---- main ----------------------------------------------------------------

def main() -> int:
    # Encoding robustness: on Windows the std streams default to cp936, so Chinese
    # output would mojibake / raise UnicodeEncodeError. Force UTF-8 on output —
    # effectively a no-op on Linux/macOS (already UTF-8); errors="replace" only
    # affects the rare undecodable char, and never crashes. stdin stays strict so
    # malformed piped input surfaces instead of being silently replaced.
    # Same approach as aqg_re_anchor.py; regression: tests/test_win_utf8_stdout.py.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8")  # strict: surface bad input
    except (AttributeError, ValueError, OSError):
        pass
    p = argparse.ArgumentParser(
        prog="aqg_session_handoff.py",
        description=(
            "AQG generic cross-session handoff — a paste-ready session-to-session handoff for any "
            "project (no EAF engine state; for an EAF workspace use eaf-session-handoff)."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    new_p = sub.add_parser("new", help="print the 8-section handoff skeleton")
    new_p.add_argument("--repo", default=None,
                       help="repo root for ledger prefill (default: cwd)")
    new_p.add_argument("--minimal", action="store_true",
                       help="CTX-pressure degraded mode: only 3 sections (mission / 🟡 stop-point / first step)")
    new_p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    new_p.set_defaults(func=cmd_new)

    val_p = sub.add_parser("validate", help="read a handoff from stdin and gate it")
    val_p.add_argument("--minimal", action="store_true",
                       help="gate the degraded 3-section handoff (R1 expects 3 sections; secret scan still runs)")
    val_p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    val_p.set_defaults(func=cmd_validate)

    args = p.parse_args()
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: unexpected: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
