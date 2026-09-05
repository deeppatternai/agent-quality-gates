"""Behavior lock for the outward-narrative overclaim gate (WS-4 item 4).

The D4×D5 constraint: until an empirical benchmark exists, AQG's outward copy must
not assert causal quality outcomes ("fewer bugs", "higher quality"), only describe
mechanisms. This gate is the mechanical enforcement (plan R1-Cluster I). These
tests lock (a) the wordlist catches real causal claims, (b) it does NOT
false-positive on honest negations / mechanism language, (c) the scanner's
append-only-history exclusion (LOG.md, CHANGELOG — plan R2-4), and (d) the
currently-shipped README stays clean.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _overclaim_terms import scan_text  # noqa: E402


# ---- wordlist: catches real causal claims ---------------------------------


@pytest.mark.parametrize("bad", [
    "AQG delivers fewer bugs than a plain CLAUDE.md.",
    "Teams ship with fewer defects.",
    "It reduces bugs across the board.",
    "reduces the number of defects by a lot",
    "2x fewer bugs in our trials",
    "40% fewer bugs",
    "delivers higher quality code",
    "better code quality overall",
    "proven to reduce defects",
    "更少缺陷",
    "让代码更少 bug",
    "减少缺陷",
    "降低了缺陷",
    "质量更高",
    "更高的质量",
    "缺陷更少",
    "证明更少",
    "证明了更可靠",
])
def test_causal_claims_are_flagged(bad):
    hits = scan_text(bad)
    assert hits, f"should flag causal claim: {bad!r}"


# ---- wordlist: does NOT false-positive on honest / mechanism language ------


@pytest.mark.parametrize("ok", [
    # the actual shipped README line — an honest negation
    "Treat the hooks as nudges, not a guaranteed-delivery channel.",
    "Prefer battle-tested, proven libraries over hand-rolled code.",
    "AQG provides deterministic gates, an evidence ledger, and anti-forgetting.",
    "The engine is proven in production for years.",  # bare 'proven', not causal-quality
    "This guarantees the file is written atomically.",  # guarantee, not about quality
    "a higher timeout value",  # 'higher' but not quality
    "更高的超时时间",  # higher timeout, not quality
    "reduce the number of open PRs",  # reduce, but not bugs/defects
])
def test_honest_language_is_not_flagged(ok):
    hits = scan_text(ok)
    assert not hits, f"should NOT flag honest/mechanism language: {ok!r} -> {hits}"


# ---- scanner CLI: scope + exclusion + exit codes --------------------------


def _run(*args, cwd):
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "scan_overclaim.py"), *args],
        cwd=str(cwd), capture_output=True, text=True,
    )


def test_cli_clean_file_exits_zero(tmp_path):
    f = tmp_path / "clean.md"
    f.write_text("AQG provides deterministic gates and an evidence ledger.\n")
    r = _run(str(f), cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_cli_dirty_file_exits_one_and_reports(tmp_path):
    f = tmp_path / "dirty.md"
    f.write_text("AQG means fewer bugs for your team.\n")
    r = _run(str(f), cwd=tmp_path)
    assert r.returncode == 1
    out = r.stdout + r.stderr  # findings are reported on stderr (gate convention)
    assert "fewer bugs" in out and "dirty.md:1" in out and "[fewer_defects]" in out


def test_cli_excludes_append_only_history(tmp_path):
    """A banned term inside LOG.md / CHANGELOG must NOT trip the gate (plan R2-4)."""
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    log = tmp_path / "docs" / "decisions" / "LOG.md"
    log.write_text("2026-01-01 | owner | claimed fewer bugs once in history | ...\n")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## 0.1.0\n- early copy said higher quality\n")
    r = _run(str(log), str(changelog), cwd=tmp_path)
    assert r.returncode == 0, f"append-only history must be excluded: {r.stdout}"


def test_cli_scans_dir_default_and_skips_excluded(tmp_path):
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "README.md").write_text("deterministic gates only.\n")
    (tmp_path / "docs" / "decisions" / "LOG.md").write_text("said fewer bugs\n")
    r = _run(cwd=tmp_path)  # no args -> default scan of cwd
    assert r.returncode == 0, r.stdout


# ---- regression: the shipped README stays honest --------------------------


def test_shipped_readme_is_clean():
    for name in ("README.md", "README.zh-CN.md"):
        hits = scan_text((_REPO / name).read_text(encoding="utf-8"))
        assert not hits, f"{name} contains a banned causal claim: {hits}"


# ---- audit 6d0db06d hardening: negation / precision / coverage / exclusion ----


@pytest.mark.parametrize("negated", [
    "AQG is not proven to reduce bugs.",
    "These hooks do not reduce bugs.",
    "We never reduce defects automatically.",
    "This does not guarantee fewer bugs.",
    "The gate cannot deliver higher quality on its own.",
    "无法减少缺陷",
    "并未证明更可靠",
    "这不能带来更高质量",
    "没有更少的 bug",
])
def test_honest_negations_not_flagged(negated):
    """4/4 audit finding: honest negations MUST pass (they are the disclaimer copy
    a fail-closed gate must not block)."""
    hits = scan_text(negated)
    assert not hits, f"negation should NOT be flagged: {negated!r} -> {hits}"


@pytest.mark.parametrize("ops_ok", [
    "40% fewer open PRs to triage",
    "2x fewer merge conflicts",
    "50% fewer flaky reruns",
    "improved quality gates keep the bar visible",
    "a better quality feedback signal for the panel",
    "higher quality evidence in the ledger",
])
def test_non_quality_reductions_not_flagged(ops_ok):
    """num_fewer / higher_quality must not fire on legitimate non-outcome copy."""
    hits = scan_text(ops_ok)
    assert not hits, f"ops/mechanism copy should NOT be flagged: {ops_ok!r} -> {hits}"


@pytest.mark.parametrize("claim", [
    "AQG catches more bugs than a plain setup.",
    "ship faster with fewer errors",
    "it cuts defects dramatically",
    "eliminates defects before commit",
    "prevents bugs from landing",
    "30% less bugs in practice",
    "improves reliability across the board",
    "boosts code quality",
    "a 40% reduction in defects",
    "提升质量",
    "提高代码质量",
    "让系统更可靠",
    "更少的 BUG",
])
def test_expanded_coverage_flags_more_claims(claim):
    """Broadened wordlist catches common causal-claim rewordings."""
    hits = scan_text(claim)
    assert hits, f"should flag causal claim: {claim!r}"


def test_cli_exclusion_is_case_insensitive(tmp_path):
    cl = tmp_path / "Changelog.md"
    cl.write_text("early copy said fewer bugs\n")
    r = _run(str(cl), cwd=tmp_path)
    assert r.returncode == 0, f"case-variant history must be excluded: {r.stdout + r.stderr}"


def test_cli_warns_on_missing_explicit_path(tmp_path):
    r = _run(str(tmp_path / "nope.md"), cwd=tmp_path)
    out = r.stdout + r.stderr
    assert "nope.md" in out and ("skip" in out.lower() or "warn" in out.lower() or "not" in out.lower())


# ---- --tree mode: the whole-tree walk publish_public.sh delegates to (WS-0 ①) --

# The blocklist enumeration + nested append-only refusal + scan used to live in
# publish_public.sh as bash (find/xargs/pure-bash walk). It now lives here as
# `scan_overclaim.py --tree <dir>`, killing the shell edges (SIGPIPE / dash-basename
# / trailing-slash / ARG_MAX) that 4 audit rounds kept finding. These tests lock the
# Python contract directly; private release-orchestration tests lock it through
# the private canonical publish path without exposing that internal tooling here.


def test_tree_dirty_outward_doc_fails(tmp_path):
    """R1/S1.1: a banned causal claim in an outward doc -> exit 4 (the ONLY
    publish-overridable code; deliberately not 1, see audit c88dc6ae f1)."""
    (tmp_path / "README.md").write_text("AQG catches fewer bugs than the baseline.\n")
    r = _run("--tree", str(tmp_path), cwd=tmp_path)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "fewer bugs" in (r.stdout + r.stderr)


def test_tree_clean_outward_doc_passes(tmp_path):
    (tmp_path / "README.md").write_text("AQG runs a multi-voice audit gate.\n")
    r = _run("--tree", str(tmp_path), cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_tree_ignores_code_file(tmp_path):
    """R1/S1.2: a banned phrase inside a blocklisted code file must NOT block."""
    (tmp_path / "README.md").write_text("AQG runs an audit gate.\n")
    (tmp_path / "wordlist.py").write_text('BANNED = "fewer bugs"  # definition\n')
    r = _run("--tree", str(tmp_path), cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_tree_extensionless_doc_scanned(tmp_path):
    """Blocklist (not allowlist) fails closed on unknown/extensionless files."""
    (tmp_path / "README.md").write_text("AQG runs an audit gate.\n")
    (tmp_path / "NOTICE").write_text("This release delivers fewer bugs.\n")
    r = _run("--tree", str(tmp_path), cwd=tmp_path)
    assert r.returncode == 4, r.stdout + r.stderr  # banned claim


def test_tree_empty_fails_closed(tmp_path):
    """R1/S1.3: a tree with no scannable outward doc fails closed -> exit 3
    (structural, NOT the overridable banned code)."""
    (tmp_path / "only.py").write_text("x = 1\n")  # blocklisted -> nothing to scan
    r = _run("--tree", str(tmp_path), cwd=tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr


def test_tree_nested_history_refused(tmp_path):
    """R2/S2.1: a NESTED append-only-named file (scanner would basename-exclude it)
    is refused fail-closed, not silently skipped."""
    (tmp_path / "README.md").write_text("AQG runs an audit gate.\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CHANGELOG.md").write_text("anything at all.\n")
    r = _run("--tree", str(tmp_path), cwd=tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr  # structural, non-overridable
    assert "CHANGELOG" in (r.stdout + r.stderr)


def test_tree_root_history_exempt(tmp_path):
    """R2/S2.2: a ROOT append-only history file with a banned phrase is exempt
    (excluded by basename), so the gate does not deadlock."""
    (tmp_path / "README.md").write_text("AQG runs an audit gate.\n")
    (tmp_path / "CHANGELOG.md").write_text("v1: once claimed fewer bugs historically.\n")
    r = _run("--tree", str(tmp_path), cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_tree_missing_dir_is_usage_error(tmp_path):
    r = _run("--tree", str(tmp_path / "nope"), cwd=tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr


# ---- audit 60b40c39 hardening (round 2): fail-closed parity edges -------------


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses filesystem permission bits, so chmod 000 does not block it",
)
def test_tree_unreadable_subdir_fails_closed(tmp_path):
    """audit 60b40c39 f1 (7/7 voices): os.walk defaults to onerror=None and SILENTLY
    drops an unreadable subtree — a banned doc hidden there would be published while
    a clean root doc keeps the scan non-empty. The scanner must fail CLOSED (exit 3,
    structural) on a directory it cannot enumerate — parity with `find … || _die 5`."""
    (tmp_path / "README.md").write_text("AQG runs an audit gate.\n")  # clean root
    sub = tmp_path / "locked"
    sub.mkdir()
    (sub / "secret.md").write_text("This release delivers fewer bugs.\n")  # banned, hidden
    os.chmod(sub, 0o000)
    try:
        r = _run("--tree", str(tmp_path), cwd=tmp_path)
    finally:
        os.chmod(sub, 0o755)  # restore so tmp cleanup can recurse
    assert r.returncode == 3, (
        f"unreadable subtree must fail closed (exit 3), got {r.returncode}: "
        f"{r.stdout + r.stderr}"
    )


def test_tree_root_only_history_passes(tmp_path):
    """audit 60b40c39 f2 (Voice 6): a tree whose ONLY non-code file is a ROOT
    append-only history file must PASS (old bash `[ -s $_LIST ]` counted it), not be
    false-blocked as an empty tree. Emptiness is on ENUMERATED, not scanned, count."""
    (tmp_path / "CHANGELOG.md").write_text("v1: once claimed fewer bugs historically.\n")
    (tmp_path / "build.py").write_text('x = "fewer bugs"\n')  # blocklisted, ignored
    r = _run("--tree", str(tmp_path), cwd=tmp_path)
    assert r.returncode == 0, (
        f"root-only history must pass, not fail-closed as empty: {r.stdout + r.stderr}"
    )


@pytest.mark.parametrize("trailing", ["", "/"])
def test_tree_trailing_slash_root_vs_nested_parity(tmp_path, trailing):
    """audit 60b40c39 f4 (Voice 4): pin trailing-slash nested-vs-root parity so a
    future relative_to/parts change can't reopen the 00afdb79-class bug. A root
    CHANGELOG is exempt; a nested one is refused — regardless of a trailing slash."""
    (tmp_path / "README.md").write_text("AQG runs an audit gate.\n")
    (tmp_path / "CHANGELOG.md").write_text("root history says fewer bugs.\n")  # exempt
    arg = str(tmp_path) + trailing
    assert _run("--tree", arg, cwd=tmp_path).returncode == 0
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CHANGELOG.md").write_text("nested.\n")  # refused
    assert _run("--tree", arg, cwd=tmp_path).returncode == 3


# ---- audit c88dc6ae hardening (round 3): crash / read-error / oversize --------


def test_tree_unexpected_error_is_structural(tmp_path, monkeypatch):
    """audit c88dc6ae f1 (4/4 voices): an unexpected exception inside scan_tree must
    resolve to the NON-overridable structural code (3), never propagate as Python's
    native crash exit 1 — which equals the overridable banned code at publish."""
    import scan_overclaim

    def _boom(_root):
        raise RuntimeError("simulated scanner failure")

    monkeypatch.setattr(scan_overclaim, "scan_tree", _boom)
    assert scan_overclaim._main_tree(tmp_path) == 3


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses filesystem permission bits",
)
def test_tree_unreadable_file_fails_closed(tmp_path):
    """audit c88dc6ae f2 (Voice 3): a listable dir with an UNREADABLE regular file
    fails closed (exit 3) — the read-path OSError is a structural error, not a
    silent skip that could drop a banned claim."""
    (tmp_path / "README.md").write_text("AQG runs an audit gate.\n")
    secret = tmp_path / "secret.md"
    secret.write_text("delivers fewer bugs\n")
    os.chmod(secret, 0o000)
    try:
        rc = _run("--tree", str(tmp_path), cwd=tmp_path).returncode
    finally:
        os.chmod(secret, 0o644)
    assert rc == 3, rc


def test_tree_oversize_file_fails_closed(tmp_path, monkeypatch):
    """audit c88dc6ae f2 (Voice 4): a file above the read cap is STRUCTURAL
    fail-closed (exit 3), not a silent skip — an unbounded read_text could OOM."""
    import scan_overclaim

    monkeypatch.setattr(scan_overclaim, "MAX_SCAN_BYTES", 8)
    (tmp_path / "README.md").write_text("this prose is longer than eight bytes\n")
    assert scan_overclaim._main_tree(tmp_path) == 3
