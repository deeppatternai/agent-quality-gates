#!/usr/bin/env python3
"""Outward-narrative overclaim gate — scan copy for banned causal quality claims.

WS-4 item 4: the mechanical enforcement of the D4×D5 constraint (plan R1-Cluster I).
Until an empirical benchmark exists, AQG's outward copy may describe MECHANISMS but
must NOT assert CAUSAL quality outcomes ("fewer bugs", "higher quality"). This is
the reusable scanner WS-0/G0-⑥ wires into the publish pipeline as a required check;
run standalone it is a fail-closed gate (exit 1 on any hit).

Scope (plan §4 G0-⑥ + R2-4):
- Scans OUTWARD-FACING copy only.
- ALWAYS excludes append-only history — LOG.md, CHANGELOG.md — so a banned word
  that appears once in the historical record does not deadlock the gate forever.

Usage:
    scan_overclaim.py [paths...]     # scan the given files (dirs are NOT walked)
    scan_overclaim.py                # scan the default outward-doc set under cwd
    scan_overclaim.py --tree <dir>   # walk a whole tree: blocklist code/data/binary,
                                     # refuse NESTED append-only history, scan the rest
Exit (path/default mode): 0 = clean, 1 = a banned causal claim found, 2 = bad usage.
Exit (--tree mode): 0 = clean · 2 = usage (not a dir) · 3 = STRUCTURAL fail-closed
      (nested-history refusal / no scannable doc / walk|IO|oversize error / any
      unexpected scanner error) · 4 = banned causal claim (the ONLY publish-
      OVERRIDABLE code). banned is 4 (not 1) so a crash — Python's native exit 1 —
      stays in the non-overridable branch (see publish_public.sh).

--tree is the whole-tree walk publish_public.sh delegates to (WS-0 ①): it replaces
the prior bash find/xargs/pure-bash refusal walk with one os.walk pass, removing the
shell edges (SIGPIPE / dash-basename / trailing-slash / ARG_MAX) 4 audit rounds found.
It fails CLOSED on a partial walk (os.walk onerror handler), unlike a default os.walk.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Import the wordlist (single source of truth). Works whether run as a script or
# imported: ensure this file's dir is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _overclaim_terms import scan_text  # noqa: E402

# Append-only history — never scanned, even when passed explicitly (plan R2-4:
# a banned word in the historical record must not deadlock the gate). Matched
# case-insensitively so 'changelog.md' / 'Changelog.md' are also excluded.
EXCLUDE_BASENAMES = {"log.md", "changelog.md", "changes.md", "history.md", "news.md"}

# --tree blocklist: code/config/data/binary suffixes NOT scanned for outward
# narrative (a banned phrase there is a wordlist definition, a docstring example,
# or a benchmark hypothesis — not copy). Ported verbatim from the `find ! -iname`
# list publish_public.sh used before this logic moved into --tree (WS-0 ①). A
# blocklist (not allowlist): an unknown/extensionless outward file (README,
# LICENSE, NOTICE, *.html, *.adoc) is still scanned, so it fails closed (audit
# 7324228b: an inclusion allowlist silently skipped those). Suffixes are lower-cased.
BLOCKLIST_SUFFIXES = frozenset({
    ".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".lock",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".woff", ".woff2",
    ".ttf", ".zip", ".tar", ".gz", ".so", ".dylib", ".pyc",
})
# Matched with str.endswith (a tuple), not Path.suffix, for exact `find -iname
# '*.py'` parity: a file named EXACTLY '.py' has an empty Path.suffix but IS
# excluded by find (audit 60b40c39 f3 / Voice 2). Names are casefolded before the
# check to mirror -iname case-insensitivity.
_BLOCKLIST_SUFFIX_TUPLE = tuple(sorted(BLOCKLIST_SUFFIXES))

# --tree per-file read cap: a non-blocklisted file larger than this is treated as a
# STRUCTURAL fail-closed condition (not silently skipped), so an oversized/binary
# outward file cannot exhaust memory via read_text (audit c88dc6ae f2 / Voice 4 —
# amplified once the .git prune was removed). 25 MiB is far above any prose doc.
MAX_SCAN_BYTES = 25 * 1024 * 1024

# Default outward-facing copy scanned when no paths are given (root-level only —
# G0 passes an explicit, curated list; this is the convenient standalone default).
DEFAULT_DOCS = [
    "README.md", "README.zh-CN.md",
    "AI_SETUP.md", "AI_SETUP.zh-CN.md",
    "CONTRIBUTING.md", "CONTRIBUTING.zh-CN.md",
]


def _targets(paths: "list[str]", root: Path, warn: "list[str]") -> "list[Path]":
    """Resolve scan targets. Appends operator-facing notes to `warn` when an
    EXPLICITLY-passed path is skipped, so a typo/dir/excluded arg cannot masquerade
    as a clean scan."""
    explicit = bool(paths)
    candidates = [Path(p) for p in paths] if paths else [root / d for d in DEFAULT_DOCS]
    out: list[Path] = []
    for p in candidates:
        if p.name.casefold() in EXCLUDE_BASENAMES:
            if explicit:
                warn.append(f"{p}: excluded as append-only history — NOT scanned (R2-4)")
            continue
        if not p.exists():
            if explicit:
                warn.append(f"{p}: does not exist — NOT scanned")
            continue
        if not p.is_file():
            if explicit:
                warn.append(f"{p}: is a directory — NOT scanned (pass files, not dirs)")
            continue
        out.append(p)
    return out


def scan_files(
    paths: "list[str]", root: "Path | None" = None, warn: "list[str] | None" = None,
) -> "list[tuple[str, str, int, str, str]]":
    """Scan files for banned causal claims. Returns hits as
    (relpath, term_id, line_no, matched_text, line_text). Skipped-target notes are
    appended to `warn` (an operator-facing list) when provided."""
    root = root or Path.cwd()
    warn = warn if warn is not None else []
    hits: list[tuple[str, str, int, str, str]] = []
    for path in _targets(paths, root, warn):
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        for term_id, line_no, matched, line_text in scan_text(text):
            hits.append((rel, term_id, line_no, matched, line_text))
    return hits


def scan_tree(
    root: Path,
) -> "tuple[list[tuple[str, str, int, str, str]], list[str], int, list[str]]":
    """Walk `root` and enforce the whole-tree overclaim contract (WS-0 ①, replaces
    the bash find/xargs/pure-bash walk in publish_public.sh). Returns
    (hits, refusals, enumerated, structural_errors):

      - BLOCKLIST_SUFFIXES-suffixed files (code/data/binary) are skipped.
      - non-regular / symlink entries are skipped (`find -type f` parity: regular
        files only — no symlinks, FIFOs, sockets, devices).
      - `enumerated` counts every non-blocklisted regular file (INCLUDING an
        exempt/refused one) — the parity of the bash `[ -s "$_LIST" ]` emptiness
        test, which listed root history too (audit 60b40c39 f2 / Voice 6).
      - a file whose basename is in EXCLUDE_BASENAMES: at the ROOT it is exempt
        (not scanned — append-only history must not deadlock the gate); NESTED it
        is REFUSED because the scanner would otherwise basename-exclude it, letting
        a nested CHANGELOG bypass the gate.
      - `structural_errors` collects any directory-enumeration or per-file IO error.
        os.walk defaults to onerror=None (SILENTLY drops an unreadable subtree — a
        fail-OPEN moat leak, audit 60b40c39 f1 / 7 of 7 voices). We supply onerror
        so a partial walk fails CLOSED, restoring the retired `find … || _die 5`.
      - every other file is scanned; hits are (relpath, term_id, line, matched, line).

    The CALLER fails closed (non-overridable) when `structural_errors` OR `refusals`
    is non-empty, OR `enumerated == 0`; only `hits` (a real banned claim) is the
    Owner-overridable condition.
    """
    hits: list[tuple[str, str, int, str, str]] = []
    refusals: list[str] = []
    structural_errors: list[str] = []
    enumerated = 0

    def _on_walk_error(err: OSError) -> None:
        # fail CLOSED: a directory we cannot enumerate could hide a banned doc.
        structural_errors.append(f"cannot enumerate {getattr(err, 'filename', '?')}: {err}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):
        dirnames.sort()  # deterministic traversal; do NOT prune (a nested '.git'
        #                  must still be scanned — pruning it is a blind spot,
        #                  audit 60b40c39 f2 / Voice 3, and the real publish tree
        #                  has no .git at scan time anyway).
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                if p.is_symlink() or not p.is_file():
                    continue  # find -type f: regular files only
            except OSError as e:
                structural_errors.append(f"cannot stat {p}: {e}")
                continue
            if name.casefold().endswith(_BLOCKLIST_SUFFIX_TUPLE):
                continue
            rel = p.relative_to(root)
            enumerated += 1
            if name.casefold() in EXCLUDE_BASENAMES:
                if len(rel.parts) > 1:  # nested: scanner would silently exclude it
                    refusals.append(
                        f"nested append-only-named file would bypass the basename "
                        f"exclusion: {rel}"
                    )
                continue  # root history is exempt from scanning (still enumerated)
            try:
                if p.stat().st_size > MAX_SCAN_BYTES:
                    structural_errors.append(
                        f"exceeds {MAX_SCAN_BYTES}-byte scan cap (fail-closed, not "
                        f"skipped): {rel}"
                    )
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                structural_errors.append(f"cannot read {rel}: {e}")
                continue
            for term_id, line_no, matched, line_text in scan_text(text):
                hits.append((str(rel), term_id, line_no, matched, line_text))
    return hits, refusals, enumerated, structural_errors


# --tree exit codes (kept distinct so publish can override ONLY a real banned
# claim, never a structural fail-closed condition — parity with the old bash where
# nested-refusal / empty / enumeration-error _die 5'd BEFORE the override branch).
# TREE_BANNED is deliberately NOT 1: Python exits 1 on ANY uncaught exception
# (crash / import error / MemoryError), so if banned==1 a crash would collide with
# the single overridable code and be silently overridable (audit c88dc6ae f1, 4/4
# voices). With banned==4 (a value the interpreter never emits on failure) every
# crash falls into publish's non-overridable branch.
TREE_CLEAN = 0
TREE_USAGE = 2         # not a directory / bad --tree arity
TREE_STRUCTURAL = 3    # nested-history refusal / empty tree / walk|IO|oversize error — NOT overridable
TREE_BANNED = 4        # a banned causal claim — the ONLY Owner-OVERRIDABLE code at the publish layer


def _main_tree(root: Path) -> int:
    """CLI for `--tree <dir>`. Exit: 0 clean · 2 usage · 3 structural fail-closed
    (nested-history refusal / no scannable doc / walk|IO|oversize error / any
    unexpected scanner error) · 4 banned claim (the ONLY publish-overridable code).
    The 4-vs-everything-else split is load-bearing: publish overrides only 4, so a
    crash (Python's native exit 1) stays non-overridable."""
    if not root.is_dir():
        print(f"[overclaim] --tree: not a directory: {root}", file=sys.stderr)
        return TREE_USAGE
    try:
        hits, refusals, enumerated, structural_errors = scan_tree(root)
    except Exception as e:  # aqg: top-level boundary
        # fail CLOSED and NON-overridable on any unexpected scanner error, so it can
        # never present as the overridable banned-claim code (audit c88dc6ae f1).
        print(f"[overclaim] FAIL — unexpected scanner error (fail-closed): {e}",
              file=sys.stderr)
        return TREE_STRUCTURAL
    if structural_errors:
        print("[overclaim] FAIL — tree enumeration/IO error (fail-closed; a subtree "
              "we cannot read could hide a banned claim):", file=sys.stderr)
        for e in structural_errors:
            print(f"  {e}", file=sys.stderr)
        return TREE_STRUCTURAL
    if refusals:
        print("[overclaim] FAIL — nested append-only-named file(s) refused "
              "(fail-closed; only ROOT history is exempt):", file=sys.stderr)
        for r in refusals:
            print(f"  {r}", file=sys.stderr)
        return TREE_STRUCTURAL
    if enumerated == 0:
        print("[overclaim] FAIL — no outward docs found to scan (fail-closed)",
              file=sys.stderr)
        return TREE_STRUCTURAL
    if not hits:
        print(f"[overclaim] clean — enumerated {enumerated} outward file(s), "
              "no banned causal quality claims found")
        return TREE_CLEAN
    print(f"[overclaim] FAIL — {len(hits)} banned causal claim(s) found "
          "(D4×D5: describe mechanisms, not causal quality outcomes):", file=sys.stderr)
    for rel, term_id, line_no, matched, line_text in hits:
        print(f"  {rel}:{line_no}: [{term_id}] {matched!r}  in: {line_text}", file=sys.stderr)
    return TREE_BANNED


def main(argv: "list[str] | None" = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    if args and args[0] == "--tree":
        if len(args) != 2:
            print("[overclaim] usage: scan_overclaim.py --tree <dir>", file=sys.stderr)
            return 2
        return _main_tree(Path(args[1]))
    warn: list[str] = []
    hits = scan_files(args, warn=warn)
    for w in warn:
        print(f"[overclaim] WARN: {w}", file=sys.stderr)
    if not hits:
        print("[overclaim] clean — no banned causal quality claims found")
        return 0
    print(f"[overclaim] FAIL — {len(hits)} banned causal claim(s) found "
          "(D4×D5: describe mechanisms, not causal quality outcomes):", file=sys.stderr)
    for rel, term_id, line_no, matched, line_text in hits:
        print(f"  {rel}:{line_no}: [{term_id}] {matched!r}  in: {line_text}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
