#!/usr/bin/env python3
"""aqg-test-quality-review core — pure diff/classification/detection helpers.

Imported by the `aqg_test_quality_review.py` entry script (which owns the CLI,
ledger validation, and the exit-code contract). Split out to keep each file
under the 800-line house limit. stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ===== File classification =====================================================

# Test-file path/name conventions across the common ecosystems. Precision-first:
# only paths that clearly look like tests are classified as test files.
_TEST_PATH_RE = re.compile(
    r"(^|/)tests?/"                       # .../test/ or .../tests/ dir
    r"|(^|/)__tests__/"                   # JS __tests__/
    r"|(^|/)spec/"                        # ruby/js spec dir
)
_TEST_FILE_RE = re.compile(
    r"(^|/)test_[^/]+\.py$"               # pytest test_*.py
    r"|(^|/)[^/]+_test\.py$"              # *_test.py
    r"|(^|/)[^/]+_test\.go$"              # go *_test.go
    r"|(^|/)[^/]+\.test\.[jt]sx?$"        # *.test.js/ts/jsx/tsx
    r"|(^|/)[^/]+\.spec\.[jt]sx?$"        # *.spec.js/ts/jsx/tsx
    r"|(^|/)[^/]+Test\.java$"             # JUnit *Test.java
    r"|(^|/)[^/]+Tests\.cs$"              # *Tests.cs
    r"|(^|/)[^/]+_spec\.rb$"              # ruby *_spec.rb
)

# Source files we know how to reason about (code, not config/docs/data).
_SOURCE_EXT_RE = re.compile(
    r"\.(py|go|ts|tsx|js|jsx|rs|java|kt|swift|rb|cs|php|scala)$"
)


def is_test_file(path: str) -> bool:
    """True if the path looks like a test file by name or directory convention."""
    return bool(_TEST_FILE_RE.search(path) or _TEST_PATH_RE.search(path))


def is_source_file(path: str) -> bool:
    """True if the path is reasonable-able source code that is NOT a test file."""
    return bool(_SOURCE_EXT_RE.search(path)) and not is_test_file(path)


# ===== Unified diff parsing ====================================================


@dataclass(frozen=True)
class AddedLine:
    """An added (`+`) line with its line number in the new file."""

    lineno: int
    text: str  # content without the leading '+'


@dataclass(frozen=True)
class FileDiff:
    """One file's changes within a unified diff."""

    path: str  # new path (or old path when deleted)
    is_new: bool = False
    is_deleted: bool = False
    added: tuple[AddedLine, ...] = field(default_factory=tuple)
    removed: tuple[str, ...] = field(default_factory=tuple)  # content w/o leading '-'


_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _strip_ab_prefix(p: str) -> str:
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a unified (`git diff`) text into per-file changes.

    Tracks new-file line numbers for added lines so candidates can cite
    `path:line`. Handles new / deleted files (`/dev/null`). stdlib only.
    """
    files: list[FileDiff] = []
    cur_path: str | None = None
    cur_old: str | None = None
    is_new = False
    is_deleted = False
    added: list[AddedLine] = []
    removed: list[str] = []
    new_lineno = 0

    def flush() -> None:
        nonlocal cur_path, cur_old, is_new, is_deleted, added, removed
        if cur_path is None:
            return
        path = cur_path
        if path == "/dev/null" and cur_old and cur_old != "/dev/null":
            path = cur_old  # deleted file: new side is /dev/null, use old path
        files.append(
            FileDiff(
                path=_strip_ab_prefix(path),
                is_new=is_new,
                is_deleted=is_deleted,
                added=tuple(added),
                removed=tuple(removed),
            )
        )
        cur_path = cur_old = None
        is_new = is_deleted = False
        added = []
        removed = []

    for raw in text.splitlines():
        m = _DIFF_GIT_RE.match(raw)
        if m:
            flush()
            cur_old = m.group(1)
            cur_path = m.group(2)
            new_lineno = 0
            continue
        if cur_path is None:
            continue  # preamble before first file header
        if raw.startswith("new file mode"):
            is_new = True
            continue
        if raw.startswith("deleted file mode"):
            is_deleted = True
            continue
        if raw.startswith("--- "):
            cur_old = raw[4:].strip()
            continue
        if raw.startswith("+++ "):
            cur_path = raw[4:].strip()
            continue
        hm = _HUNK_RE.match(raw)
        if hm:
            new_lineno = int(hm.group(1))
            continue
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        if raw.startswith("+"):
            added.append(AddedLine(lineno=new_lineno, text=raw[1:]))
            new_lineno += 1
        elif raw.startswith("-"):
            removed.append(raw[1:])
        else:  # context line (leading space, or blank within a hunk)
            new_lineno += 1

    flush()
    return files


# ===== Assertion classification (precision-first) ==============================
#
# Per double-audit G5: toMatchSnapshot is BEHAVIORAL (captures serialized
# values), NOT a shape smell. Shape markers are checked BEFORE behavioral so
# `assert isinstance(...)` and `assert type(x) == int` classify as shape even
# though they contain `==`.

_ASSERT_LOOKS_LIKE_RE = re.compile(
    r"^\s*assert\b"
    r"|^\s*self\.assert\w+\s*\("
    r"|\bexpect\s*\("
    r"|\bpytest\.raises\b|\.raises\s*\("
    r"|^\s*assert_that\s*\("  # hamcrest / assertpy
)

_SHAPE_MARKERS_RE = re.compile(
    r"\bisinstance\s*\("
    r"|\bassertIsInstance\b"
    r"|\bhasattr\s*\("
    r"|\binspect\.signature\b"
    r"|\btype\s*\([^)]*\)\s*(?:==|!=|\bis\b)"  # type(x) == / type(x) is
    r"|\.keys\(\)\s*==|==\s*[^=]*\.keys\(\)"   # dict-keys equality
    r"|\b(?:set|list|sorted|frozenset)\s*\([^)]*\.keys\s*\(\)"  # set(x.keys()) == ... (audit P3)
    r"|\btoBeInstanceOf\s*\(|\btoHaveProperty\s*\("
    r"|\binstanceof\b|\btypeof\b|\bObject\.keys\s*\("
)

_BEHAVIORAL_MARKERS_RE = re.compile(
    r"\bassertEqual\b|\bassertNotEqual\b|\bassertAlmostEqual\b"
    r"|\bassertTrue\b|\bassertFalse\b|\bassertIn\b|\bassertNotIn\b"
    r"|\bassertGreater\b|\bassertLess\b|\bassertIsNone\b|\bassertIsNotNone\b"
    r"|\bassertRaises\b|\bpytest\.raises\b|\.raises\s*\("
    r"|\bassert_called|\bassert_any_call|\bassert_awaited|\bassert_has_calls"
    r"|\.toBe\s*\(|\.toEqual\s*\(|\.toStrictEqual\s*\("
    r"|\.toThrow\s*\(|\.toContain\s*\(|\.toMatch\s*\("
    r"|\.toHaveBeenCalled|\.toMatchSnapshot\s*\(|\.toMatchInlineSnapshot\s*\("
    r"|\.resolves\b|\.rejects\b"
)

# plain `assert <expr> <cmp> <expr>` value comparison. ` is `/` is not ` included
# (audit P4) — `type(x) is ...` is already caught by the shape marker checked first.
_PLAIN_CMP_RE = re.compile(r"^\s*assert\b.*(==|!=|<=|>=|<|>| in | not in | is )")


def classify_assertion(line: str) -> str | None:
    """Classify one source line as a 'shape' / 'behavioral' assertion, or None.

    Shape is checked first (more specific). Bare truthiness asserts (`assert x`)
    are ambiguous and return None — precision-first: an unclassifiable assert
    never pushes a test toward a shape_only verdict.
    """
    if not _ASSERT_LOOKS_LIKE_RE.search(line):
        return None
    if _SHAPE_MARKERS_RE.search(line):
        return "shape"
    if _BEHAVIORAL_MARKERS_RE.search(line):
        return "behavioral"
    if _PLAIN_CMP_RE.search(line):
        return "behavioral"
    return None


# ===== Per-test grouping (newly-added test functions only) =====================

_TEST_DEF_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+(test\w*)\b"          # python def test_*
    r"|^\s*func\s+(Test\w*)\s*\("                  # go func Test*
    r"|^\s*(?:it|test)\s*\(\s*['\"]"               # js/ts it('...') / test('...')
)


@dataclass(frozen=True)
class TestRegion:
    """A newly-added test function with its assertion classification tally."""

    name: str
    lineno: int
    shape_lines: tuple[int, ...] = field(default_factory=tuple)
    behavioral_count: int = 0

    @property
    def kind(self) -> str:
        has_shape = bool(self.shape_lines)
        if has_shape and self.behavioral_count == 0:
            return "shape_only"
        if has_shape and self.behavioral_count > 0:
            return "mixed"
        if self.behavioral_count > 0:
            return "behavioral_only"
        return "unknown"


def _test_def_name(line: str) -> str | None:
    m = _TEST_DEF_RE.match(line)
    if not m:
        return None
    for g in m.groups():
        if g:
            return g
    return "<test>"  # it('...') form has no captured name group


# A non-test def/class/func that ends the current test region (audit C2b).
_GENERIC_DEF_RE = re.compile(r"^(\s*)(?:async\s+)?(?:def|class|func)\b")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def group_added_tests(file_diff: FileDiff) -> list[TestRegion]:
    """Group added lines into newly-added test functions.

    Only assertions inside a test whose `def`/`it(` line is itself added are
    grouped — a shape_only verdict requires the WHOLE test to be visible.
    Assertions added into a pre-existing test (def is context, not added) are
    intentionally not grouped here (covered by suppression detection instead).

    The test-def line itself is also classified (audit C2a — single-line
    `it('x', () => expect(...))`). A region ends when a non-test def/class/func
    appears at the same or shallower indent (audit C2b — a later `def helper()`
    must not absorb its assertions into the previous test).
    """
    regions: list[TestRegion] = []
    cur_name: str | None = None
    cur_lineno = 0
    cur_indent = 0
    cur_shape: list[int] = []
    cur_beh = 0

    def flush() -> None:
        nonlocal cur_name, cur_shape, cur_beh
        if cur_name is not None:
            regions.append(
                TestRegion(
                    name=cur_name,
                    lineno=cur_lineno,
                    shape_lines=tuple(cur_shape),
                    behavioral_count=cur_beh,
                )
            )
        cur_name = None
        cur_shape = []
        cur_beh = 0

    for al in file_diff.added:
        name = _test_def_name(al.text)
        if name is not None:
            flush()
            cur_name = name
            cur_lineno = al.lineno
            cur_indent = _indent(al.text)
            # fall through: classify this same line (single-line it/test)
        elif (cur_name is not None and _GENERIC_DEF_RE.match(al.text)
              and _indent(al.text) <= cur_indent):
            flush()  # non-test def/class at same/shallower indent ends the region
            continue
        if cur_name is None:
            continue
        kind = classify_assertion(al.text)
        if kind == "shape":
            cur_shape.append(al.lineno)
        elif kind == "behavioral":
            cur_beh += 1

    flush()
    return regions


# ===== Candidates ==============================================================


@dataclass(frozen=True)
class Candidate:
    """One mechanical finding-candidate. NOT a verdict — the LLM dispositions it.

    confidence drives NLJ: only HIGH-confidence unhandled candidates feed
    needs_llm_judgement (per double-audit C2, generalized to all concerns).
    """

    id: str
    concern: str  # shape_over_behavioral | coverage_gap | test_suppression | flaky
    confidence: str  # high | low
    evidence: str  # path or path:line
    pattern: str
    subtype: str = ""


def _ids(prefix: str):
    n = {"i": 0}

    def nxt() -> str:
        n["i"] += 1
        return f"{prefix}-{n['i']:03d}"

    return nxt


_FLAKY_HIGH_RE = re.compile(
    r"\btime\.sleep\s*\(|\bsetTimeout\s*\("
    r"|\bdatetime\.now\s*\(|\bdatetime\.utcnow\s*\(|\bdate\.today\s*\(|\btime\.time\s*\("
    r"|\bDate\.now\s*\(|\bnew Date\s*\("
)
_FLAKY_LOW_RE = re.compile(
    r"\brandom\.(?:random|randint|choice|shuffle|uniform)\s*\(|\bMath\.random\s*\("
    r"|\brequests\.(?:get|post|put|delete|patch)\s*\(|\burllib\.request\b|\bhttp\.client\b"
    r"|\bfetch\s*\(|\bsocket\.socket\s*\("
)
_SKIP_ADDED_RE = re.compile(
    r"@pytest\.mark\.skip|@pytest\.mark\.skipif|@unittest\.skip|@skip\b"
    r"|\bpytest\.skip\s*\(|\.skipTest\s*\(|\braise SkipTest\b"
    r"|\bxit\s*\(|\bit\.skip\s*\(|\btest\.skip\s*\(|\bdescribe\.skip\s*\(|\bxdescribe\s*\("
    r"|@Ignore\b|@Disabled\b|\bt\.Skip(?:Now)?\s*\("
)


def detect_shape_candidates(test_files: list[FileDiff], nxt=None) -> list[Candidate]:
    """shape_over_behavioral candidates: newly-added tests that are shape_only."""
    nxt = nxt or _ids("shape")
    out: list[Candidate] = []
    for f in test_files:
        for r in group_added_tests(f):
            if r.kind == "shape_only":
                out.append(
                    Candidate(
                        id=nxt(),
                        concern="shape_over_behavioral",
                        confidence="high",
                        evidence=f"{f.path}:{r.shape_lines[0]}",
                        pattern=f"test '{r.name}' asserts only shape (no behavioral assertion)",
                    )
                )
    return out


def detect_flaky_candidates(test_files: list[FileDiff], nxt=None) -> list[Candidate]:
    """Flaky-risk patterns in added test lines. sleep/wall-clock = high; random/net = low."""
    nxt = nxt or _ids("flaky")
    out: list[Candidate] = []
    for f in test_files:
        for al in f.added:
            mh = _FLAKY_HIGH_RE.search(al.text)
            ml = _FLAKY_LOW_RE.search(al.text)
            if mh:
                out.append(Candidate(
                    id=nxt(), concern="flaky", confidence="high",
                    evidence=f"{f.path}:{al.lineno}", pattern=f"flaky: {mh.group(0)}"))
            elif ml:
                out.append(Candidate(
                    id=nxt(), concern="flaky", confidence="low",
                    evidence=f"{f.path}:{al.lineno}", pattern=f"flaky (low-conf): {ml.group(0)}"))
    return out


def detect_suppression_candidates(test_files: list[FileDiff], nxt=None) -> list[Candidate]:
    """test_suppression: skip/disable added, or behavioral assertions deleted."""
    nxt = nxt or _ids("supp")
    out: list[Candidate] = []
    for f in test_files:
        for al in f.added:
            ms = _SKIP_ADDED_RE.search(al.text)
            if ms:
                out.append(Candidate(
                    id=nxt(), concern="test_suppression", confidence="high",
                    evidence=f"{f.path}:{al.lineno}", pattern=f"skip/disable added: {ms.group(0)}",
                    subtype="skip_added"))
        for rline in f.removed:
            if classify_assertion(rline) == "behavioral":
                out.append(Candidate(
                    id=nxt(), concern="test_suppression", confidence="high",
                    evidence=f"{f.path}", pattern=f"behavioral assertion deleted: {rline.strip()[:60]}",
                    subtype="assertion_deleted"))
    return out


def _source_basename(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0]


def _test_pairs_base(test_paths: list[str], base: str) -> bool:
    """True if a changed test file's basename matches a recognized pairing
    convention for source `base` (audit P4 — basename tokens, not raw substring,
    so `src/app.py` is NOT 'paired' with `tests/happy_path_test.py`)."""
    if not base:
        return False
    needles = (
        f"test_{base}.", f"{base}_test.", f"{base}.test.",
        f"{base}.spec.", f"{base}_spec.",
    )
    for tp in test_paths:
        tb = tp.rsplit("/", 1)[-1]
        if tb.startswith(needles):
            return True
    return False


def detect_coverage_gaps(files: list[FileDiff], nxt=None) -> list[Candidate]:
    """source changed without a paired changed test. Confidence per C2:
    high iff the WHOLE diff touched ZERO test files; else low (advisory)."""
    nxt = nxt or _ids("cov")
    sources = [f for f in files if is_source_file(f.path) and not f.is_deleted]
    test_files = [f for f in files if is_test_file(f.path)]
    any_test_changed = bool(test_files)
    test_paths = [f.path for f in test_files]
    out: list[Candidate] = []
    for f in sources:
        base = _source_basename(f.path)
        if _test_pairs_base(test_paths, base):
            continue
        if not any_test_changed:
            out.append(Candidate(
                id=nxt(), concern="coverage_gap", confidence="high",
                evidence=f.path, pattern="source changed but ZERO test files touched in diff"))
        else:
            out.append(Candidate(
                id=nxt(), concern="coverage_gap", confidence="low",
                evidence=f.path, pattern="no paired test file changed (other tests changed elsewhere)"))
    return out


# ===== Counts + bulk_shape advisory ============================================

# Tunable thresholds (per ADR; live here not in SKILL.md per GUIDE anti-pattern #1).
BULK_TEST_MIN = 6          # >= this many new tests to consider "bulk"
BULK_SHAPE_RATIO_MIN = 0.5  # AND >= this fraction shape_only → advisory risk
# shape_ratio at/above which validate emits NLJ on accept (test-level)
SHAPE_RATIO_NLJ = 0.5


@dataclass(frozen=True)
class Counts:
    test_funcs_added: int
    shape_only_tests: int
    mixed_tests: int
    behavioral_only_tests: int
    shape_assertions: int
    behavioral_assertions: int

    @property
    def shape_ratio(self) -> float:
        """test-level: shape_only / total_new_tests (no unit mismatch, per G1)."""
        return round(self.shape_only_tests / self.test_funcs_added, 4) if self.test_funcs_added else 0.0


def compute_counts(test_files: list[FileDiff]) -> Counts:
    regions = [r for f in test_files for r in group_added_tests(f)]
    shape_only = sum(1 for r in regions if r.kind == "shape_only")
    mixed = sum(1 for r in regions if r.kind == "mixed")
    beh_only = sum(1 for r in regions if r.kind == "behavioral_only")
    shape_asserts = sum(len(r.shape_lines) for r in regions)
    beh_asserts = sum(r.behavioral_count for r in regions)
    return Counts(
        test_funcs_added=len(regions),
        shape_only_tests=shape_only,
        mixed_tests=mixed,
        behavioral_only_tests=beh_only,
        shape_assertions=shape_asserts,
        behavioral_assertions=beh_asserts,
    )


def bulk_shape_test_risk(counts: Counts) -> bool:
    """Advisory only — NOT an anti-horizontal verdict (single diff can't prove
    temporal ordering, per double-audit C1). Never feeds NLJ in single-diff mode."""
    return (counts.test_funcs_added >= BULK_TEST_MIN
            and counts.shape_ratio >= BULK_SHAPE_RATIO_MIN)


# Focus prompts for a caller-run /audit when the audit policy routes there.
FOCUS_PROMPTS: dict[str, str] = {
    "shape_vs_behavioral": (
        "For each flagged test, decide if it asserts on BEHAVIOR (output values / "
        "side effects / errors given specific inputs) or only on SHAPE (types / "
        "structure / key presence). A test that never asserts a concrete value is "
        "a smell — state the behavioral assertion it SHOULD make. Ignore type "
        "guards that precede a value assertion (those are 'mixed' and fine)."
    ),
    "suppression_justification": (
        "For each deleted assertion or added skip/disable, decide if it is "
        "JUSTIFIED (test moved / obsolete / behavior removed) or a QUALITY "
        "REGRESSION (skipping a failing test to green CI; silently dropping a "
        "behavioral assertion). If justified, cite where the behavior is still "
        "covered."
    ),
    "anti_horizontal": (
        "Only meaningful with --commits-numstat. Given per-commit test/impl interleaving, "
        "decide if tests were written VERTICALLY (test→impl→test tracer-bullet) or "
        "HORIZONTALLY (all tests batched, then all impl — yields tests of imagined "
        "behavior). See docs/TESTING_METHODOLOGY.md."
    ),
}


# ===== Anti-horizontal — commit-history mode (per double-audit C1) =============


@dataclass(frozen=True)
class CommitStat:
    sha: str
    test_funcs_added: int
    impl_lines_added: int


def detect_anti_horizontal(commits: list[CommitStat]) -> dict:
    """Temporal signal: tests batched BEFORE impl = horizontal. Requires a
    test-only commit that precedes an impl-only commit (oldest→newest order).
    Interleaved (commits that add both) → not fired. This is the only place a
    real anti-horizontal verdict can come from (single diff cannot — C1)."""
    test_only = [i for i, c in enumerate(commits)
                 if c.test_funcs_added > 0 and c.impl_lines_added == 0]
    impl_only = [i for i, c in enumerate(commits)
                 if c.impl_lines_added > 0 and c.test_funcs_added == 0]
    fired = bool(test_only and impl_only and min(test_only) < max(impl_only))
    if fired:
        reason = (f"{len(test_only)} test-only commit(s) precede "
                  f"{len(impl_only)} impl-only commit(s) — tests batched before impl")
    else:
        reason = "tests and impl interleaved across commits (vertical)"
    return {"fired": fired, "reason": reason, "test_only_commits": len(test_only),
            "impl_only_commits": len(impl_only)}


def parse_commit_numstat(text: str) -> list[CommitStat]:
    """Parse `git log --numstat --format=COMMIT:%H <range>` output (oldest first
    if caller passes --reverse). Commits delimited by `COMMIT:<sha>` lines;
    numstat rows are `<added>\\t<deleted>\\t<path>`."""
    commits: list[CommitStat] = []
    sha = ""
    tfa = 0
    ila = 0

    def flush() -> None:
        nonlocal sha, tfa, ila
        if sha:
            commits.append(CommitStat(sha=sha, test_funcs_added=tfa, impl_lines_added=ila))
        sha = ""
        tfa = ila = 0

    for raw in text.splitlines():
        if raw.startswith("COMMIT:"):
            flush()
            sha = raw[len("COMMIT:"):].strip()[:12]
            continue
        parts = raw.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            added = int(parts[0])
            path = parts[2]
            if is_test_file(path):
                # numstat gives line counts, not function counts; added>0 in a
                # test file marks the commit as test-bearing (sufficient for the
                # test-only-vs-impl-only temporal split).
                tfa += added
            elif is_source_file(path):
                ila += added
    flush()
    return commits


# ===== Analyze (build ledger) ==================================================


@dataclass(frozen=True)
class AnalyzeResult:
    candidates: tuple[Candidate, ...]
    counts: Counts
    bulk_shape_risk: bool
    anti_horizontal: dict
    source_changed: int
    test_changed: int
    preliminary_nlj: bool
    preliminary_nlj_reasons: tuple[str, ...]


def analyze(files: list[FileDiff], commits: list[CommitStat] | None = None) -> AnalyzeResult:
    test_files = [f for f in files if is_test_file(f.path)]
    source_files = [f for f in files if is_source_file(f.path)]
    cands: list[Candidate] = []
    cands += detect_coverage_gaps(files)
    cands += detect_shape_candidates(test_files)
    cands += detect_suppression_candidates(test_files)
    cands += detect_flaky_candidates(test_files)
    counts = compute_counts(test_files)
    ah = detect_anti_horizontal(commits) if commits else {
        "fired": False, "reason": "single-diff mode (no commit history)",
        "test_only_commits": 0, "impl_only_commits": 0}

    # Preliminary NLJ (advisory at analyze time): any HIGH-conf candidate, or a
    # fired anti_horizontal (commits mode). bulk_shape_risk does NOT count (C1).
    reasons: list[str] = []
    high = [c for c in cands if c.confidence == "high"]
    if high:
        reasons.append(f"{len(high)} high-confidence candidate(s) need LLM disposition")
    if ah["fired"]:
        reasons.append("anti_horizontal fired (commit-history mode)")
    return AnalyzeResult(
        candidates=tuple(cands), counts=counts, bulk_shape_risk=bulk_shape_test_risk(counts),
        anti_horizontal=ah, source_changed=len(source_files), test_changed=len(test_files),
        preliminary_nlj=bool(reasons), preliminary_nlj_reasons=tuple(reasons))
