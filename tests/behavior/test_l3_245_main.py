"""L3 #245 — main() --with-repo-reality wiring + acceptance #1 (zero-subprocess default).

ADR §6:
- acceptance #1: the DEFAULT (no-flag) path is logic-unchanged and makes ZERO
  git/gh subprocess calls (a subprocess spy asserts it — replaces brittle
  byte-identical comparison).
- the flag wires collect_repo_reality with since = the ledger's last-activity
  timestamp and renders the banner in-band.

End-to-end, in-process: import the CLI + collect module (the self_test pattern),
seed a temp XDG_DATA_HOME ledger, monkeypatch collect.run so no REAL subprocess
fires under the flag either.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _sub in ("contracts", "skills/aqg-project-status/scripts"):
    _p = str(_ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import aqg_project_status as cli  # noqa: E402
import collect_repo_reality as collect  # noqa: E402
from ledger.paths import project_events_path  # noqa: E402

# A 2-event ledger whose LAST activity (ledger_seq 2) is 2026-05-29T10:05:00Z →
# that timestamp is the expected `since`.
_EVENTS = [
    '{"schema_version":"1.0","event_id":"eaf:r1:1","project":"o/r","source":"eaf",'
    '"source_ref":{"run_id":"r1"},"kind":"progress",'
    '"payload":{"phase_event":"started","title":"Kick off"},'
    '"occurred_at":"2026-05-29T10:00:00Z","recorded_at":"2026-05-29T12:00:00+00:00","ledger_seq":1}',
    '{"schema_version":"1.0","event_id":"eaf:r1:2","project":"o/r","source":"eaf",'
    '"source_ref":{"run_id":"r1"},"kind":"defect",'
    '"payload":{"defect_id":"D-1","action":"opened","title":"Leak","severity":"critical"},'
    '"occurred_at":"2026-05-29T10:05:00Z","recorded_at":"2026-05-29T12:00:00+00:00","ledger_seq":2}',
]
_LAST_ACTIVITY = "2026-05-29T10:05:00Z"


@contextmanager
def _data_home(tmp: str):
    prev = os.environ.get("XDG_DATA_HOME")
    os.environ["XDG_DATA_HOME"] = tmp
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = prev


def _seed_ledger(tmp: str, project_id: str = "o/r") -> Path:
    events = project_events_path(project_id)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("\n".join(_EVENTS) + "\n", encoding="utf-8")
    return events


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_default_path_makes_zero_git_gh_subprocess(monkeypatch):
    import subprocess as sp
    seen = []
    real_run = sp.run

    def spy(cmd, *a, **k):
        seen.append(cmd)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(sp, "run", spy)
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--no-drain", "--format", "text"])
    assert rc == 0, out
    git_gh = [c for c in seen if isinstance(c, (list, tuple)) and c and str(c[0]) in ("git", "gh")]
    assert git_gh == [], git_gh                      # opt-in: zero VCS subprocess by default
    assert "Project Ledger — o/r" in out             # default logic unchanged
    assert "coverage gap" not in out                 # no banner without the flag


def test_default_json_has_no_repo_reality_key():
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--no-drain", "--json"])
    assert rc == 0
    assert "repo_reality" not in json.loads(out)     # default JSON shape unchanged


def test_flag_renders_banner_with_since_from_last_activity(monkeypatch):
    prs = [{"mergedAt": "2026-06-02T00:00:00Z"}]    # after last_activity → counted
    seen = {}

    def fake_run(cmd, cwd=None, timeout=30):
        if cmd[0] == "git":
            if "rev-parse" in cmd:
                return 0, "false", ""
            seen["git"] = cmd
            return 0, "12", ""
        seen["gh"] = cmd
        return 0, json.dumps(prs), ""

    monkeypatch.setattr(collect, "run", fake_run)
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--repo", tmp, "--no-drain",
                           "--format", "text", "--with-repo-reality"])
    assert rc == 0, out
    assert "12 commits" in out
    assert "1 PRs" in out
    assert "coverage gap" in out
    assert f"since {_LAST_ACTIVITY}" in out                       # since = ledger last activity
    assert f"--since={_LAST_ACTIVITY}" in seen["git"]             # passed through to git rev-list


def test_flag_json_carries_repo_reality(monkeypatch):
    def fake_run(cmd, cwd=None, timeout=30):
        if cmd[0] == "git":
            return (0, "false", "") if "rev-parse" in cmd else (0, "7", "")
        return (0, "[]", "")

    monkeypatch.setattr(collect, "run", fake_run)
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--repo", tmp, "--no-drain",
                           "--json", "--with-repo-reality"])
    assert rc == 0, out
    data = json.loads(out)
    assert data["repo_reality"]["commits"] == 7
    assert data["repo_reality"]["since"] == _LAST_ACTIVITY


def test_flag_exit_0_even_when_collection_degrades(monkeypatch):
    # both sources down → banner shows unavailable notes, report still exits 0.
    def fake_run(cmd, cwd=None, timeout=30):
        return collect._RC_NOT_FOUND, "", ""

    monkeypatch.setattr(collect, "run", fake_run)
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--repo", tmp, "--no-drain",
                           "--format", "text", "--with-repo-reality"])
    assert rc == 0, out
    assert "commits: unavailable" in out
    assert "PRs: unavailable" in out


def test_no_ledger_note_renders_in_band_all_formats():
    # G3 (acceptance #5): the "no ledger yet" data-quality note was stderr-only;
    # it must ALSO render in-band so a consumer reading only the report (redirected
    # to a file / piped) still sees WHY the report is empty.
    for fmt in ("text", "markdown", "html", "json"):
        argv = ["--project-id", "no/such", "--no-drain"]
        argv += ["--json"] if fmt == "json" else ["--format", fmt]
        with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
            rc, out, err = _run(argv)
        assert rc == 0, (fmt, out, err)
        assert "no ledger yet" in err, fmt           # stderr channel unchanged
        assert "no ledger yet" in out, (fmt, out)    # AND now in-band in the report


def test_collector_error_surfaces_in_band(monkeypatch):
    # gpt-f1: if collection raises (partial checkout / unexpected), the banner must
    # STILL render in-band with a collector-error note — not silently vanish.
    def _boom(*a, **k):
        raise RuntimeError("simulated collector failure")

    monkeypatch.setattr(collect, "collect_repo_reality", _boom)
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, err = _run(["--project-id", "o/r", "--repo", tmp, "--no-drain",
                             "--format", "text", "--with-repo-reality"])
    assert rc == 0, out
    assert "repo-reality: unavailable (collector-error)" in out   # in-band, not vanished
    assert "collection skipped" in err                            # stderr channel too
