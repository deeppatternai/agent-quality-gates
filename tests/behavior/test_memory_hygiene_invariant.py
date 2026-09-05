"""aqg-memory-hygiene hard invariants (ADR §6 acceptance #3 + #4).

#3 signal-only: a black-box subprocess run of BOTH subcommands (text + json) over
a fixture corpus must not change a single byte / mtime / file in the memory dir.
This is the gate that proves the scanner never mutates memory.

#4 no external service / network: the source imports only stdlib + the declared
PyYAML dep, and contains no audit-mcp / network call tokens.

Lives in tests/behavior/ so CI collects it (the most visible gate). Self-contained
fixture builder — no dependency on the skill's own tests/ helpers.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "skills" / "aqg-memory-hygiene" / "scripts"
_ENTRY = _SCRIPTS / "aqg_memory_hygiene.py"
_CORE = _SCRIPTS / "_mh_core.py"


# ===== fixture corpus =========================================================


def _node(name: str, *, type_="feedback", status="active", volatility="durable",
          last_verified="2020-01-01", extra: str = "") -> str:
    return (
        f"---\nname: {name}\ndescription: synthetic\nmetadata:\n"
        f"  type: {type_}\n  status: {status}\n  volatility: {volatility}\n"
        f"  last_verified: {last_verified}\n{extra}---\n\nbody prose\n"
    )


def _build_corpus(d: Path) -> None:
    (d / "feedback_ok.md").write_text(_node("feedback_ok"), encoding="utf-8")
    (d / "ref_stale.md").write_text(
        _node("ref_stale", type_="reference", volatility="volatile"), encoding="utf-8"
    )
    (d / "proj_superseded.md").write_text(
        _node(
            "proj_superseded",
            type_="project",
            status="superseded",
            extra="  superseded_by: null\n  superseded_reason: moved to repo\n  superseded_date: 2020-02-01\n",
        ),
        encoding="utf-8",
    )
    (d / "broken.md").write_text("not even frontmatter\n", encoding="utf-8")
    (d / "MEMORY.md").write_text("# Memory Index\n\n- [x](feedback_ok.md) — hook\n", encoding="utf-8")


def _snapshot(d: Path) -> dict[str, tuple[bytes, int]]:
    """Map relpath -> (content bytes, mtime_ns) for every file under d."""
    snap: dict[str, tuple[bytes, int]] = {}
    for p in sorted(d.rglob("*")):
        if p.is_file():
            snap[str(p.relative_to(d))] = (p.read_bytes(), p.stat().st_mtime_ns)
    return snap


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_ENTRY), *args], capture_output=True, text=True
    )


# ===== #3 signal-only =========================================================


def test_signal_only_zero_writes(tmp_path):
    _build_corpus(tmp_path)
    before = _snapshot(tmp_path)

    r_val = _run("validate", "--memory-dir", str(tmp_path))
    r_val_json = _run("validate", "--memory-dir", str(tmp_path), "--json")
    r_stale = _run("staleness", "--memory-dir", str(tmp_path))
    r_stale_json = _run("staleness", "--memory-dir", str(tmp_path), "--json")

    after = _snapshot(tmp_path)
    assert before == after, (
        "signal-only invariant VIOLATED: running validate/staleness changed the "
        "memory dir (a file's content, mtime, or the file set differs)."
    )
    # sanity: the commands actually executed (not a crash that wrote nothing)
    assert "AQG Memory Hygiene" in r_val.stdout
    assert '"command": "validate"' in r_val_json.stdout
    assert "AQG Memory Hygiene" in r_stale.stdout
    assert '"command": "staleness"' in r_stale_json.stdout
    assert r_stale.returncode == 0  # staleness always exits 0


def test_signal_only_holds_even_when_validate_fails(tmp_path):
    # broken.md → validate reports a violation (exit 1) but STILL writes nothing.
    _build_corpus(tmp_path)
    before = _snapshot(tmp_path)
    r = _run("validate", "--memory-dir", str(tmp_path))
    after = _snapshot(tmp_path)
    assert r.returncode == 1, "a malformed node should make validate exit 1"
    assert before == after, "validate must not write even while reporting violations"


# ===== #4 no external service / network =======================================

_STDLIB_OK = {
    "argparse", "datetime", "os", "re", "json", "sys", "dataclasses", "pathlib",
    "__future__", "typing", "io", "contextlib", "tempfile",
}
_ALLOWED_IMPORTS = _STDLIB_OK | {"yaml", "_mh_core"}


def _imported_top_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    return mods


def test_only_stdlib_and_declared_pyyaml_imported():
    mods = _imported_top_modules(_ENTRY) | _imported_top_modules(_CORE)
    extra = mods - _ALLOWED_IMPORTS
    assert not extra, f"unexpected import(s) — possible network/external dependency: {sorted(extra)}"
    assert "yaml" in mods, "PyYAML (the one declared non-stdlib dep) must be imported"


def test_no_audit_mcp_or_network_call_tokens():
    src = _ENTRY.read_text(encoding="utf-8") + _CORE.read_text(encoding="utf-8")
    # ADR §6 #4: the skill emits SIGNAL only; it never invokes audit-mcp.
    # Guard both the old (gpt_audit / mcp__audit) and the renamed (de_audit /
    # mcp__decision-engine, 2026-06-05) audit-mcp tool identifiers.
    assert "gpt_audit" not in src
    assert "de_audit" not in src
    assert "mcp__audit" not in src
    assert "mcp__decision-engine" not in src
    for token in ("urlopen", "requests.get", "requests.post", "socket.socket",
                  "httpx", "smtplib", "urllib.request"):
        assert token not in src, f"network call token {token!r} present in source"
