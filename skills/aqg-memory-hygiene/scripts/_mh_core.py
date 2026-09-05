#!/usr/bin/env python3
"""aqg-memory-hygiene core — pure memory-node parsing + schema / staleness rules.

Imported by `aqg_memory_hygiene.py` (which owns the CLI, rendering, and the
exit-code contract). Split out to keep each file under the 800-line house limit.

Signal-only per ADR 2026-06-04-aqg-memory-hygiene-skill-a1.md §4.2: these helpers
only READ + classify. They never write a memory file, never call audit-mcp, and
never touch the network. stdlib + PyYAML (a DECLARED AQG runtime dependency,
already `pip install`-ed in CI; not stdlib — wording per ADR §6 acceptance #4).
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass
from pathlib import Path

# ===== schema contract (ADR §4.3, batch-1 finalized — this repo's single authority) =

TYPE_ENUM = ("user", "feedback", "project", "reference")
STATUS_ENUM = ("active", "superseded")
VOLATILITY_ENUM = ("durable", "volatile")
REQUIRED_META_FIELDS = ("type", "status", "volatility", "last_verified")
SUPERSEDED_FIELDS = ("superseded_by", "superseded_reason", "superseded_date")
FORBIDDEN_META_FIELDS = ("node_type",)  # redundant residue (batch-1 self-contradiction)

# Index / non-node files that live in a memory dir but carry NO node frontmatter.
# Excluded BEFORE the strict gate, else it fatal-fails on every standard dir
# (round2 convergent finding — a memory dir always has MEMORY.md).
INDEX_FILENAMES = frozenset({"memory.md", "readme.md"})

DEFAULT_STALE_DAYS = 90
ENV_STALE_DAYS = "AQG_MEMORY_STALE_DAYS"

# A date stamped as a local "today" can be up to +1 calendar day ahead of the
# UTC date (timezones span UTC-12..UTC+14) — and a harness/host clock can differ
# from the machine clock by ~a day. So "future" means strictly BEYOND today + 1d;
# within the grace is legitimate skew, not a typo. Beyond it (2099, next month)
# is a genuine clock-error / mis-fill and is rejected. (Local-verification on the
# real corpus surfaced a 2026-06-04-vs-UTC-2026-06-03 false-positive without this.)
FUTURE_GRACE_DAYS = 1

PYYAML_MISSING_NOTE = (
    "PyYAML not installed; `pip install pyyaml>=6.0` (a DECLARED AQG runtime "
    "dependency, not stdlib) is required to parse memory frontmatter"
)

_MISSING = object()  # sentinel: a key absent vs present-but-None


# ===== frontmatter parsing =====================================================


@dataclass(frozen=True)
class MemoryNode:
    """One parsed memory-node `.md`. parse_error set => frontmatter unusable."""

    path: Path
    stem: str  # filename without the .md suffix (filesystem identity)
    frontmatter: dict | None  # top-level YAML mapping, or None on parse failure
    metadata: dict | None  # frontmatter["metadata"] iff it is a mapping
    name: str | None  # frontmatter["name"] iff it is a str
    parse_error: str | None = None


def extract_frontmatter(text: str) -> tuple[str | None, str | None]:
    """Return (frontmatter_block, error). The block is the YAML text between a
    leading `---` line and the next `---` line. error is set when the leading
    delimiter is absent or the block is unterminated."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "no leading `---` frontmatter delimiter"
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), None
    return None, "unterminated frontmatter (missing closing `---`)"


def _safe_yaml_load(block: str) -> tuple[object, str | None]:
    """yaml.safe_load with graceful ImportError + parse-error handling."""
    try:
        import yaml
    except ImportError:
        return None, PYYAML_MISSING_NOTE
    try:
        return yaml.safe_load(block), None
    except (yaml.YAMLError, ValueError) as exc:
        # Malformed frontmatter is a violation, not a crash. ValueError is included
        # because PyYAML's timestamp constructor raises it (NOT a YAMLError) on an
        # invalid-but-timestamp-shaped scalar like `2026-02-30` / `2026-13-01`
        # (audit round2 gpt-f1) — without this, such a node would escape to the
        # CLI top-level handler as exit 70, breaking staleness's always-exit-0 and
        # validate's report-don't-crash contracts.
        first = str(exc).splitlines()[0] if str(exc) else "parse error"
        return None, f"frontmatter parse error: {first[:200]}"


def parse_node(path: Path) -> MemoryNode:
    """Read + parse one memory-node file. Never raises — any failure is captured
    in parse_error so the caller reports a problem instead of crashing."""
    stem = path.name[:-3] if path.name.endswith(".md") else path.name
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return MemoryNode(path, stem, None, None, None, parse_error=f"read error: {exc}")
    block, ferr = extract_frontmatter(text)
    if ferr is not None:
        return MemoryNode(path, stem, None, None, None, parse_error=ferr)
    parsed, yerr = _safe_yaml_load(block or "")
    if yerr is not None:
        return MemoryNode(path, stem, None, None, None, parse_error=yerr)
    if not isinstance(parsed, dict):
        return MemoryNode(path, stem, None, None, None, parse_error="frontmatter is not a mapping")
    meta = parsed.get("metadata")
    meta = meta if isinstance(meta, dict) else None
    name = parsed.get("name") if isinstance(parsed.get("name"), str) else None
    return MemoryNode(path, stem, parsed, meta, name)


def is_index_file(p: Path) -> bool:
    return p.name.lower() in INDEX_FILENAMES


def iter_node_files(memory_dir: Path) -> list[Path]:
    """Memory-node `.md` files in a dir, index/non-node files excluded.

    `glob("*.md")` matches only names ending exactly in `.md`, so sidecar backups
    like `x.md.backup-YYYY-MM-DD` are excluded naturally."""
    return sorted(p for p in memory_dir.glob("*.md") if not is_index_file(p))


def index_files(memory_dir: Path) -> list[Path]:
    return sorted(p for p in memory_dir.glob("*.md") if is_index_file(p))


# ===== date coercion ===========================================================

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_future(d: _dt.date, today: _dt.date) -> bool:
    """True iff d is beyond today + FUTURE_GRACE_DAYS (genuine future, not skew)."""
    return d > today + _dt.timedelta(days=FUTURE_GRACE_DAYS)


def coerce_date(value) -> _dt.date | None:
    """Coerce a frontmatter date value to a date. PyYAML auto-parses an unquoted
    `2026-05-01` to a `datetime.date` already; a quoted / malformed value arrives
    as a str. datetime is checked first (it subclasses date) so it is narrowed to
    a date for safe comparison against `today`."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str) and _ISO_DATE_RE.fullmatch(value.strip()):
        try:
            return _dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def coerce_schema_date(value) -> _dt.date | None:
    """Strict variant for the VALIDATE gate (audit gpt-f1): the schema requires a
    date-only `YYYY-MM-DD`, so a YAML timestamp scalar (PyYAML parses
    `2026-01-01T12:00:00` to a `datetime.datetime`) is REJECTED, not truncated.
    A plain `datetime.date` or an exact ISO-date string is accepted."""
    if isinstance(value, _dt.datetime):
        return None  # a full timestamp is not the required date-only form
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str) and _ISO_DATE_RE.fullmatch(value.strip()):
        try:
            return _dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


# ===== superseded_by resolution ===============================================


@dataclass(frozen=True)
class SuperResolution:
    kind: str  # "ok" | "warn" | "reject"
    detail: str = ""


@dataclass(frozen=True)
class IndexEntry:
    """A resolvable sibling identity. status = the node's metadata.status
    (None for index/non-node files or unparseable nodes)."""

    path: Path
    is_index: bool
    status: str | None


def _stem(p: Path) -> str:
    return p.name[:-3] if p.name.endswith(".md") else p.name


def _node_status(node: MemoryNode) -> str | None:
    return node.metadata.get("status") if node.metadata is not None else None


def build_dir_index(
    nodes: list[MemoryNode], idx_files: list[Path]
) -> dict[str, IndexEntry]:
    """Map every resolvable identity (filename stem + `name:` field) to an
    IndexEntry. Node stems take precedence over index stems and over name-field
    aliases on collision; each entry carries the target node's status so a
    superseded_by slug can be required to point to an ACTIVE node (ADR §0 ②)."""
    index: dict[str, IndexEntry] = {}
    for p in idx_files:
        index.setdefault(_stem(p), IndexEntry(p, True, None))
    for node in nodes:
        index[node.stem] = IndexEntry(node.path, False, _node_status(node))
    for node in nodes:
        if node.name:
            index.setdefault(node.name, IndexEntry(node.path, False, _node_status(node)))
    return index


def classify_slug(
    slug: str, self_node: MemoryNode, dir_index: dict[str, IndexEntry]
) -> tuple[IndexEntry | None, str]:
    """Resolve a bare slug → (entry, kind) where kind ∈ self|index|node|dangling.

    Consults dir_index FIRST (audit gpt-f3): a node whose `name:` collides with
    another node's filename stem must resolve to the stem-owner, not be misread as
    a self-reference. The post-index identity check only catches a self-slug that
    somehow is not in the index (defensive)."""
    entry = dir_index.get(slug)
    if entry is not None:
        if entry.path == self_node.path:
            return entry, "self"
        return entry, ("index" if entry.is_index else "node")
    if slug == self_node.stem or (self_node.name and slug == self_node.name):
        return None, "self"
    return None, "dangling"


def resolve_superseded_by(
    value, self_node: MemoryNode, dir_index: dict[str, IndexEntry]
) -> SuperResolution:
    """superseded_by may be: null (pure retirement, ok) | "repo:<path>" (warn-only
    dangling, reject empty / traversal / absolute — §9 Decision 3) | <slug> (must
    resolve to ANOTHER ACTIVE sibling node; self / index / dangling / non-active →
    reject, per ADR §0 ② "a slug must point to another active node")."""
    if value is None:
        return SuperResolution("ok")
    if not isinstance(value, str) or not value.strip():
        return SuperResolution("reject", f"{value!r} must be a slug, \"repo:<path>\", or null")
    value = value.strip()
    if value.startswith("repo:"):
        rel = value[len("repo:"):].strip()
        if not rel:
            return SuperResolution("reject", "repo: pointer has an empty path")
        # Normalize backslashes so a Windows-style traversal is caught on POSIX too
        # (audit gpt-f2). Reject ANY `..` path segment — including an internal one
        # that would normalize back inside the dir (`foo/../bar`) — plus absolute /
        # drive-letter forms (audit gpt-f3: the contract rejects EVERY `..` segment,
        # not only net escapes; a `.` no-op segment stays allowed as warn).
        probe = rel.replace("\\", "/")
        if ".." in probe.split("/"):
            return SuperResolution("reject", f"repo: path contains a `..` traversal segment: {rel!r}")
        if probe.startswith("/") or re.match(r"^[A-Za-z]:", probe):
            return SuperResolution("reject", f"repo: absolute path not allowed: {rel!r}")
        return SuperResolution(
            "warn", f"repo:{rel} not verified across repo boundary (dangling = warn-only)"
        )
    entry, kind = classify_slug(value, self_node, dir_index)
    if kind == "node":
        if entry is not None and entry.status != "active":
            return SuperResolution(
                "reject",
                f"slug {value!r} points to a non-active node (status={entry.status!r}); "
                f"supersede must point to another ACTIVE node",
            )
        return SuperResolution("ok")
    if kind == "self":
        return SuperResolution("reject", f"slug {value!r} points to self (a node cannot supersede itself)")
    if kind == "index":
        return SuperResolution("reject", f"slug {value!r} points to an index/non-node file")
    return SuperResolution("reject", f"slug {value!r} is dangling (no sibling memory-node resolves to it)")


# ===== validate ================================================================


@dataclass(frozen=True)
class NodeResult:
    path: Path
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations


def _contains_key_deep(obj: object, key: str, _seen: set[int] | None = None) -> bool:
    """True iff `key` appears as a mapping key at ANY depth (mappings + sequences
    walked). Cycle-safe: PyYAML anchors/aliases can build a recursive structure
    (`metadata: &m {self: *m}`), so visited containers are tracked by id to avoid a
    RecursionError that would crash validate to exit 70 (audit commit-gate f1). The
    key check runs before the seen-guard so a node_type reachable before a back-edge
    is still found. Enforces 'no node_type anywhere' (audit gpt-f2 / gemini-f2)."""
    if _seen is None:
        _seen = set()
    if isinstance(obj, dict):
        if key in obj:
            return True
        if id(obj) in _seen:
            return False
        _seen.add(id(obj))
        return any(_contains_key_deep(v, key, _seen) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        if id(obj) in _seen:
            return False
        _seen.add(id(obj))
        return any(_contains_key_deep(item, key, _seen) for item in obj)
    return False


def validate_node(
    node: MemoryNode, today: _dt.date, dir_index: dict[str, IndexEntry]
) -> NodeResult:
    """Apply the ADR §4.3 strict-gate rules to one parsed node."""
    v: list[str] = []
    w: list[str] = []
    rel = node.path.name

    # rule ①: frontmatter present + parseable
    if node.parse_error is not None:
        return NodeResult(node.path, (f"{rel}: {node.parse_error}",), ())
    fm = node.frontmatter or {}

    # rule ⑤: no redundant node_type at ANY depth (audit gpt-f2 / gemini-f2: the
    # old check only saw the top level + the immediate metadata mapping; `fm`
    # already contains metadata, so one recursive walk covers every nesting level).
    for forbidden in FORBIDDEN_META_FIELDS:
        if _contains_key_deep(fm, forbidden):
            v.append(f"{rel}: redundant `{forbidden}` field present (remove it)")

    # rule ②: metadata must be a nested mapping
    if node.metadata is None:
        raw = fm.get("metadata", _MISSING)
        if raw is _MISSING:
            if any(k in fm for k in REQUIRED_META_FIELDS):
                v.append(f"{rel}: flat frontmatter — required keys must be nested under a `metadata:` mapping")
            else:
                v.append(f"{rel}: frontmatter missing a `metadata:` mapping")
        else:
            v.append(f"{rel}: `metadata:` must be a mapping, got {type(raw).__name__}")
        return NodeResult(node.path, tuple(v), tuple(w))
    meta = node.metadata

    # rule ③: required fields present
    for fld in REQUIRED_META_FIELDS:
        if fld not in meta:
            v.append(f"{rel}: metadata missing required `{fld}`")

    # rule ④: enums
    if "type" in meta and meta["type"] not in TYPE_ENUM:
        v.append(f"{rel}: metadata.type {meta['type']!r} not in {list(TYPE_ENUM)}")
    status = meta.get("status")
    if "status" in meta and status not in STATUS_ENUM:
        v.append(f"{rel}: metadata.status {status!r} not in {list(STATUS_ENUM)}")
    if "volatility" in meta and meta["volatility"] not in VOLATILITY_ENUM:
        v.append(f"{rel}: metadata.volatility {meta['volatility']!r} not in {list(VOLATILITY_ENUM)}")

    # rule ⑥: last_verified is a valid date-only ISO date AND not in the future
    if "last_verified" in meta:
        d = coerce_schema_date(meta["last_verified"])
        if d is None:
            v.append(f"{rel}: metadata.last_verified {meta['last_verified']!r} is not a YYYY-MM-DD date")
        elif is_future(d, today):
            v.append(f"{rel}: metadata.last_verified {d.isoformat()} is in the future (beyond today {today.isoformat()} + skew grace)")

    # rule ⑦ + ⑧: superseded integrity
    present_sup = [f for f in SUPERSEDED_FIELDS if f in meta]
    if status == "superseded":
        missing = [f for f in SUPERSEDED_FIELDS if f not in meta]
        if missing:
            v.append(f"{rel}: status superseded but missing {missing} (all three superseded_* required)")
        else:
            res = resolve_superseded_by(meta["superseded_by"], node, dir_index)
            if res.kind == "reject":
                v.append(f"{rel}: superseded_by {res.detail}")
            elif res.kind == "warn":
                w.append(f"{rel}: superseded_by {res.detail}")
            sd = coerce_schema_date(meta["superseded_date"])
            if sd is None:
                v.append(f"{rel}: metadata.superseded_date {meta['superseded_date']!r} is not a YYYY-MM-DD date")
            elif is_future(sd, today):
                v.append(f"{rel}: metadata.superseded_date {sd.isoformat()} is in the future (beyond today + skew grace)")
    elif status == "active" and present_sup:
        v.append(
            f"{rel}: status active but carries superseded field(s) {sorted(present_sup)} "
            f"— only status: superseded may"
        )

    return NodeResult(node.path, tuple(v), tuple(w))


@dataclass(frozen=True)
class ValidateReport:
    memory_dir: Path
    dir_exists: bool
    results: tuple[NodeResult, ...]

    @property
    def node_count(self) -> int:
        return len(self.results)

    @property
    def violation_results(self) -> list[NodeResult]:
        return [r for r in self.results if r.violations]

    @property
    def warning_results(self) -> list[NodeResult]:
        return [r for r in self.results if r.warnings]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)


def validate_dir(memory_dir: Path, today: _dt.date) -> ValidateReport:
    if not memory_dir.exists() or not memory_dir.is_dir():
        return ValidateReport(memory_dir, dir_exists=False, results=())
    nodes = [parse_node(p) for p in iter_node_files(memory_dir)]
    dir_index = build_dir_index(nodes, index_files(memory_dir))
    results = tuple(validate_node(n, today, dir_index) for n in nodes)
    return ValidateReport(memory_dir, dir_exists=True, results=results)


# ===== staleness ===============================================================


@dataclass(frozen=True)
class StalenessItem:
    path: Path
    age_days: int | None
    reason: str


@dataclass(frozen=True)
class StalenessReport:
    memory_dir: Path
    dir_exists: bool
    threshold: int
    scanned: int
    stale: tuple[StalenessItem, ...]
    skipped: tuple[tuple[Path, str], ...]

    @property
    def fresh_count(self) -> int:
        """Nodes assessed and healthy — durable, or volatile within the horizon —
        i.e. neither stale nor skipped. Derived so the report is self-consistent:
        scanned == stale + skipped + fresh (audit gemini-f1: durable nodes are
        `fresh`, never `skipped`; the old label wrongly lumped them into skipped)."""
        return self.scanned - len(self.stale) - len(self.skipped)


def assess_staleness(
    node: MemoryNode, today: _dt.date, threshold: int
) -> tuple[str, str, int | None]:
    """Return (verdict, reason, age_days). verdict ∈ {stale, fresh, skip}.

    Graceful: unparseable / no-metadata / missing-last_verified → skip + note
    (staleness can run on a dirty corpus that never passed validate). superseded
    → skip (already retired; its date hygiene is validate's job, not staleness').
    durable → fresh (not gated). future-dated (negative age) → stale (defensive)."""
    if node.parse_error is not None or node.metadata is None:
        return "skip", f"unparseable / no metadata ({node.parse_error or 'no metadata mapping'})", None
    meta = node.metadata
    if meta.get("status") == "superseded":
        return "skip", "superseded (already retired)", None
    if meta.get("volatility") != "volatile":
        return "fresh", f"volatility={meta.get('volatility')!r} (durable not gated)", None
    d = coerce_date(meta.get("last_verified"))
    if d is None:
        return "skip", "missing / malformed last_verified", None
    age = (today - d).days
    if age < -FUTURE_GRACE_DAYS:
        return "stale", f"future-dated last_verified ({d.isoformat()}, age {age}d, beyond skew grace) — defensive list", age
    if age > threshold:
        return "stale", f"last_verified {d.isoformat()} is {age}d old (> {threshold}d)", age
    return "fresh", f"last_verified {d.isoformat()} is {age}d old (<= {threshold}d)", age


def staleness_dir(memory_dir: Path, today: _dt.date, threshold: int) -> StalenessReport:
    if not memory_dir.exists() or not memory_dir.is_dir():
        return StalenessReport(memory_dir, False, threshold, 0, (), ())
    stale: list[StalenessItem] = []
    skipped: list[tuple[Path, str]] = []
    files = iter_node_files(memory_dir)
    for p in files:
        node = parse_node(p)
        verdict, reason, age = assess_staleness(node, today, threshold)
        if verdict == "stale":
            stale.append(StalenessItem(p, age, reason))
        elif verdict == "skip":
            skipped.append((p, reason))
        # verdict == "fresh" (durable / volatile-within-horizon) is counted via
        # scanned - stale - skipped, not listed (the report surfaces only what
        # needs attention or could not be assessed).
    return StalenessReport(memory_dir, True, threshold, len(files), tuple(stale), tuple(skipped))


# ===== shared helpers (CLI consumes these) =====================================


def resolve_threshold(cli_days: int | None) -> int:
    """--days (explicit) > AQG_MEMORY_STALE_DAYS env > DEFAULT_STALE_DAYS.
    A non-integer / negative env value falls through to the default."""
    if cli_days is not None:
        return cli_days
    raw = os.environ.get(ENV_STALE_DAYS)
    if raw:
        try:
            val = int(raw)
        except ValueError:
            return DEFAULT_STALE_DAYS
        if val >= 0:
            return val
    return DEFAULT_STALE_DAYS


def discover_memory_dir() -> Path:
    """Best-effort auto-discovery of the current project's memory dir, following
    the Claude Code convention `~/.claude/projects/<projhash>/memory` where
    projhash = cwd with `/` and `.` replaced by `-` (same normpath hardening as
    the #162 write-guard hook). Missing dir → the caller emits a note + exit 0."""
    cwd = os.path.normpath(str(Path.cwd().resolve()))
    projhash = re.sub(r"[/.]", "-", cwd)
    return Path.home() / ".claude" / "projects" / projhash / "memory"
