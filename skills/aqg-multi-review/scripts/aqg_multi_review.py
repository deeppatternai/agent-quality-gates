#!/usr/bin/env python3
"""aqg-multi-review — 5-dimension code review router (development source).

This is the open development source: the decision logic lives here in this
local script. The future cloud release form (`aqg_cloud/skills/multi_review.py`)
is a thin client repackaged from this once mature, with the logic moved
server-side to protect it. `tests/test_multi_review.py` guards that the two stay
behavior-identical (this source currently leads). PyYAML is the only dependency;
this script is also what CI runs to verify that parity.

Two subcommands:
- `new`      → emit YAML skeleton + 5 dim-specific focus prompts
- `validate` → parse YAML ledger, emit needs_llm_judgement signal

Per ADR §5: this script does NOT call audit-mcp /audit. Caller (human /
Claude / EAF) runs /audit per dim with focus_prompt; this script is
the dispatcher + ledger validator.

Mirror source: the cloud backend commit b4d2262 (post-audit 970c0e6a hardening).
Tests guard cross-impl parity: see tests/test_multi_review.py.

Exit codes:
  0: success — `new` printed skeleton OR `validate` ledger valid
  1: validation failure — `validate` found schema / missing-dim violations in ledger
  2: usage error — argparse propagates exit 2 on bad args / unknown subcommand
  3: reserved — config or schema error (not currently emitted)
  70: reserved — internal error placeholder for future use
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


# ---- Constants (mirror cloud module) -------------------------------------

DIMENSIONS: list[str] = ["logic", "edge_cases", "security", "performance", "concurrency"]

VALID_SEVERITIES: set[str] = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}

# WS-4 §9-5: the only recognized engine-unavailable fallback marker. A fallback
# ledger is a single-model, same-vendor, non-independent rough eval — never a
# cross-vendor panel (Cluster J anti-overclaim constraint).
FALLBACK_MODE_SESSION_LLM: str = "session-llm"
FALLBACK_LABEL: str = "single-model, same-vendor, non-independent, rough-eval-only"

# Per-dim focus prompts — caller passes these as /audit focus=... parameter.
FOCUS_PROMPTS: dict[str, str] = {
    "logic": (
        "Application logic correctness only. Scan for: off-by-one; conditional "
        "branch coverage gaps; return value consistency; state mutation order; "
        "non-idempotent re-entry. Do NOT comment on style / performance / "
        "concurrency / security — those have separate audit passes."
    ),
    "edge_cases": (
        "Edge cases only. Scan for: empty / null / max int / unicode boundary; "
        "error path correctness; recovery semantics on partial failure; "
        "resource cleanup on early return / exception. Do NOT comment on "
        "happy-path logic — that's separate."
    ),
    "security": (
        "Security only — OWASP Top 10 + CWE Top 25 patterns. Scan for: "
        "auth/authz check missing; SQL/XSS/SSRF/path-traversal; secret "
        "leakage; deserialization of untrusted data; weak crypto; rate-limit "
        "absent on auth path; input validation. Severity per OWASP risk model. "
        "For deep security ledger walk-through use aqg-security-review."
    ),
    "performance": (
        "Performance only. Scan for: algorithmic complexity (O(n²) where O(n) "
        "would do); N+1 queries; unnecessary loops over same data; redundant "
        "memory allocation in hot path; missing cache opportunities; large "
        "object copy where reference would do. Do NOT comment on micro-opt "
        "(prefer correctness)."
    ),
    "concurrency": (
        "Concurrency / thread safety only. Scan for: race conditions; lock "
        "ordering; missing atomic ops on shared state; goroutine / task / "
        "thread leaks; channel close semantics; non-thread-safe library use "
        "in concurrent context. Do NOT comment on async-style preferences."
    ),
}

# Tier A3 ① (PR #183 absorption): pre-emit verification gate appended to every
# dimension prompt — make external auditors cite the exact line before flagging,
# cutting false positives (gstack + Anthropic /ultrareview "report only what you
# can cite" semantics). Mirror in the cloud backend multi_review.py.
_QUOTE_LINE_GATE: str = (
    " Cite the exact line(s) as evidence before flagging any issue — if you "
    "cannot quote the specific line, do not report it."
)
FOCUS_PROMPTS = {d: p + _QUOTE_LINE_GATE for d, p in FOCUS_PROMPTS.items()}

DECISION_ALIASES: dict[str, str] = {
    "accept": "accept", "accepted": "accept",
    "reject": "reject", "rejected": "reject",
    "needs-cross-llm-rerun": "needs-cross-llm-rerun",
    "needs_cross_llm_rerun": "needs-cross-llm-rerun",
    "needs-rerun": "needs-cross-llm-rerun",
    "rerun": "needs-cross-llm-rerun",
}

# Audit fix gpt-5.5 #3 (audit 970c0e6a): mirror security-review placeholder set.
AUDIT_ID_PLACEHOLDERS: set[str] = {
    "todo", "tbd", "pending", "n/a", "na", "null", "none", "?", "<set>",
}

# Audit fix C1 (gpt-5.5 #1 + gemini #3 convergent): parse evidence into
# (path, line_start, line_end) for line-range overlap detection.
# C1 (audit 6066aaeb): path group allows colons (non-greedy, anchored by the
# trailing :<start>(-<end>)$) so Windows drive-letter paths like C:\x.py:10-20
# parse instead of falling back to raw-string match (which misses overlaps).
_EVIDENCE_RE = re.compile(
    r"^\s*(?P<path>.+?)\s*:\s*(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?\s*$"
)

# PR3 fallback evidence gate — a file:line locator check that blocks PROSE from
# masquerading as a locator. Three audit rounds (47f85142, e904d91d, 2add047b)
# proved a BLOCKLIST regex is whack-a-mole: reusing parse_evidence let "auth code
# somewhere: 10" through; requiring a '.' let "2.0:1"/"10.0.0.1:8080" through;
# requiring a letter-initial extension still let "http://x:8080"/"3.x:1"/
# "os.path:42"/"@scope/pkg:1" through — AND the greedy `[^\s:]+\.` regex was
# quadratic (ReDoS, gemini f1: a 200k-char no-colon string ran ~100s under
# finditer). So this is an ALLOWLIST + LINEAR parser instead:
#   - split on whitespace (O(n)); for each token, rpartition on the last ':'
#     (O(len)); the line side must START WITH A DIGIT (tolerates "10", "10-20",
#     "10,"); the path side's basename must end in an ALLOWLISTED code/config/doc
#     extension. No regex over the whole string -> no catastrophic backtracking.
# This closes every audited prose class (URLs, versions, IPs, ratios, @scope,
# module.attr, host:port incl. the old api.example.com residual).
# Documented FAIL-CLOSED residuals (safe direction; rare in real evidence): an
# extensionless top-level file ("Makefile:10"), a non-allowlisted extension
# ("foo.zig:10", man-page "git.1:120"), and a space after the colon ("x.py: 10")
# are all REJECTED — the producer must cite "<path>.<known-ext>:<line>".
_CODE_EXTENSIONS: frozenset[str] = frozenset({
    "py", "pyi", "ipynb", "js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "rs",
    "java", "kt", "kts", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "cs", "rb",
    "php", "swift", "m", "mm", "scala", "sh", "bash", "zsh", "ps1", "lua", "pl",
    "r", "dart", "ex", "exs", "erl", "clj", "hs", "sql", "proto", "graphql",
    "html", "htm", "css", "scss", "less", "vue", "svelte", "json", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "xml", "md", "rst", "txt", "gradle", "cmake",
    "mk", "tf", "env",
})


def _token_has_locator(token: str) -> bool:
    """A single whitespace-delimited token is a locator iff it splits into
    `<path>:<line>` where line starts with a digit and path's basename ends in an
    allowlisted extension. Pure string ops — linear, no regex backtracking."""
    path, sep, line = token.rpartition(":")
    if not sep or not line[:1].isdigit():
        return False
    basename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem, dot, ext = basename.rpartition(".")
    return bool(dot) and bool(stem) and ext.lower() in _CODE_EXTENSIONS


def evidence_has_locator(evidence: str) -> bool:
    """True iff the evidence CONTAINS a `file:line` token whose extension is a
    known code/config/doc type (v5 §17.7).

    A fail-closed gate, NOT a full path parser: it blocks prose from masquerading
    as a locator. See the module comment above for the three audited bypass
    classes it closes, the ReDoS it avoids, and the documented fail-closed
    residuals (extensionless / exotic-extension files, space-after-colon).
    """
    return any(_token_has_locator(tok) for tok in evidence.split())


def is_real_audit_id(v: Any) -> bool:
    """Truthy audit_id must be non-empty string + non-placeholder."""
    if not isinstance(v, str):
        return False
    s = v.strip().lower()
    return bool(s) and s not in AUDIT_ID_PLACEHOLDERS


def verify_audit_id_issued(audit_id: Any, results_dir: str | None = None) -> str:
    """Cross-check that audit_id was actually ISSUED by /audit (WS-8 P2-2).

    The decision-engine MCP persists each finished audit as ``<audit_id>.json`` in a
    results dir. AQG locates that dir via the ``AQG_DE_RESULTS_DIR`` env var (no
    hardcoded DE install path — keeps AQG decoupled from the DE checkout). Returns:

    - ``"issued"``       — results dir found and ``<id>.json`` present
    - ``"fabricated"``   — results dir found but ``<id>.json`` absent, OR the id
                           carries path characters (never issued by /audit)
    - ``"unverifiable"`` — no results dir configured / not a directory; the caller
                           must DEGRADE. This is OPT-IN hardening: with the env unset
                           there is no behavioural/decision change (the output merely
                           gains an additive ``audit_id_issuance`` field). It is only
                           active where an operator points ``AQG_DE_RESULTS_DIR`` at a
                           trusted DE results store — unset ⇒ the original
                           type-any-string bypass is unchanged.

    Best-effort + fail-safe: never raises, never blocks on absence.

    Honest residuals (this is an existence check, NOT unforgeable proof — audit
    22d13af0 convergent):
      - REPLAY: it confirms only that ``<id>.json`` EXISTS, not that the audit was
        run for THIS ledger. An agent can reuse any legitimately-issued id (one it
        triggered on unrelated work, or any live id within the DE 7-day TTL) to make
        an arbitrary ledger read ``issued`` — no write access required.
      - FORGE: an actor able to WRITE the dir can drop an empty ``<id>.json`` (the
        contents are not parsed) or a symlink.
    Closing these needs DE-side binding (the result record storing the reviewed-
    artifact identity so validate can match it) + signing — DE repo, not here. What
    this DOES buy: ``has_audit_id`` now means "references a real /audit result on
    this machine" instead of "is any non-placeholder string".
    """
    if not is_real_audit_id(audit_id):
        return "unverifiable"
    rd = results_dir if results_dir is not None else os.environ.get("AQG_DE_RESULTS_DIR")
    if not rd:
        return "unverifiable"
    try:
        base = Path(rd).expanduser()
        if not base.is_dir():
            return "unverifiable"
        # DE ids are lowercase hex; match case-insensitively so an upper/mixed-case
        # paste of a legit id is not mis-flagged fabricated on a case-sensitive FS.
        aid = audit_id.strip().lower()
        # Charset allowlist BEFORE any filesystem access: reject an id with path
        # separators / dots so a claimed id like `../../secret` cannot stat outside
        # the results dir. DE ids are hex; the test contract also allows `-`/`_`.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", aid):
            return "fabricated"
        return "issued" if (base / f"{aid}.json").is_file() else "fabricated"
    except (OSError, RuntimeError, ValueError):
        # RuntimeError: Path.expanduser() on `~unknownuser` / unset HOME.
        # Fail-safe: any resolution error degrades to unverifiable, never crashes validate.
        return "unverifiable"


def parse_evidence(evidence: str) -> tuple[str, int, int] | None:
    m = _EVIDENCE_RE.match(evidence)
    if not m:
        return None
    path = m.group("path").strip()
    try:
        line_start = int(m.group("start"))
    except ValueError:
        return None
    line_end_raw = m.group("end")
    line_end = int(line_end_raw) if line_end_raw else line_start
    if line_end < line_start:
        # C3 (audit 6066aaeb): normalize a reversed range by swapping, not by
        # clamping to one line (which would hide overlaps with intermediate lines).
        line_start, line_end = line_end, line_start
    return (path, line_start, line_end)


def evidence_overlaps(a: tuple[str, int, int], b: tuple[str, int, int]) -> bool:
    if a[0] != b[0]:
        return False
    return not (a[2] < b[1] or b[2] < a[1])


def is_session_llm_fallback(block: dict) -> bool:
    """True iff the ledger carries the recognized session-LLM degraded marker.

    Canonicalizes case + whitespace (`Session-LLM` / ` SESSION-LLM ` all match)
    exactly as the anti-overclaim gate does — ONE predicate, so the fallback-only
    file:line evidence gate and that gate can never diverge on what counts as a
    fallback ledger.
    """
    raw = block.get("fallback_mode")
    return isinstance(raw, str) and raw.strip().lower() == FALLBACK_MODE_SESSION_LLM


def _overlap_groups(entries: list[dict]) -> list[list[int]]:
    """Union-find over entries whose parsed line-ranges overlap. Transitive
    overlaps (A~B, B~C) join ONE group even if A and C are disjoint
    (gem-f4, audit 6066aaeb): the old greedy seen-skip dropped such links."""
    n = len(entries)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if evidence_overlaps(entries[i]["parsed_evidence"], entries[j]["parsed_evidence"]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _convergent_groups(entries: list[dict]) -> list[list[int]]:
    """Maximal sets of entry indices whose parsed spans share a COMMON point.

    Distinct from `_overlap_groups` (transitive union-find, correct for conflict
    *flagging*): convergence is a confidence claim — "these dimensions all point
    at the SAME place" — so it requires a genuine common point, not a transitive
    chain. By 1D Helly, intervals pairwise-overlap iff they share a point, so a
    chain like A`10-20` ~ B`20-30` ~ C`30-40` (no point common to all three)
    must decompose into the real pairwise convergences {A,B} and {B,C}, never one
    overstated 3-way (audit 63cf450f f1).

    Method: for each entry, take the set of entries (same path) whose span covers
    that entry's start point — every maximal common-point clique of an interval
    graph is realised at some interval's left endpoint. Dedupe and drop any set
    that is a strict subset of another (keep only maximal). Caller filters for
    >=2 distinct dimensions.
    """
    n = len(entries)
    candidate: set[frozenset[int]] = set()
    for i in range(n):
        path_i, start_i, _end_i = entries[i]["parsed_evidence"]
        covering = frozenset(
            j for j in range(n)
            if entries[j]["parsed_evidence"][0] == path_i
            and entries[j]["parsed_evidence"][1] <= start_i <= entries[j]["parsed_evidence"][2]
        )
        candidate.add(covering)
    maximal = [s for s in candidate if not any(s < other for other in candidate)]
    return [sorted(s) for s in maximal]


# ---- Skeleton --------------------------------------------------------------


def skeleton_yaml(dimensions: list[str], *, fallback: bool = False) -> str:
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dims_inline = ", ".join(dimensions)
    # WS-4 §9-5: engine-unavailable fallback. The marker travels with the ledger so
    # the validator (and any downstream output) keeps labeling it single-model /
    # non-independent — it can NEVER be presented as a cross-vendor panel (Cluster J).
    fallback_block = (
        "  fallback_mode: session-llm   # DEGRADED: current session model, single-model rough eval\n"
        "  independence: none           # single-model, same-vendor, non-independent — NOT a cross-vendor panel\n"
    ) if fallback else ""
    finding_source = (
        "your OWN single-model rough eval (NOT a cross-vendor /audit panel)"
        if fallback else "/audit focus=... panel result"
    )
    audit_id_note = (
        "  audit_id: null     # MUST stay null in fallback — a real panel id is an overclaim (validate errors)\n"
        if fallback else
        "  audit_id: null     # set if cross-checked via /audit panel\n"
    )
    body = (
        # Tier A3 ③ (PR #183): optional per-repo tuning block (Anthropic REVIEW.md
        # concept). Caller-supplied; validator does NOT gate it — left commented so
        # it stays inert until a caller opts in. Lives in the header above multi_review.
        "# repo_overrides (optional, caller-supplied — validator does NOT gate this):\n"
        "#   per-repo review tuning, mirrors Anthropic REVIEW.md. Uncomment to use.\n"
        "#   MUST stay at document root — do NOT indent under multi_review: (nested\n"
        "#   it is silently ignored, not applied). Advisory context for the panel,\n"
        "#   parsed by the caller not the validator.\n"
        "# repo_overrides:\n"
        "#   ignore: [<path-glob or dim>]   # de-prioritize for THIS repo\n"
        "#   emphasize: [<dim>]             # weight heavier for THIS repo\n"
        "#   notes: <free text for the panel>\n"
        "\n"
        "multi_review:\n"
        f"  reviewed_at: {today}\n"
        f"{fallback_block}"
        f"  diff_chars: <fill>\n"
        f"  dimensions_audited: [{dims_inline}]\n"
        "  findings:\n"
    )
    for d in dimensions:
        body += (
            f"    # {d} — fill from {finding_source}\n"
            "    # - dimension: {d}\n"
            "    #   severity: HIGH | MEDIUM | LOW | CRITICAL\n"
            "    #   pattern: <one line>\n"
            "    #   evidence: <file:line>\n"
            "    #   fix: <one line>\n"
            "    #   audit_mode_used: fast | single | two | three\n"
            "    #   verified: true | false        # optional (A3 ②) — true only after independent reproduction\n"
            "    #   audit_id: <audit ID> | null\n"
        ).replace("{d}", d)
    body += (
        "  decision: TODO     # accept | reject | needs-cross-llm-rerun\n"
        "  decision_reason: TODO\n"
        f"{audit_id_note}"
    )
    return body


# ---- Validate ledger -------------------------------------------------------

def _try_yaml_load(text: str) -> tuple[Any, str | None]:
    """Load YAML safely. Returns (parsed, error_message)."""
    try:
        import yaml  # PyYAML — the script's only dependency
    except ImportError:
        return None, (
            "PyYAML not installed; install via `pip install pyyaml>=6.0` "
            "or use cloud endpoint via `aqg-cli multi-review validate`"
        )
    # Reject anchors/aliases (Billion Laughs DoS)
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(token, (yaml.AnchorToken, yaml.AliasToken)):
                return None, (
                    "YAML anchors/aliases (& / *) not allowed (Billion Laughs DoS defense)"
                )
    except yaml.YAMLError:
        pass  # fall through to safe_load which will surface the parse error
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as exc:
        msg = str(exc).splitlines()[0][:200] if str(exc) else "yaml parse error"
        return None, f"yaml parse error: {msg}"


def _contains_key_recursive(obj: Any, key: str) -> bool:
    """True if `key` appears as a mapping key anywhere within obj (any depth).
    Used to catch repo_overrides mis-nested below multi_review at ANY level — a
    shallow `key in block` misses findings[].repo_overrides etc. Scans dict keys
    only (not string values), so a `notes:` value mentioning the word never
    false-positives (audit f3466101 #2 convergent)."""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_contains_key_recursive(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_key_recursive(x, key) for x in obj)
    return False


def validate_ledger(text: str) -> dict:
    """Validate filled multi-review YAML ledger.

    Returns a dict with: valid, violations, parsed_decision, parsed_dimensions,
    findings_count_per_dim, severity_counts, has_audit_id, cross_dim_conflicts,
    needs_llm_judgement, needs_llm_judgement_reasons, next_safe_step.

    Mirrors aqg_cloud.skills.multi_review.decide() validate path.
    """
    violations: list[dict] = []

    parsed, err = _try_yaml_load(text)
    if err is not None:
        return _invalid({"severity": "error", "message": err})

    if not isinstance(parsed, dict) or "multi_review" not in parsed:
        return _invalid({
            "severity": "error",
            "message": "ledger missing top-level `multi_review:` key",
            "field": "multi_review",
        })
    block = parsed["multi_review"]
    if not isinstance(block, dict):
        return _invalid({
            "severity": "error",
            "message": "`multi_review` value must be a YAML mapping",
            "field": "multi_review",
        })

    # Tier A3 ③ (PR #183, audit 1f6d550d #2 convergent): repo_overrides belongs
    # at document root (caller-supplied, validator-ignored there). If it appears
    # INSIDE multi_review the caller almost certainly mis-indented it — warn so the
    # per-repo tuning isn't silently dropped (warning, not a gate — spec keeps it
    # un-gated).
    if _contains_key_recursive(block, "repo_overrides"):
        violations.append({
            "severity": "warning",
            "message": (
                "repo_overrides found nested inside multi_review (any depth); move "
                "it to document root — nested it is silently ignored, not applied"
            ),
            "field": "repo_overrides",
        })

    # dimensions_audited
    raw_dims = block.get("dimensions_audited") or []
    if not isinstance(raw_dims, list):
        violations.append({
            "severity": "error",
            "message": "dimensions_audited must be a list",
            "field": "dimensions_audited",
        })
        raw_dims = []
    parsed_dims: list[str] = []
    unknown_dims: list[str] = []
    non_string_dim = False
    for d in raw_dims:
        if isinstance(d, str) and d in DIMENSIONS:
            parsed_dims.append(d)
        elif isinstance(d, str):
            unknown_dims.append(d)
        else:
            non_string_dim = True  # gem-f5: a non-string entry must not be ignored
    if unknown_dims:
        violations.append({
            "severity": "error",
            "message": f"unknown dimensions {unknown_dims}; valid: {DIMENSIONS}",
            "field": "dimensions_audited",
        })
    if non_string_dim:
        violations.append({
            "severity": "error",
            "message": "dimensions_audited contains a non-string entry",
            "field": "dimensions_audited",
        })
    if not parsed_dims:
        violations.append({
            "severity": "error",
            "message": "dimensions_audited list is empty",
            "field": "dimensions_audited",
        })

    # findings (audit fix gpt-5.5 #8: explicit isinstance check)
    raw_findings = block.get("findings", [])
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        violations.append({
            "severity": "error",
            "message": f"findings must be a list (got {type(raw_findings).__name__})",
            "field": "findings",
        })
        raw_findings = []

    findings_count_per_dim: dict[str, int] = {d: 0 for d in DIMENSIONS}
    severity_counts: dict[str, int] = {s: 0 for s in VALID_SEVERITIES}
    findings_verified_count = 0  # A3 ②: tallied before the evidence gate (parity w/ cloud)
    parsed_findings: list[dict] = []
    # PR3 (Local Degraded Audit v5 §17.7): recognized here, before the loop, so the
    # evidence gate can fail closed on a fallback finding that lacks a locatable
    # file:line. Fallback-mode ONLY — the cross-vendor path stays evidence-non-empty.
    is_fallback = is_session_llm_fallback(block)
    for idx, entry in enumerate(raw_findings):
        if not isinstance(entry, dict):
            violations.append({
                "severity": "error",
                "message": f"findings[{idx}] must be a mapping",
                "field": "findings",
            })
            continue
        dim = entry.get("dimension")
        if not isinstance(dim, str) or dim not in DIMENSIONS:
            violations.append({
                "severity": "error",
                "message": f"findings[{idx}].dimension {dim!r} unknown",
                "field": "findings",
            })
            continue
        # Audit fix C2 (convergent): finding dim must be in parsed_dims
        if parsed_dims and dim not in parsed_dims:
            violations.append({
                "severity": "error",
                "message": (
                    f"findings[{idx}].dimension {dim!r} not in declared "
                    f"dimensions_audited {parsed_dims}"
                ),
                "field": "findings",
            })
            continue
        sev = entry.get("severity")
        if not isinstance(sev, str):
            violations.append({
                "severity": "error",
                "message": f"findings[{idx}].severity must be string",
                "field": "findings",
            })
            continue
        sev_norm = sev.strip().upper()
        if sev_norm not in VALID_SEVERITIES:
            violations.append({
                "severity": "error",
                "message": f"findings[{idx}].severity {sev!r} unknown",
                "field": "findings",
            })
            continue
        # C2 (audit 6066aaeb): evidence is REQUIRED + must be a non-empty string.
        # A finding without parseable evidence escapes cross-dim conflict
        # detection, so fail-closed instead of silently counting it.
        evidence = entry.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            violations.append({
                "severity": "error",
                "message": f"findings[{idx}].evidence missing or empty (file:line required)",
                "field": "findings",
            })
            continue
        # PR3 (v5 §17.7): a session-LLM fallback finding MUST cite a locatable
        # file:line — there is no independent cross-vendor panel to corroborate a
        # prose-only note, so fail closed. Uses the STRICT locator (not the
        # permissive parse_evidence, which prose + a trailing ":N" would bypass —
        # audit 47f85142 f1). The cross-vendor path keeps the softer non-empty
        # rule above (parse_evidence there feeds overlap, not a gate).
        if is_fallback and not evidence_has_locator(evidence):
            violations.append({
                "severity": "error",
                "message": (
                    f"findings[{idx}].evidence {evidence.strip()!r} has no locatable "
                    "file:line token; a session-LLM fallback finding MUST cite a real "
                    "file:line (no cross-vendor panel corroborates it)"
                ),
                "field": "findings",
            })
            continue
        findings_count_per_dim[dim] += 1
        severity_counts[sev_norm] += 1
        # Tier A3 ② (PR #183, audit f3466101 f1 convergent): `verified` is a
        # property of an ACCEPTED finding — evaluate the warning + tally only
        # AFTER all gates, so findings_verified_count can never exceed the
        # accepted-finding count. (R1 moved this before the evidence gate, letting
        # a rejected finding be tallied → verified>total, an impossible state. The
        # evidence-drift-driven count difference vs cloud for a missing-evidence
        # finding is the CORRECT propagation of that pre-existing drift, not an A3
        # parity bug.)
        verified_raw = entry.get("verified")
        if verified_raw is not None and not isinstance(verified_raw, bool):
            violations.append({
                "severity": "warning",
                "message": (
                    f"findings[{idx}].verified must be true/false "
                    f"(got {type(verified_raw).__name__}); treated as unverified"
                ),
                "field": "findings",
            })
        if verified_raw is True:
            findings_verified_count += 1
        parsed_findings.append({
            "parsed_evidence": parse_evidence(evidence),
            "raw_evidence": evidence.strip(),
            "dimension": dim,
            "severity": sev_norm,
            "idx": idx,
        })

    # decision
    raw_decision = block.get("decision")
    parsed_decision = None
    if not isinstance(raw_decision, str) or not raw_decision.strip():
        violations.append({
            "severity": "error",
            "message": "decision must be non-empty string",
            "field": "decision",
        })
    else:
        canonical = DECISION_ALIASES.get(raw_decision.strip().lower())
        if canonical is None:
            violations.append({
                "severity": "error",
                "message": f"unrecognized decision {raw_decision!r}",
                "field": "decision",
            })
        else:
            parsed_decision = canonical

    raw_reason = block.get("decision_reason")
    if (not isinstance(raw_reason, str) or not raw_reason.strip()
            or raw_reason.strip().upper() == "TODO"):
        violations.append({
            "severity": "error",
            "message": "decision_reason must be non-empty string (no TODO placeholder)",
            "field": "decision_reason",
        })

    raw_audit_id = block.get("audit_id")
    has_audit_id = is_real_audit_id(raw_audit_id)
    # WS-8 P2-2: cross-check that a well-formed audit_id was actually ISSUED by
    # /audit (via the DE results dir). A fabricated-but-well-formed id would
    # otherwise set has_audit_id=True and SUPPRESS the needs_llm_judgement signal
    # below (incl. the CRITICAL / needs-cross-llm-rerun triggers). If the DE results
    # dir is configured (AQG_DE_RESULTS_DIR) and the id is not there, downgrade it so
    # a forged id can no longer suppress NLJ. Unset dir -> "unverifiable" -> unchanged.
    audit_id_issuance = "unverifiable"
    if has_audit_id:
        audit_id_issuance = verify_audit_id_issued(raw_audit_id)
        if audit_id_issuance == "fabricated":
            # WARNING, not error: do not hard-reject the whole ledger (a legit id can
            # be absent if the audit ran on another machine, or aged out of the 7-day
            # results TTL). Instead downgrade has_audit_id so the unverified id cannot
            # SUPPRESS needs_llm_judgement -- the review still gets re-scrutinized.
            has_audit_id = False
            violations.append({
                "severity": "warning",
                "message": (
                    f"audit_id {raw_audit_id!r} was not found in the DE results dir "
                    "(AQG_DE_RESULTS_DIR) -- /audit never issued it on this machine "
                    "(fabricated, copied from another machine, or aged out of the "
                    "results TTL). Treated as no audit_id so it cannot suppress the "
                    "needs_llm_judgement signal; re-run /audit here, or clear it."
                ),
                "field": "audit_id",
            })

    # WS-4 §9-5 fallback marker + anti-overclaim gate (R1-Cluster J). A fallback
    # ledger is a single-model rough eval; it must NEVER be dressed up as an
    # independent cross-vendor panel. FAIL CLOSED (audit 14c08bb3, 4/4 convergent):
    # the overclaim error fires for ANY present marker + a real audit_id, not only
    # the exact string — otherwise a mis-cased / typo'd / non-string marker slips
    # past into a panel-looking ledger.
    raw_fallback = block.get("fallback_mode")
    fallback_mode: str | None = None
    if raw_fallback is not None:
        # Canonicalize case + whitespace via the shared predicate so `Session-LLM`
        # / ` SESSION-LLM ` are recognized as the degraded marker rather than
        # silently discarded. Reuse the SAME early `is_fallback` binding the
        # evidence gate uses — do NOT re-normalize fallback_mode inline here, or
        # the two gates could diverge again (grok f3, audit 47f85142).
        if is_fallback:
            fallback_mode = FALLBACK_MODE_SESSION_LLM
            # A single-model fallback claiming a cross-vendor panel is contradictory.
            raw_indep = block.get("independence")
            if isinstance(raw_indep, str) and raw_indep.strip().lower() not in ("", "none"):
                violations.append({
                    "severity": "error",
                    "message": (
                        f"fallback_mode={FALLBACK_MODE_SESSION_LLM} is {FALLBACK_LABEL}, but "
                        f"independence={raw_indep.strip()!r} claims otherwise; a single-model "
                        "fallback must be independence: none"
                    ),
                    "field": "independence",
                })
        else:
            violations.append({
                "severity": "warning",
                "message": (
                    f"unrecognized fallback_mode {raw_fallback!r}; the only known "
                    f"marker is {FALLBACK_MODE_SESSION_LLM!r} — a typo here would not "
                    "get the degraded-mode label"
                ),
                "field": "fallback_mode",
            })
        # Fail closed: ANY present fallback marker (recognized OR not) cannot
        # co-exist with a real cross-panel audit_id — that packages a single-model
        # rough eval as an independent panel, the exact overclaim the constraint forbids.
        if has_audit_id:
            violations.append({
                "severity": "error",
                "message": (
                    f"fallback_mode is present ({raw_fallback!r}) — {FALLBACK_LABEL}; "
                    "it cannot also carry a real cross-panel audit_id "
                    "(that packages a single-model rough eval as an independent panel). "
                    "Set audit_id: null, or drop fallback_mode if a real panel ran."
                ),
                "field": "fallback_mode",
            })

    valid = not any(v["severity"] == "error" for v in violations)

    # Cross-dim co-location: ≥2 DISTINCT dimensions flag the SAME file:line, read
    # two ways (dedup-confirm, 2026-05-31):
    #   - cross_dim_conflicts (strings, UNCHANGED): transitive `_overlap_groups`
    #     union-find — right for *flagging* a region for review (feeds NLJ); a
    #     co-located group with DISAGREEING severities is the genuine conflict.
    #   - convergent_findings (structured): a confidence BOOST — "N dims
    #     corroborate THIS location". This requires a genuine COMMON point
    #     (`_convergent_groups`), NOT a transitive chain, so a chain decomposes
    #     into honest pairwise convergences instead of one overstated N-way
    #     (audit 63cf450f f1). Emit-only: the caller (Claude/EAF) prioritises
    #     convergent findings as likely-real; the skill does not act.
    parsed_entries = [e for e in parsed_findings if e["parsed_evidence"] is not None]
    raw_entries = [e for e in parsed_findings if e["parsed_evidence"] is None]

    cross_dim_conflicts: list[str] = []
    for members in _overlap_groups(parsed_entries):
        group_dims = {parsed_entries[m]["dimension"] for m in members}
        if len(group_dims) >= 2:
            group_sevs = {parsed_entries[m]["severity"] for m in members}
            group_evidences = {parsed_entries[m]["raw_evidence"] for m in members}
            cross_dim_conflicts.append(
                f"evidences={sorted(group_evidences)} flagged by "
                f"{sorted(group_dims)} with severities {sorted(group_sevs)}"
            )
    raw_by_evidence: dict[str, list[dict]] = {}
    for e in raw_entries:
        raw_by_evidence.setdefault(e["raw_evidence"], []).append(e)
    for evidence, entries in raw_by_evidence.items():
        if len(entries) >= 2:
            unique_dims = {x["dimension"] for x in entries}
            if len(unique_dims) >= 2:
                cross_dim_conflicts.append(
                    f"evidence={evidence!r} flagged by {sorted(unique_dims)} "
                    f"with severities {sorted({x['severity'] for x in entries})}"
                )

    convergent_findings: list[dict] = []

    def _record_convergence(member_entries: list[dict]) -> None:
        dims = {e["dimension"] for e in member_entries}
        if len(dims) < 2:
            return
        sevs = {e["severity"] for e in member_entries}
        convergent_findings.append({
            "evidences": sorted({e["raw_evidence"] for e in member_entries}),
            "dimensions": sorted(dims),
            "dim_count": len(dims),
            "severities": sorted(sevs),
            "severity_agreement": len(sevs) == 1,
            "confidence": "high" if len(dims) >= 3 else "elevated",
        })

    # parsed evidence → common-point maximal groups (honest co-location)
    for members in _convergent_groups(parsed_entries):
        _record_convergence([parsed_entries[m] for m in members])
    # raw (unparseable) evidence → exact-string match IS a common location
    for entries in raw_by_evidence.values():
        _record_convergence(entries)

    # NLJ triggers
    nlj = False
    nlj_reasons: list[str] = []
    if valid:
        if cross_dim_conflicts:
            nlj = True
            nlj_reasons.append(
                f"cross-dim conflicts on {len(cross_dim_conflicts)} evidence point(s)"
            )

        non_low_severity_count = (
            severity_counts.get("CRITICAL", 0)
            + severity_counts.get("HIGH", 0)
            + severity_counts.get("MEDIUM", 0)
        )
        if (parsed_decision == "accept"
                and sum(1 for n in findings_count_per_dim.values() if n > 0) >= 3
                and (non_low_severity_count >= 1 or not has_audit_id)):
            nlj = True
            nlj_reasons.append(
                "3+ dims have findings but decision=accept; high cognitive load"
            )

        if parsed_decision == "needs-cross-llm-rerun" and not has_audit_id:
            nlj = True
            nlj_reasons.append(
                "decision=needs-cross-llm-rerun but audit_id null"
            )

        if severity_counts.get("CRITICAL", 0) > 0 and not has_audit_id:
            nlj = True
            nlj_reasons.append(
                "CRITICAL severity finding but audit_id null"
            )

        # Audit fix C3 (convergent): skeleton-paste trap
        dims_with_findings_count = sum(1 for n in findings_count_per_dim.values() if n > 0)
        if (len(parsed_dims) > 0 and dims_with_findings_count == 0
                and parsed_decision == "accept"):
            nlj = True
            nlj_reasons.append(
                f"declared {len(parsed_dims)} dims but ZERO findings + decision=accept "
                "(skeleton-paste trap)"
            )

    if valid and not nlj:
        next_safe_step = (
            "ledger structure valid; paste multi_review block into "
            "`.aqg/current_ledger.md` Evidence section"
        )
    elif valid and nlj:
        next_safe_step = (
            "ledger valid but needs_llm_judgement signal fired; review reasons "
            "+ run cross-LLM rerun via /audit"
        )
    else:
        err_count = sum(1 for v in violations if v["severity"] == "error")
        next_safe_step = f"fix {err_count} validation error(s) before adjudication"

    return {
        "valid": valid,
        "violations": violations,
        "parsed_decision": parsed_decision,
        "parsed_dimensions": parsed_dims,
        "findings_count_per_dim": findings_count_per_dim,
        "findings_verified_count": findings_verified_count,
        "severity_counts": severity_counts,
        "has_audit_id": has_audit_id,
        "audit_id_issuance": audit_id_issuance,
        "fallback_mode": fallback_mode,
        "cross_dim_conflicts": cross_dim_conflicts,
        "convergent_findings": convergent_findings,
        "needs_llm_judgement": nlj,
        "needs_llm_judgement_reasons": nlj_reasons,
        "next_safe_step": next_safe_step,
    }


def _invalid(violation: dict) -> dict:
    """Quick exit for fatal parse errors."""
    return {
        "valid": False,
        "violations": [violation],
        "parsed_decision": None,
        "parsed_dimensions": [],
        "findings_count_per_dim": {d: 0 for d in DIMENSIONS},
        "findings_verified_count": 0,
        "severity_counts": {s: 0 for s in VALID_SEVERITIES},
        "has_audit_id": False,
        "audit_id_issuance": "unverifiable",
        "fallback_mode": None,
        "cross_dim_conflicts": [],
        "convergent_findings": [],
        "needs_llm_judgement": False,
        "needs_llm_judgement_reasons": [],
        "next_safe_step": "fix 1 validation error before adjudication",
    }


# ---- CLI -------------------------------------------------------------------


def cmd_new(args: argparse.Namespace) -> int:
    requested = args.dimensions or list(DIMENSIONS)
    dims_used = [d for d in requested if d in DIMENSIONS]
    # Dedupe preserving order (audit fix C4)
    seen: set[str] = set()
    deduped: list[str] = []
    for d in dims_used:
        if d not in seen:
            seen.add(d)
            deduped.append(d)
    dims_used = deduped
    if not dims_used:
        dims_used = list(DIMENSIONS)
    fallback = bool(getattr(args, "fallback_session_llm", False))
    skeleton_filename = (
        f"{dt.datetime.now(dt.timezone.utc).date().isoformat()}-multi-review.md"
    )
    skeleton = skeleton_yaml(dims_used, fallback=fallback)

    if args.json:
        print(json.dumps({
            "skeleton_filename": skeleton_filename,
            "dimensions_used": dims_used,
            "fallback_mode": FALLBACK_MODE_SESSION_LLM if fallback else None,
            "independence": "none" if fallback else "cross-vendor-panel",
            "focus_prompts": [
                {"dimension": d, "focus_prompt": FOCUS_PROMPTS[d]} for d in dims_used
            ],
            "skeleton_markdown": skeleton,
        }, indent=2, ensure_ascii=False))
        return 0

    if fallback:
        # Cluster J: the degraded mode MUST be labeled up front, before any content,
        # so it can never be read as a cross-vendor independent review.
        print("# ⚠ DEGRADED FALLBACK — engine unavailable (mode=new --fallback-session-llm)")
        print()
        print(f"> **This is NOT a cross-vendor review.** {FALLBACK_LABEL}.")
        print("> The current session model does a single-model rough self-eval; there is")
        print("> no independent second voice. Escalate to a real `/audit` cross-vendor")
        print("> panel before trusting any accept decision. Do NOT present this output as")
        print("> a cross-vendor / independent / multi-LLM review.")
        print()
    else:
        print(f"# AQG Multi-Dimension Review — skeleton + focus prompts (mode=new)")
        print()
    print(f"- generated_at: {dt.datetime.now(dt.timezone.utc).isoformat()}")
    print(f"- dimensions: {', '.join(dims_used)}")
    print("- decided_by: aqg-multi-review local script (development source)")
    if fallback:
        print(f"- fallback_mode: {FALLBACK_MODE_SESSION_LLM} ({FALLBACK_LABEL})")
    print()
    print(f"**Suggested filename**: `{skeleton_filename}`")
    print()
    if fallback:
        print("## Per-dimension self-eval prompts (the CURRENT session model answers each — single-model)")
    else:
        print("## Per-dimension focus prompts (paste as `focus=<prompt>` to /audit)")
    print()
    for d in dims_used:
        print(f"### {d}")
        print()
        print(f"> {FOCUS_PROMPTS[d]}")
        print()
    print("## Closeout-importable YAML skeleton")
    print()
    print("```yaml")
    print(skeleton.rstrip())
    print("```")
    print()
    print("## Next safe step")
    if fallback:
        print(
            f"- Engine unavailable: the current session model self-evaluates each of "
            f"{len(dims_used)} dimension(s) and fills the YAML ledger directly (single-model, "
            "rough). Keep `audit_id: null` and `fallback_mode: session-llm` — do NOT relabel "
            "as a cross-vendor panel. Then run `aqg_multi_review.py validate --file <path>`; "
            "prefer decision `needs-cross-llm-rerun` so a real panel still runs later."
        )
    else:
        print(
            f"- For each of {len(dims_used)} dimension(s), run "
            "`/audit artifact=<diff> focus=<dim focus_prompt> mode=<phase-transition recommended>`. "
            "Then fill the YAML ledger and run `aqg_multi_review.py validate --file <path>`."
        )
    print()
    print(
        "> Per ADR §5: this script emits SIGNAL only. Caller (human / Claude / EAF) "
        "is responsible for invoking /audit per dimension."
    )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    if not args.file:
        print("ERROR: --file required", file=sys.stderr)
        return 2
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8", errors="replace")
    result = validate_ledger(text)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["valid"] else 1

    if result["valid"]:
        suffix = " (NEEDS CROSS-LLM RERUN)" if result["needs_llm_judgement"] else ""
        print(f"# AQG Multi-Review verdict: VALID{suffix}")
    else:
        err_count = sum(1 for v in result["violations"] if v["severity"] == "error")
        print(f"# AQG Multi-Review verdict: INVALID — {err_count} error(s)")
    print()

    # Cluster J: whenever the ledger is a fallback eval, the degraded label rides
    # along with the verdict — the output can never drop it and read as a panel.
    if result.get("fallback_mode") == FALLBACK_MODE_SESSION_LLM:
        print(f"> ⚠ **DEGRADED FALLBACK** ({FALLBACK_MODE_SESSION_LLM}): {FALLBACK_LABEL}. "
              "NOT a cross-vendor independent review.")
        print()

    if result["violations"]:
        print("## Violations")
        for v in result["violations"]:
            field = f" [{v.get('field', '')}]" if v.get("field") else ""
            sev = "ERROR" if v["severity"] == "error" else "WARN"
            print(f"- **{sev}**{field}: {v['message']}")
        print()

    if result["valid"]:
        print("## Parsed summary")
        print(f"- decision: `{result['parsed_decision']}`")
        print(f"- dimensions: [{', '.join(result['parsed_dimensions'])}]")
        for d in DIMENSIONS:
            n = result["findings_count_per_dim"].get(d, 0)
            if d in result["parsed_dimensions"] or n > 0:
                print(f"  - {d}: {n}")
        sev = result["severity_counts"]
        print(
            f"- severity_counts: CRITICAL={sev.get('CRITICAL', 0)} "
            f"HIGH={sev.get('HIGH', 0)} MEDIUM={sev.get('MEDIUM', 0)} "
            f"LOW={sev.get('LOW', 0)}"
        )
        print(f"- has_audit_id: {result['has_audit_id']}")
        print(f"- findings_verified: {result['findings_verified_count']}")
        print(f"- cross_dim_conflicts: {len(result['cross_dim_conflicts'])}")
        for c in result["cross_dim_conflicts"]:
            print(f"  - {c}")
        print()

    if result["needs_llm_judgement"]:
        print("## needs_llm_judgement: TRUE")
        print()
        for r in result["needs_llm_judgement_reasons"]:
            print(f"- {r}")
        print()

    print("## Next safe step")
    print(f"- {result['next_safe_step']}")
    return 0 if result["valid"] else 1


def main() -> int:
    p = argparse.ArgumentParser(
        prog="aqg_multi_review.py",
        description=(
            "AQG multi-dimension review (5-dim cross-LLM router) — the "
            "development source. Cloud release form mirrors this for parity. "
            "Emits SIGNAL only; caller invokes /audit per dim."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    new_p = sub.add_parser("new", help="emit skeleton + 5 dim focus prompts")
    new_p.add_argument("--dimensions", nargs="*", default=None,
                       help=f"subset of {DIMENSIONS}; default = all 5")
    new_p.add_argument("--fallback-session-llm", action="store_true",
                       help="engine-unavailable degraded mode: single-model rough self-eval "
                            "by the current session model (labeled non-independent; NOT a "
                            "cross-vendor panel)")
    new_p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    new_p.set_defaults(func=cmd_new)

    val_p = sub.add_parser("validate", help="validate filled YAML ledger")
    val_p.add_argument("--file", required=True, help="path to filled YAML ledger")
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
