#!/usr/bin/env python3
"""AQG Code Construction Checker — validates ledger + anti-pattern + objection structure.

Per sketch a1 audit (audit_id c87fb770) 14 fixes integrated in v1, plus
PR-B three-audit (audit_id 83d00576) 15 fixes integrated in v2.

v1 fixes (PR-A):
- C1: --ledger arg + canonical .aqg/code-construction/<task>.md path
- C2: pathlib.exists() validation + >=1 objection ref changed file in diff
- D1: ledger header created_at <= first edit mtime
- D2: ## Warning Acknowledgements section
- D3: deterministic ledger field presence checks (not semantic)
- D5: --mode={staged,worktree,base}
- D8: Layer 1 secret pattern detection (hard reject)
- D9: ledger header path: mini|full|plan declared at session start + size match
- D10: tiered objection count (mini=1, full=3, plan=5)
- D11: AST not regex for `except Exception`
- gemini #1 (option c): AQG_AGENT env var gating

v2 fixes (PR-B audit):
- v2-C1 (CRITICAL gpt #1 + gemini #1): 6-step ledger table parser + 3 hard blockers
  (Behavior Lock for prod, Local Verification for schema, skipped_checks reason)
- v2-C2 (gpt #2 + gemini #3 + o3): diff-aware broad-except + tuple form + per-handler boundary marker
- v2-C4 (gpt #7 + gemini #6): path bound check (reject ../../../etc/passwd)
- v2-D1 (gpt #3 + o3): diff-hunk warnings (TODO/dep based on added lines only) + W7 + W8 + ack-condition matching
- v2-D4 (gpt #8): performance budget warn-only on overrun
- v2-D5 (gemini #4): _get_diff_files fail-closed on git error
- v2-D6 (gemini #5): YAML parser strict on multi-line + JSON parsing for skipped_checks
- v2-D7 (gemini #7): markdown table parsing via line.split('|') not regex

v0.14.0 (Behavior Contract — OpenSpec requirement/scenario absorption):
- parse_behavior_contract() + _check_behavior_contract(): structure the free-text
  Behavior Lock into RFC-2119 requirement + GIVEN/WHEN/THEN scenarios. WARN-ONLY
  (BC0-BC4 advisories on stderr, own channel — NOT fed to warning-ack or hard-block,
  so exit code is unchanged). Two Deep audit rounds: 7f24f960 → 0041b460.

Exit codes:
  0: success or silent pass when AQG_AGENT is not set
  1: generic check failure (e.g., subprocess error; D5 fail-closed)
  2: secret pattern detected in ledger (D8 critical) — also propagated by argparse on bad CLI args; distinguish via stderr message
  3: ledger missing or malformed (D9 / C1)
  4: size mismatch (declared path doesn't match actual diff; D9)
  5: anti-pattern hard block (D3 / D11 / v2-C1)
  6: objection malformed (C2 / D10)
  7: RESERVED — Behavior Contract hard block (v0.14.0 is warn-only; a later minor promotes BC0-BC4 to exit 7)
  70: reserved usage error (currently unused; argparse exits 2 instead)
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ===== Exit codes =====
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_SECRET_DETECTED = 2
EXIT_LEDGER_MALFORMED = 3
EXIT_SIZE_MISMATCH = 4
EXIT_HARD_BLOCK = 5
EXIT_OBJECTION_MALFORMED = 6
EXIT_USAGE = 70

# ===== Subprocess timeouts =====
TIMEOUT_GIT_S = 5
TIMEOUT_GIT_SHOW_S = 10  # git show may need more time on large files

# ===== Path size thresholds (per sketch path tier) =====
SIZE_MINI_MAX = 30
SIZE_FULL_MAX = 300

# ===== Tiered objection minimums (audit D10) =====
TIER_OBJECTION_MIN = {"mini": 1, "full": 3, "plan": 5}

# ===== Performance budget (audit v2-D4: warn-only on overrun) =====
DEADLINE_S = {"mini": 3, "full": 15, "plan": 60}

# ===== Production / schema path classifiers (v2-C1) =====
PROD_PATH_PATTERNS = [
    re.compile(r"^scripts/[^/]+\.py$"),
    re.compile(r"^src/"),
    re.compile(r"^skills/.*/scripts/.*\.py$"),
    re.compile(r"^agent-packs/.*/hooks/.*\.sh$"),
]
SCHEMA_PATH_PATTERNS = [
    re.compile(r"\.proto$"),
    re.compile(r"^templates/.*\.yaml$"),
    re.compile(r"^schemas/"),
    re.compile(r"\.openapi\.json$"),
    re.compile(r"^quality-gates\.json$"),
]
REGRESSION_KEYWORDS = [
    "regression", "regress", "test_regress",
    "pytest", "unittest", "mypy", "ruff",
    "PASS", "OK", "passed",
]
DOC_PATH_PATTERNS = [
    re.compile(r"^docs/.+\.md$"),
    re.compile(r"^README\.md$"),
    re.compile(r"\.rst$"),
]

# ===== Code-debt TODO/FIXME marker form (shared by VAGUE + W5) =====
# Matches a TODO/FIXME written as a *code-debt marker*: either after a comment
# leader (`# TODO`, `// FIXME`, `/* TODO`, `<!-- TODO`) or in the colon form
# (`TODO:` / `FIXME:`). It does NOT match a leaderless, colon-less mention —
# e.g. "fill the TODO row", "renders as INCONSISTENT — TODO", or a
# `| ... | TODO |` ledger status cell — which are legitimate in skills like
# aqg-evidence-closeout and used to false-positive here (C5 dogfood fix).
# Audit 094568a8 refinements:
#   - The colon branch DOES still match a prose `TODO:` (a colon is present);
#     that is a deliberate, far narrower residual than the old bare-`todo` scan,
#     not an absolute "never prose" guarantee.
#   - Leader whitespace is `[^\S\n]*` (horizontal only) so a leader at the end
#     of one diff-added line cannot bridge the `"\n".join` to a TODO on the next
#     line (the W5 scan searches the joined added-line blob).
#   - The leader set is intentionally limited to `# // /* <!--`. Bare (colon-less)
#     `* TODO` (block-comment continuation), `@TODO`, `-- TODO` are NOT flagged:
#     adding `*` would reintroduce the markdown-bullet FP this fix removes, and
#     the others have zero in-repo use. Their `:` colon forms are still caught.
# See tests/test_aqg_construction_check.py::TestTodoFixmeDebtForm.
TODO_FIXME_DEBT_PATTERN = r"(?:#|//|/\*|<!--)[^\S\n]*(?:TODO|FIXME)\b|\b(?:TODO|FIXME):"

# ===== Vague-words blacklist for objections + acks =====
VAGUE_WORDS_PATTERNS = [
    r"加测试",
    r"做\s*review",
    r"注意边界",
    r"完善",
    r"优化一下",
    r"add\s+tests?",
    r"do\s+(a\s+)?review",
    r"watch\s+(the\s+)?boundary",
    r"\bbe\s+careful",
    r"improve\s+later",
    r"\bfix\s+later\b",
    r"\btbd\b",
    # Tightened from bare `todo`/`fixme`: only a code-debt marker form counts as
    # a vague placeholder mitigation; a prose mention of "TODO" does not.
    TODO_FIXME_DEBT_PATTERN,
]
VAGUE_REGEX = re.compile("|".join(VAGUE_WORDS_PATTERNS), re.IGNORECASE)
# W5 scans diff-added lines for new code debt. Keep it case-sensitive (debt
# markers are conventionally upper-case) so a lower-cased prose "todo" — e.g. a
# markdown "# todo list" heading — is not newly flagged; this preserves the
# casing of the prior `\bTODO\b|\bFIXME\b` scan while excluding prose/table
# mentions.
TODO_FIXME_DEBT_REGEX = re.compile(TODO_FIXME_DEBT_PATTERN)

# ===== Objection file:line reference matching (G4) =====
# Candidate token before a ':<line>' suffix. Accepted as a real file:line ref
# only when it is dotted (has an extension), path-like (contains '/'), or a
# known extensionless filename — see _is_file_line_path. The pathlib.exists()
# backstop in _check_objections still applies. This conservative gate avoids
# treating prose like "step 2:10" as a reference.
FILE_LINE_CANDIDATE_REGEX = re.compile(
    r"([\w/.\-]+):(\d+(?:-\d+)?)"
)
KNOWN_NO_EXT_FILENAMES = {"Makefile", "Dockerfile", "LICENSE"}

# ===== Secret patterns (audit D8 Layer 1 hard reject + PR-D audit gemini #1 + gpt #2 fixes) =====
# CRITICAL: patterns must capture the FULL secret (not just header) so that
# `re.sub(pattern, "[REDACTED]", text)` actually replaces the entire token.
# Used by:
# - construction_check (`_check_secrets`): re.search → block on hit (header alone OK)
# - closeout (`redact_secrets`): re.sub → must capture full token to avoid leaking body
SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    # P4: AWS temporary (STS) access key — distinctive ASIA prefix, full token.
    (re.compile(r"ASIA[0-9A-Z]{16}"), "AWS temporary access key"),
    # P4: extend GitHub token prefixes to include u (user-to-server) + r (refresh).
    (re.compile(r"gh[posur]_[A-Za-z0-9_]{36,}"), "GitHub token"),
    (re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{24,}"), "Stripe API key"),
    # P4: OpenAI project key (sk-proj-...) — must precede the generic sk- pattern
    # so the full token (incl. the proj- segment) is captured for re.sub redaction.
    (re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}"), "OpenAI project key"),
    # PR-D gemini #1 critical fix: PEM regex captures BEGIN..END whole block (was header only)
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "PEM private key",
    ),
    # PR-D gpt #2 + gemini #3 fix: extend coverage to common dev token classes
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI API key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=+/-]{10,}"
        ),
        "JWT",
    ),
    # GitHub fine-grained PAT (github_pat_...)
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "GitHub fine-grained PAT"),
]

# ===== AGENT enforcement gating (audit gemini #1 option c) =====
ENFORCING_AGENTS = {"codex", "claude", "human-opt-in", "ci-rerun"}

# ===== Boundary marker for broad except exemption =====
BOUNDARY_MARKER = "# aqg: top-level boundary"
BOUNDARY_MARKER_LOOKBACK = 5  # lines to scan above each handler


@dataclass
class LedgerHeader:
    task_slug: str
    path: str  # mini | full | plan
    created_at: str  # ISO 8601
    session_agent: str
    skipped_checks: list[dict]  # v2-D6: parsed as JSON list
    objections_diff_coverage_exception: Optional[str]
    # v0.14.0: whole-section opt-out of the Behavior Contract advisories (BC0-BC4).
    # Kept RAW here (strip only); activeness (null/none/~/vague normalization) is
    # decided by _bc_exception_active so the check stays the single source of truth.
    behavior_contract_exception: Optional[str] = None


@dataclass
class StepRow:
    step: str
    required: str
    evidence: str
    file_line: str
    command_result: str


# ===== v0.14.0 Behavior Contract (OpenSpec requirement/scenario absorption) =====
@dataclass(frozen=True)
class BCScenario:
    id: str
    markers: frozenset  # subset of {"GIVEN", "WHEN", "THEN"} actually present
    lines: tuple  # raw scenario-block lines, for durable closeout render


@dataclass(frozen=True)
class BCRequirement:
    id: str
    title: str
    statement: str  # the statement block (for durable render)
    has_normative: bool  # a MUST/SHALL keyword present in the statement block
    scenarios: tuple  # tuple[BCScenario, ...]


@dataclass(frozen=True)
class BehaviorContract:
    present: bool  # a "## Behavior Contract" section exists
    requirements: tuple  # tuple[BCRequirement, ...]
    parse_error: Optional[str]  # set iff parsing raised — rendered as a BC0 advisory


def _safe_run(cmd: list[str], timeout: int = TIMEOUT_GIT_S) -> tuple[int, str]:
    """Run subprocess with timeout + non-interactive env, return (rc, stdout)."""
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",  # git emits UTF-8; on Windows (cp936) default decode
            errors="surrogateescape",  # lossless: gate never crashes AND never
            # silently substitutes bytes it scans (secret/anti-pattern checks).
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return proc.returncode, proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "command not found"


def _git_show_raw(cmd: list[str], timeout: int = TIMEOUT_GIT_SHOW_S) -> tuple[int, str]:
    """Like _safe_run but WITHOUT stripping stdout (line numbers must align).

    Separates stderr so a non-zero rc doesn't pollute file content. Used by
    _read_post_image where exact byte/line layout matters (C1/P1).
    """
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",  # git emits UTF-8; on Windows (cp936) default decode
            errors="surrogateescape",  # lossless: gate never crashes AND never
            # silently substitutes bytes it scans (secret/anti-pattern checks).
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""


def _is_enforcing(env: Optional[dict] = None) -> bool:
    """gemini #1 option c: only enforce when AQG_AGENT set to known value."""
    e = env if env is not None else os.environ
    return e.get("AQG_AGENT", "") in ENFORCING_AGENTS


def _resolve_ledger_path(ledger_arg: Optional[str], cwd: Path) -> Path:
    """Default: .aqg/current_ledger.md (per audit C1).

    Always returns the fully-resolved target (Path.resolve follows symlinks),
    so callers can bound-check the real target (P3). Resolution only — bounding
    enforcement lives in main via _ledger_path_within_bounds.
    """
    if ledger_arg:
        return Path(ledger_arg).resolve()
    return (cwd / ".aqg" / "current_ledger.md").resolve()


def _ledger_path_within_bounds(resolved_ledger: Path, cwd: Path) -> bool:
    """P3: the resolved ledger target must live under cwd/.aqg/code-construction/.

    This bounds both an explicit --ledger arg and the canonical
    .aqg/current_ledger.md symlink (resolved). A symlink whose target escapes
    code-construction/ resolves outside the bound dir and is rejected, so a
    stale/external ledger cannot satisfy the check.
    """
    bound_dir = (cwd / ".aqg" / "code-construction").resolve()
    try:
        try:
            return resolved_ledger.is_relative_to(bound_dir)
        except AttributeError:  # Python < 3.9 fallback
            resolved_ledger.relative_to(bound_dir)
            return True
    except (ValueError, OSError):
        return False


def _split_md_table_row(line: str) -> Optional[list[str]]:
    """v2-D7: parse markdown table row using split('|'), allowing pipe in cells.

    Returns list of stripped cell strings, or None if not a valid row.
    """
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|"):
        return None
    cells = [c.strip() for c in line.split("|")[1:-1]]
    if not cells:
        return None
    if all("---" in c or c == "" for c in cells):
        return None  # separator row
    return cells


def _parse_ledger_header(content: str) -> Optional[LedgerHeader]:
    """v2-D6: stricter YAML parser; skipped_checks parsed as JSON list."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not m:
        return None
    fm = m.group(1)
    fields: dict[str, str] = {}
    for raw_line in fm.split("\n"):
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # v2-D6: reject multi-line or unindented continuation
        if line[0] in (" ", "\t"):
            return None  # we don't support multi-line scalars in v1
        if ":" not in line:
            return None
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        # Strip quotes
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            v = v[1:-1]
        fields[k] = v

    required = {"task_slug", "path", "created_at", "session_agent"}
    if not required.issubset(fields):
        return None
    if fields["path"] not in TIER_OBJECTION_MIN:
        return None
    if not fields["task_slug"]:
        return None

    # v2-D6: parse skipped_checks as JSON list
    skipped_raw = fields.get("skipped_checks", "[]").strip()
    try:
        if skipped_raw in ("", "null", "None"):
            skipped: list[dict] = []
        else:
            parsed = json.loads(skipped_raw)
            if not isinstance(parsed, list):
                return None
            skipped = parsed
    except json.JSONDecodeError:
        return None

    coverage_raw = fields.get("objections_diff_coverage_exception", "null").strip()
    coverage_exception: Optional[str] = (
        None if coverage_raw in ("null", "None", "") else coverage_raw
    )

    # v0.14.0: optional (legacy ledgers lack it → .get default → None, never raises).
    # Kept RAW (empty-string → None only); null/none/~/vague activeness is decided
    # later by _bc_exception_active so a single place owns the normalization.
    bc_exc_raw = fields.get("behavior_contract_exception", "").strip()
    behavior_contract_exception: Optional[str] = bc_exc_raw or None

    return LedgerHeader(
        task_slug=fields["task_slug"],
        path=fields["path"],
        created_at=fields["created_at"],
        session_agent=fields["session_agent"],
        skipped_checks=skipped,
        objections_diff_coverage_exception=coverage_exception,
        behavior_contract_exception=behavior_contract_exception,
    )


def _parse_ledger_steps(content: str) -> list[StepRow]:
    """v2-C1: parse the 6-step table from ledger.

    Returns list of StepRow; empty if table malformed/missing.
    """
    table_match = re.search(
        r"\|\s*step\s*\|\s*required\s*\|\s*evidence\s*\|\s*file:line\s*\|\s*command/result\s*\|.*?\n((?:\|.*\n)+)",
        content,
        re.MULTILINE | re.IGNORECASE,
    )
    if not table_match:
        return []
    rows_text = table_match.group(1)
    rows: list[StepRow] = []
    for line in rows_text.split("\n"):
        cells = _split_md_table_row(line)
        if cells is None:
            continue
        # G3: a shell pipe inside the command/result cell over-splits the row.
        # Rejoin the surplus (cells[4:]) with '|' into the final cell so a
        # piped command (e.g. `pytest -q | tee out.txt`) stays a valid 5-col row.
        if len(cells) > 5:
            cells = cells[:4] + [" | ".join(cells[4:])]
        if len(cells) != 5:
            continue
        rows.append(
            StepRow(
                step=cells[0],
                required=cells[1],
                evidence=cells[2],
                file_line=cells[3],
                command_result=cells[4],
            )
        )
    return rows


def _get_diff_files(mode: str, base: Optional[str], cwd: Path) -> tuple[list[str], int]:
    """Return (changed_files, total_lines_changed) per --mode.

    v2-D5: fail-closed on git error (raises RuntimeError).
    """
    if mode == "staged":
        rc, out = _safe_run(["git", "-C", str(cwd), "diff", "--cached", "--numstat"])
    elif mode == "worktree":
        rc, out = _safe_run(["git", "-C", str(cwd), "diff", "--numstat"])
    elif mode == "base":
        b = base or "origin/main"
        rc, out = _safe_run(
            ["git", "-C", str(cwd), "diff", f"{b}...HEAD", "--numstat"]
        )
    else:
        raise RuntimeError(f"unknown mode: {mode}")
    if rc != 0:
        # v2-D5: fail-closed
        raise RuntimeError(f"git diff failed (mode={mode}, rc={rc}): {out[:200]}")
    files: list[str] = []
    total_lines = 0
    for line in out.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            adds = int(parts[0]) if parts[0].isdigit() else 0
            dels = int(parts[1]) if parts[1].isdigit() else 0
            total_lines += adds + dels
            files.append(parts[2])
    return files, total_lines


def _diff_unified0_cmd(
    file_path: str, mode: str, base: Optional[str], cwd: Path
) -> list[str]:
    """Build the per-file `git diff --unified=0` command for a given mode.

    Shared by _get_diff_added_lines (content) and _get_diff_added_line_numbers
    (hunk-header line numbers) so both read the SAME mode's diff (P1/C1).
    """
    if mode == "staged":
        return [
            "git", "-C", str(cwd), "diff", "--cached", "--unified=0", "--", file_path,
        ]
    if mode == "base":
        b = base or "origin/main"
        return [
            "git", "-C", str(cwd), "diff", f"{b}...HEAD", "--unified=0", "--", file_path,
        ]
    # worktree
    return ["git", "-C", str(cwd), "diff", "--unified=0", "--", file_path]


def _get_diff_added_lines(
    file_path: str, mode: str, base: Optional[str], cwd: Path
) -> list[str]:
    """v2-D1: get only ADDED lines (content) from diff for a specific file."""
    cmd = _diff_unified0_cmd(file_path, mode, base, cwd)
    rc, out = _safe_run(cmd, timeout=TIMEOUT_GIT_SHOW_S)
    if rc != 0:
        # C2: fail-closed (was silent []). A silent empty result means W5/W6
        # warnings are never generated for an un-diffable file — i.e. the gate
        # false-passes. Match _get_diff_files: raise and let main exit non-zero.
        raise RuntimeError(
            f"git diff (added lines) failed (file={file_path}, mode={mode}, rc={rc}): {out[:200]}"
        )
    added = []
    for line in out.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return added


# Hunk header: @@ -a,b +c,d @@  (b and d optional; default 1).
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _get_diff_added_line_numbers(
    file_path: str, mode: str, base: Optional[str], cwd: Path
) -> set[int]:
    """C1: return the set of post-image line NUMBERS that the diff ADDS.

    Parses `git diff --unified=0` hunk headers `@@ -a,b +c,d @@`: the added
    range starts at line c and spans d lines (d defaults to 1; d==0 means a
    pure deletion with no added lines). Line numbers are in the NEW (post)
    image, so they align with handler line numbers from the same mode's
    post-image (see _read_post_image). Fail-closed on git error (C2).
    """
    cmd = _diff_unified0_cmd(file_path, mode, base, cwd)
    rc, out = _safe_run(cmd, timeout=TIMEOUT_GIT_SHOW_S)
    if rc != 0:
        raise RuntimeError(
            f"git diff (added line numbers) failed "
            f"(file={file_path}, mode={mode}, rc={rc}): {out[:200]}"
        )
    added: set[int] = set()
    for line in out.split("\n"):
        m = _HUNK_HEADER_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        for ln in range(start, start + count):
            added.add(ln)
    return added


def _read_post_image(
    file_path: str, mode: str, base: Optional[str], cwd: Path
) -> Optional[str]:
    """P1: read the post-image content appropriate to the mode.

    - staged:   the staged blob (`git show :<path>`)
    - base:     the committed HEAD blob (`git show HEAD:<path>`) — the new side
                of `{base}...HEAD`
    - worktree: the working-tree file on disk

    Returns None when the post-image cannot be read (e.g. file absent on the
    relevant side); callers treat None as "nothing to check".
    """
    if mode == "worktree":
        full = (cwd / file_path).resolve()
        if not full.exists():
            return None
        try:
            return full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    post_ref = ":" if mode == "staged" else "HEAD:"
    # NOTE: use a raw runner (not _safe_run) so leading/trailing whitespace is
    # preserved — line numbers must align exactly with the diff hunk headers.
    rc, out = _git_show_raw(
        ["git", "-C", str(cwd), "show", f"{post_ref}{file_path}"]
    )
    if rc != 0:
        return None
    return out


def _check_size_match(declared: str, actual_lines: int) -> Optional[str]:
    """audit D9: declared path must match actual diff size at pre-commit."""
    if declared == "mini" and actual_lines > SIZE_MINI_MAX:
        return (
            f"declared path=mini but diff has {actual_lines} lines "
            f"(> {SIZE_MINI_MAX}); upgrade to full or split task"
        )
    if declared == "full" and actual_lines > SIZE_FULL_MAX:
        return (
            f"declared path=full but diff has {actual_lines} lines "
            f"(> {SIZE_FULL_MAX}); upgrade to plan or split task"
        )
    return None


def _split_objections(obj_section: str) -> list[str]:
    """Split objections section into individual objection text blocks."""
    parts = re.split(r"(?:^|\n)\s*\d+\.\s+\*\*Objection\*\*:", obj_section)
    return [p.strip() for p in parts[1:] if p.strip()]


def _is_path_in_repo(file_path: str, cwd: Path) -> bool:
    """v2-C4: prevent path traversal — file must resolve inside repo root."""
    try:
        full = (cwd / file_path).resolve()
        repo_resolved = cwd.resolve()
        # Python 3.9+ has is_relative_to
        try:
            return full.is_relative_to(repo_resolved)
        except AttributeError:
            # Fallback for older Python
            try:
                full.relative_to(repo_resolved)
                return True
            except ValueError:
                return False
    except (OSError, ValueError):
        return False


def _is_file_line_path(fp: str) -> bool:
    """G4: decide if a candidate token is a real file path (not prose).

    Accept dotted paths (has an extension), path-like tokens (contain '/'),
    or known extensionless filenames (Makefile/Dockerfile/LICENSE). Rejects
    bare prose numbers like the '2' in 'step 2:10'.
    """
    if "/" in fp:
        return True
    base = fp.rsplit("/", 1)[-1]
    if base in KNOWN_NO_EXT_FILENAMES:
        return True
    # Dotted extension: a '.' that is not the leading char and has chars after it.
    dot = base.rfind(".")
    return dot > 0 and dot < len(base) - 1


def _check_objections(
    content: str,
    header: LedgerHeader,
    changed_files: list[str],
    cwd: Path,
) -> list[str]:
    """audit C2 + D10 + v2-C4: tiered count + path-existence + bound + diff coverage + non-vague mitigation."""
    blockers: list[str] = []
    m = re.search(
        r"##\s*Predicted Objections.*?\n(.*?)(?=\n##\s|\Z)", content, re.DOTALL
    )
    if not m:
        min_count = TIER_OBJECTION_MIN[header.path]
        blockers.append(
            f"missing '## Predicted Objections' section (path={header.path} requires {min_count})"
        )
        return blockers
    obj_section = m.group(1)
    obj_items = _split_objections(obj_section)
    min_count = TIER_OBJECTION_MIN[header.path]
    if len(obj_items) < min_count:
        blockers.append(
            f"only {len(obj_items)} objections; path={header.path} requires >= {min_count}"
        )
        return blockers

    has_diff_ref = False
    for i, item in enumerate(obj_items, 1):
        # G4: broaden beyond dotted paths — also accept path-like (contains '/')
        # or known extensionless filenames (Makefile/Dockerfile/LICENSE).
        file_refs = [
            (fp, ln)
            for fp, ln in FILE_LINE_CANDIDATE_REGEX.findall(item)
            if _is_file_line_path(fp)
        ]
        if not file_refs:
            blockers.append(f"objection #{i}: no file:line reference")
            continue
        # v2-C4: path bound check (reject out-of-repo paths)
        for fp, _ln in file_refs:
            if not _is_path_in_repo(fp, cwd):
                blockers.append(
                    f"objection #{i}: path '{fp}' resolves outside repo (path traversal blocked)"
                )
                continue
            full = (cwd / fp).resolve()
            if not full.exists():
                blockers.append(f"objection #{i}: file does not exist: {fp}")
        for fp, _ln in file_refs:
            if fp in changed_files:
                has_diff_ref = True
                break
        mit_match = re.search(r"\*\*Mitigation\*\*:\s*(.+?)$", item, re.DOTALL)
        if not mit_match:
            blockers.append(f"objection #{i}: no Mitigation")
            continue
        mit_text = mit_match.group(1).strip()
        if VAGUE_REGEX.search(mit_text):
            short = mit_text[:50].replace("\n", " ")
            blockers.append(f"objection #{i}: vague mitigation ('{short}...')")

    if not has_diff_ref and not header.objections_diff_coverage_exception:
        blockers.append(
            "no objection refs a changed file in diff; "
            "set 'objections_diff_coverage_exception' in header to acknowledge"
        )
    return blockers


def _check_secrets(content: str) -> list[str]:
    """audit D8: Layer 1 hard reject if secret patterns found in ledger."""
    blockers: list[str] = []
    for pattern, name in SECRET_PATTERNS:
        if pattern.search(content):
            blockers.append(f"secret detected in ledger: {name}")
    return blockers


def _check_created_at(
    header: LedgerHeader, changed_files: list[str], cwd: Path
) -> list[str]:
    """audit D1: ledger created_at <= earliest edit mtime."""
    blockers: list[str] = []
    try:
        ledger_dt = dt.datetime.fromisoformat(
            header.created_at.replace("Z", "+00:00")
        )
        if ledger_dt.tzinfo is None:
            ledger_dt = ledger_dt.replace(tzinfo=dt.timezone.utc)
    except (ValueError, AttributeError):
        return [f"invalid created_at format (expected ISO 8601): {header.created_at}"]
    earliest: Optional[dt.datetime] = None
    for f in changed_files:
        full = (cwd / f).resolve()
        if full.exists():
            mtime = dt.datetime.fromtimestamp(full.stat().st_mtime, tz=dt.timezone.utc)
            if earliest is None or mtime < earliest:
                earliest = mtime
    if earliest and ledger_dt > earliest:
        blockers.append(
            f"ledger created_at ({header.created_at}) > earliest edit mtime "
            f"({earliest.isoformat()}); ledger appears backfilled, create at session start"
        )
    return blockers


def _count_broad_excepts(content: str) -> int:
    """v2-C2: count broad except handlers including tuple form. Returns -1 on parse fail."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return -1
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            t = node.type
            if t is None:
                count += 1
            elif isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"):
                count += 1
            elif isinstance(t, ast.Tuple):
                if any(
                    isinstance(elt, ast.Name) and elt.id in ("Exception", "BaseException")
                    for elt in t.elts
                ):
                    count += 1
    return count


def _ast_has_broad_except(content: str) -> bool:
    """Backwards-compatible boolean version (v1 + v2-C2 tuple support)."""
    return _count_broad_excepts(content) > 0


def _broad_except_locations(content: str) -> list[int]:
    """Return list of (1-indexed) line numbers for broad except handlers."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    locs: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            t = node.type
            is_broad = (
                t is None
                or (isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"))
                or (
                    isinstance(t, ast.Tuple)
                    and any(
                        isinstance(elt, ast.Name) and elt.id in ("Exception", "BaseException")
                        for elt in t.elts
                    )
                )
            )
            if is_broad:
                locs.append(node.lineno)
    return locs


def _handler_is_boundary_marked(post_lines: list[str], handler_lineno: int) -> bool:
    """True if a BOUNDARY_MARKER sits within BOUNDARY_MARKER_LOOKBACK lines above.

    handler_lineno is 1-indexed (ast node.lineno); post_lines is 0-indexed.
    """
    start = max(0, handler_lineno - BOUNDARY_MARKER_LOOKBACK)
    for i in range(start, handler_lineno):
        if i < len(post_lines) and BOUNDARY_MARKER in post_lines[i]:
            return True
    return False


def _check_diff_aware_broad_except(
    file_path: str, mode: str, base: Optional[str], cwd: Path
) -> Optional[str]:
    """v2-C2 + C1 + P1: block on NEW unmarked broad-except handlers, per handler.

    C1: aggregate count/marker math false-passed when an old handler was deleted
        and a new one added (count unchanged), or when an old handler gained a
        marker while a new unmarked one was added (marker math cancels). Instead
        we identify which broad handlers sit on diff-ADDED lines (per-handler
        newness) and require a boundary marker above EACH such handler.
    P1: the post-image is read per mode (staged blob / HEAD blob / worktree) so
        a committed unmarked handler is not masked by a clean worktree, and the
        handler line numbers align with the same mode's added-line numbers.
    """
    post_content = _read_post_image(file_path, mode, base, cwd)
    if post_content is None:
        return None  # nothing to check (file absent on the relevant side)
    post_locs = _broad_except_locations(post_content)
    if not post_locs:
        return None  # no broad handlers in post (or AST parse failure)

    # C1: which post-image lines were ADDED by this diff (same mode, fail-closed).
    added_lines = _get_diff_added_line_numbers(file_path, mode, base, cwd)

    post_lines = post_content.split("\n")
    new_unmarked = 0
    for handler_lineno in post_locs:
        if handler_lineno not in added_lines:
            continue  # pre-existing handler — not our concern
        if not _handler_is_boundary_marked(post_lines, handler_lineno):
            new_unmarked += 1

    if new_unmarked > 0:
        return (
            f"{file_path}: {new_unmarked} new broad `except Exception/BaseException` "
            f"handler(s) without `{BOUNDARY_MARKER}` marker within "
            f"{BOUNDARY_MARKER_LOOKBACK} lines"
        )
    return None


def _classify_diff(changed_files: list[str]) -> tuple[bool, bool, bool]:
    """v2-C1: classify diff into (has_prod, has_schema, has_docs)."""
    has_prod = any(
        any(p.search(f) for p in PROD_PATH_PATTERNS) for f in changed_files
    )
    has_schema = any(
        any(p.search(f) for p in SCHEMA_PATH_PATTERNS) for f in changed_files
    )
    has_docs = any(
        any(p.search(f) for p in DOC_PATH_PATTERNS) for f in changed_files
    )
    return has_prod, has_schema, has_docs


def _check_six_step_table(
    content: str, header: LedgerHeader, changed_files: list[str]
) -> list[str]:
    """v2-C1: 3 hard blockers based on diff classification + step row presence."""
    blockers: list[str] = []
    rows = _parse_ledger_steps(content)
    if not rows:
        blockers.append(
            "ledger 6-step table missing or malformed (5 columns: step|required|evidence|file:line|command/result)"
        )
        return blockers

    has_prod, has_schema, _ = _classify_diff(changed_files)

    def _find_row(prefix: str, alt_keyword: str) -> Optional[StepRow]:
        for r in rows:
            if r.step.startswith(prefix) or alt_keyword in r.step:
                return r
        return None

    # HB1: production code changed but no Behavior Lock (step 2) row with evidence
    if has_prod:
        step2 = _find_row("2.", "Behavior Lock")
        empty_markers = ("", "-", "N/A", "<...>", "<what focused test you wrote/extended FIRST>")
        if not step2 or step2.evidence in empty_markers:
            blockers.append(
                "HB1: production code changed but ledger row 2 (Behavior Lock) has no evidence"
            )

    # HB2: schema/contract changed but step 5 (Local Verification) missing or no regression keyword
    if has_schema:
        step5 = _find_row("5.", "Local Verification")
        if not step5:
            blockers.append(
                "HB2: schema/contract changed but ledger row 5 (Local Verification) missing"
            )
        else:
            cmd = step5.command_result.lower()
            if not any(kw.lower() in cmd for kw in REGRESSION_KEYWORDS):
                blockers.append(
                    f"HB2: schema/contract changed but ledger row 5 command/result lacks regression keyword "
                    f"(one of: pytest/unittest/regression/PASS/OK)"
                )

    # HB4: skipped_checks list non-empty but reason vague/empty
    for entry in header.skipped_checks:
        if not isinstance(entry, dict):
            blockers.append(
                f"HB4: skipped_checks entry not a dict {{check, reason}}: {entry!r}"
            )
            continue
        reason = entry.get("reason", "")
        if not reason or VAGUE_REGEX.search(reason):
            blockers.append(
                f"HB4: skipped_checks entry '{entry.get('check', '?')}' has vague/empty reason"
            )

    return blockers


# ===== v0.14.0 Behavior Contract parsing + advisory checks =====
_BC_SECTION_RE = re.compile(r"^##\s+Behavior Contract\s*$")
_BC_REQ_RE = re.compile(r"^###\s+(R\d+):\s*(.*)$")
_BC_SCEN_RE = re.compile(r"^\s*-\s*Scenario\s+(S\d+\.\d+):?", re.IGNORECASE)
_BC_NORM_RE = re.compile(r"\b(?:MUST|SHALL)\b")  # case-sensitive RFC-2119 convention
_BC_H2_RE = re.compile(r"^##\s+")
# case-sensitive + \b-anchored: ids are uppercase R\d+ (per _BC_REQ_RE), and the
# anchor stops `discovers:`/`recovers:` substrings from matching (audit eb897557 f3).
_BC_COVERS_RE = re.compile(r"\bcovers:\s*(R\d+(?:\s*,\s*R\d+)*)")
_BC_INACTIVE_EXCEPTION = {"", "null", "none", "~"}


def parse_behavior_contract(content: str) -> BehaviorContract:
    """Parse the '## Behavior Contract' section. TOTAL — never raises.

    Line-based (zero new deps, per a3). On any internal error returns a contract
    with parse_error set (the caller renders it as a BC0 advisory).
    """
    try:
        return _parse_behavior_contract_impl(content)
    except Exception as exc:  # aqg: top-level boundary
        return BehaviorContract(
            present=False, requirements=(), parse_error=f"{type(exc).__name__}: {exc}"
        )


def _parse_behavior_contract_impl(content: str) -> BehaviorContract:
    lines = content.splitlines()
    start = next((i for i, ln in enumerate(lines) if _BC_SECTION_RE.match(ln)), None)
    if start is None:
        return BehaviorContract(present=False, requirements=(), parse_error=None)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _BC_H2_RE.match(lines[j]) and not lines[j].startswith("###"):
            end = j
            break
    section = lines[start + 1 : end]

    reqs: list[BCRequirement] = []
    cur_id: Optional[str] = None
    cur_title: Optional[str] = None
    stmt_lines: list[str] = []
    scenarios: list[BCScenario] = []
    scen_id: Optional[str] = None
    scen_lines: list[str] = []

    def _flush_scenario() -> None:
        nonlocal scen_id, scen_lines
        if scen_id is not None:
            # skip scen_lines[0] (the "- Scenario S1.1: <title>" line) so a marker
            # word in the title can't false-satisfy a marker (audit eb897557 f4).
            block = "\n".join(scen_lines[1:])
            markers = frozenset(
                m
                for m in ("GIVEN", "WHEN", "THEN")
                if re.search(rf"\b{m}\b", block, re.IGNORECASE)
            )
            scenarios.append(
                BCScenario(id=scen_id, markers=markers, lines=tuple(scen_lines))
            )
        scen_id = None
        scen_lines = []

    def _flush_requirement() -> None:
        nonlocal cur_id, cur_title, stmt_lines, scenarios
        if cur_id is not None:
            _flush_scenario()
            statement = "\n".join(stmt_lines).strip()
            reqs.append(
                BCRequirement(
                    id=cur_id,
                    title=(cur_title or "").strip(),
                    statement=statement,
                    has_normative=bool(_BC_NORM_RE.search(statement)),
                    scenarios=tuple(scenarios),
                )
            )
        cur_id = None
        cur_title = None
        stmt_lines = []
        scenarios = []

    for ln in section:
        mreq = _BC_REQ_RE.match(ln)
        if mreq:
            _flush_requirement()
            cur_id, cur_title = mreq.group(1), mreq.group(2)
            continue
        if cur_id is None:
            continue
        mscen = _BC_SCEN_RE.match(ln)
        if mscen:
            _flush_scenario()
            scen_id = mscen.group(1)
            scen_lines = [ln]
            continue
        if scen_id is not None:
            scen_lines.append(ln)
        else:
            stmt_lines.append(ln)
    _flush_requirement()
    return BehaviorContract(present=True, requirements=tuple(reqs), parse_error=None)


def _bc_exception_active(value: Optional[str]) -> bool:
    """The whole-section BC opt-out is active only for a non-empty, non-null
    (case-insensitive), non-vague reason. Mirrors the
    objections_diff_coverage_exception precedent + VAGUE_REGEX."""
    if value is None:
        return False
    if str(value).strip().lower() in _BC_INACTIVE_EXCEPTION:
        return False
    if VAGUE_REGEX.search(value):
        return False
    return True


def _bc_row2_evidence(content: str) -> str:
    for r in _parse_ledger_steps(content):
        if r.step.startswith("2.") or "Behavior Lock" in r.step:
            return r.evidence
    return ""


def _bc_covers_ids(evidence: str) -> list[str]:
    m = _BC_COVERS_RE.search(evidence)
    if not m:
        return []
    return re.findall(r"R\d+", m.group(1))


def _check_behavior_contract(
    content: str, header: LedgerHeader, changed_files: list[str]
) -> list[str]:
    """WARN-ONLY advisories (BC0-BC4). Scope: prod change at full/plan tier.

    TOTAL — any internal error becomes a BC0 advisory, never a raise. The caller
    prints these to stderr and includes them in the report, but MUST NOT feed
    them into _check_warning_acknowledgements or the hard-blocker list, so BC can
    never change the process exit code in 0.14.0 (warn-only rollout phase 1).
    """
    try:
        has_prod, _, _ = _classify_diff(changed_files)
        if not has_prod or header.path not in ("full", "plan"):
            return []
        if _bc_exception_active(header.behavior_contract_exception):
            return []
        return _check_behavior_contract_impl(content)
    except Exception as exc:  # aqg: top-level boundary — scope/classify included (audit f3)
        return [f"BC0: behavior contract check error: {type(exc).__name__}: {exc}"]


def _check_behavior_contract_impl(content: str) -> list[str]:
    contract = parse_behavior_contract(content)
    if contract.parse_error:
        return [f"BC0: behavior contract parse error: {contract.parse_error}"]
    if not contract.present:
        return [
            "BC1: behavior contract section missing "
            "(expected '## Behavior Contract' at full/plan tier with prod changes)"
        ]
    if not contract.requirements:
        return ["BC1: behavior contract section has no requirement (### R<n>: …)"]

    advisories: list[str] = []

    # BC4: duplicate requirement ids
    counts: dict[str, int] = {}
    for r in contract.requirements:
        counts[r.id] = counts.get(r.id, 0) + 1
    for rid, n in counts.items():
        if n > 1:
            advisories.append(f"BC4: duplicate requirement id {rid} ({n}×)")

    # BC1 (per-requirement keyword) + BC2 (per-requirement scenario + per-scenario markers)
    for r in contract.requirements:
        if not r.has_normative:
            advisories.append(
                f"BC1: requirement {r.id} statement has no normative keyword "
                f"(MUST / MUST NOT / SHALL / SHALL NOT)"
            )
        if not r.scenarios:
            advisories.append(f"BC2: requirement {r.id} has no scenario")
        for s in r.scenarios:
            missing = [m for m in ("GIVEN", "WHEN", "THEN") if m not in s.markers]
            if missing:
                advisories.append(
                    f"BC2: scenario {s.id} missing marker(s): {', '.join(missing)}"
                )

    # BC3: row-2 covers coverage (both directions) via the covers: micro-syntax
    declared = [r.id for r in contract.requirements]
    declared_set = set(declared)
    covers = _bc_covers_ids(_bc_row2_evidence(content))
    if not covers:
        advisories.append(
            "BC3: row-2 (Behavior Lock) evidence missing a 'covers: R…' citation"
        )
    else:
        covers_set = set(covers)
        for cid in covers:
            if cid not in declared_set:
                advisories.append(
                    f"BC3: row-2 covers {cid} which is not a declared requirement (dangling)"
                )
        for rid in declared:
            if rid not in covers_set:
                advisories.append(
                    f"BC3: requirement {rid} declared but not cited in row-2 covers"
                )
    return advisories


def _check_anti_patterns(
    changed_files: list[str], cwd: Path, mode: str, base: Optional[str],
    total_lines: int, declared_path: str,
) -> tuple[list[str], list[str]]:
    """v2-C2 + v2-D1: diff-aware anti-pattern checks. Return (hard_blockers, warnings)."""
    blockers: list[str] = []
    warnings: list[str] = []
    has_prod, has_schema, has_docs = _classify_diff(changed_files)

    for f in changed_files:
        full = (cwd / f).resolve()
        if not full.exists():
            continue
        # v2-C2: diff-aware broad except (only block on NEW unmarked)
        if f.endswith(".py"):
            broad_blocker = _check_diff_aware_broad_except(f, mode, base, cwd)
            if broad_blocker:
                blockers.append(broad_blocker)

        # v2-D1: TODO/FIXME from ADDED lines only (not full file scan).
        # C5 dogfood fix: match only code-debt marker forms (comment leader /
        # `TODO:` colon), not a functional `| ... | TODO |` table cell or prose
        # mention, which legitimately appear in docs/skills like aqg-evidence-closeout.
        if not f.endswith((".pyc", ".png", ".jpg", ".gif", ".jpeg")):
            added_lines = _get_diff_added_lines(f, mode, base, cwd)
            added_text = "\n".join(added_lines)
            if TODO_FIXME_DEBT_REGEX.search(added_text):
                warnings.append(f"{f}: contains new TODO/FIXME (W5)")

        # v2-D1: new dep entry from added lines
        if f in ("package.json", "requirements.txt", "pyproject.toml"):
            added_lines = _get_diff_added_lines(f, mode, base, cwd)
            if added_lines:  # any added line in dep file = warn
                warnings.append(f"new dep entry in {f} (W6); ensure ledger has Warning Ack")

    # v2-D1: W7 large diff without plan declaration
    if total_lines > SIZE_FULL_MAX and declared_path != "plan":
        warnings.append(
            f"W7: diff is {total_lines} lines (> {SIZE_FULL_MAX}) but path={declared_path}; consider 'plan' tier"
        )

    # v2-D1: W8 docs/status changed
    if has_docs:
        doc_files = [f for f in changed_files if any(p.search(f) for p in DOC_PATH_PATTERNS)]
        warnings.append(
            f"W8: docs/status changed in {len(doc_files)} file(s); ensure ledger Warning Ack with source ref"
        )

    return blockers, warnings


def _check_warning_acknowledgements(
    content: str, warnings: list[str]
) -> list[str]:
    """v2-D1 + D7: warn must have explicit ack matched by condition (W5/W6/W7/W8 codes)."""
    if not warnings:
        return []
    m = re.search(
        r"##\s*Warning Acknowledgements.*?\n(.*?)(?=\n##\s|\Z)", content, re.DOTALL
    )
    if not m:
        return [
            f"warnings present ({len(warnings)}) but no '## Warning Acknowledgements' section"
        ]
    ack_section = m.group(1)
    # v2-D7: use line.split('|') instead of regex
    real_rows: list[tuple[str, str, str]] = []
    for line in ack_section.split("\n"):
        cells = _split_md_table_row(line)
        if cells is None or len(cells) < 3:
            continue
        if cells[0].lower().startswith("condition"):
            continue  # header row
        real_rows.append((cells[0], cells[1], cells[2]))

    blockers: list[str] = []
    for warn in warnings:
        warn_code_match = re.search(r"\bW\d\b", warn)
        warn_code = warn_code_match.group(0) if warn_code_match else None
        matched = False
        for cond, ack, reason in real_rows:
            cond_lower = cond.lower()
            if warn_code and warn_code.lower() in cond_lower:
                if ack.strip().lower() not in ("yes", "ack", "y", "true"):
                    blockers.append(
                        f"warning '{warn}' ack value '{ack}' is not 'yes' (case-insensitive)"
                    )
                elif not reason or VAGUE_REGEX.search(reason):
                    blockers.append(f"warning '{warn}' ack reason vague/empty")
                matched = True
                break
        if not matched:
            blockers.append(
                f"warning '{warn}' has no matching ack row in '## Warning Acknowledgements'"
            )
    return blockers


def main(argv: Optional[list[str]] = None) -> int:
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
    parser = argparse.ArgumentParser(
        prog="aqg_construction_check",
        description="AQG code construction checker (v2 — sketch a1 + PR-B audit fixes)",
    )
    parser.add_argument(
        "--ledger", help="Path to ledger file (default: .aqg/current_ledger.md)"
    )
    parser.add_argument(
        "--mode", choices=["staged", "worktree", "base"], default="staged"
    )
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    start_time = time.monotonic()

    if not _is_enforcing():
        if not args.quiet:
            print(
                "AQG inactive: AQG_AGENT not set; skipping enforcement",
                file=sys.stderr,
            )
        return EXIT_OK

    ledger_path = _resolve_ledger_path(args.ledger, cwd)
    if not ledger_path.exists():
        print(f"ERROR: ledger not found at {ledger_path}", file=sys.stderr)
        print(
            "Create one at .aqg/code-construction/<task-slug>.md "
            "and symlink to .aqg/current_ledger.md",
            file=sys.stderr,
        )
        return EXIT_LEDGER_MALFORMED

    # P3: bound the resolved ledger target to cwd/.aqg/code-construction/ so an
    # out-of-tree path or a symlink that escapes that dir cannot satisfy the
    # check with a stale/external ledger.
    if not _ledger_path_within_bounds(ledger_path, cwd):
        bound_dir = (cwd / ".aqg" / "code-construction").resolve()
        print(
            f"ERROR: ledger target {ledger_path} resolves outside {bound_dir} "
            "(out-of-tree path or symlink escape rejected)",
            file=sys.stderr,
        )
        return EXIT_LEDGER_MALFORMED

    try:
        content = ledger_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ERROR: cannot read ledger {ledger_path}: {exc}", file=sys.stderr)
        return EXIT_LEDGER_MALFORMED

    secret_blockers = _check_secrets(content)
    if secret_blockers:
        for b in secret_blockers:
            print(f"BLOCKER: {b}", file=sys.stderr)
        print(
            "Redact ledger before commit (Layer 1 hard reject per audit D8)",
            file=sys.stderr,
        )
        return EXIT_SECRET_DETECTED

    header = _parse_ledger_header(content)
    if header is None:
        print(
            "ERROR: ledger header malformed (missing yaml frontmatter, multi-line scalar, "
            "or required fields task_slug/path/created_at/session_agent)",
            file=sys.stderr,
        )
        return EXIT_LEDGER_MALFORMED

    # v2-D5: fail-closed on git error
    try:
        changed_files, total_lines = _get_diff_files(args.mode, args.base, cwd)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    if args.mode == "staged":
        size_err = _check_size_match(header.path, total_lines)
        if size_err:
            print(f"BLOCKER: {size_err}", file=sys.stderr)
            return EXIT_SIZE_MISMATCH

    created_blockers = _check_created_at(header, changed_files, cwd)

    # v2-C1: 6-step table + 3 hard blockers
    six_step_blockers = _check_six_step_table(content, header, changed_files)

    obj_blockers = _check_objections(content, header, changed_files, cwd)

    # C2: per-file git failures inside anti-pattern checks are fail-closed
    # (broad-except diff + W5/W6 added-line scan both call git per file).
    try:
        ap_blockers, ap_warnings = _check_anti_patterns(
            changed_files, cwd, args.mode, args.base, total_lines, header.path
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    ack_blockers = _check_warning_acknowledgements(content, ap_warnings)

    # v0.14.0 Behavior Contract advisories — WARN-ONLY (phase 1). Computed on its
    # OWN channel: NOT added to ap_warnings (that path forces a Warning-Ack row and
    # would hard-block) and NOT added to all_hard, so BC can never change the exit
    # code this phase. Total by construction (returns a BC0 string on any error).
    bc_advisories = _check_behavior_contract(content, header, changed_files)

    if obj_blockers:
        for b in obj_blockers:
            print(f"BLOCKER: {b}", file=sys.stderr)
        return EXIT_OBJECTION_MALFORMED

    all_hard = created_blockers + six_step_blockers + ap_blockers + ack_blockers
    if all_hard:
        for b in all_hard:
            print(f"BLOCKER: {b}", file=sys.stderr)
        return EXIT_HARD_BLOCK

    if ap_warnings and not args.quiet:
        for w in ap_warnings:
            print(f"WARN: {w}", file=sys.stderr)

    # BC advisories print on the pass path (like ap_warnings) — advisory, exit unchanged.
    if bc_advisories and not args.quiet:
        for a in bc_advisories:
            print(f"WARNING: {a}", file=sys.stderr)

    # v2-D4: performance budget warn-only on overrun
    elapsed = time.monotonic() - start_time
    budget = DEADLINE_S.get(header.path, 60)
    if elapsed > budget and not args.quiet:
        print(
            f"WARN: AQG check took {elapsed:.1f}s (> {budget}s budget for path={header.path})",
            file=sys.stderr,
        )

    if not args.quiet:
        print(
            f"AQG construction check PASSED "
            f"(path={header.path}, files={len(changed_files)}, lines={total_lines}, elapsed={elapsed:.2f}s)"
        )
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        try:
            if not os.environ.get('AQG_NO_UPDATE_CHECK'):
                import importlib.util
                _path = Path(__file__).resolve().parents[3] / 'scripts/aqg_update/nudge.py'
                _spec = importlib.util.spec_from_file_location('_aqg_cli_nudge', _path)
                _nudge = importlib.util.module_from_spec(_spec)
                _spec.loader.exec_module(_nudge)
                _nudge.nudge()
        except (Exception, SystemExit):  # aqg: top-level boundary
            pass  # Missing/broken update support must not change the skill result.
