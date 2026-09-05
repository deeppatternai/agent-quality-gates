"""Behavior tests for `aqg_memory_hygiene.py staleness` (ADR §4.4, §6 acceptance #2).

CLI-level tests use a very old date (2020) so age >> any default threshold on any
run date, and steer the threshold via --days / env to flip listing deterministically.
Exact age==threshold boundaries live in the core tests (injected today).
"""

from __future__ import annotations

import datetime as dt

import _mh_core as core
from _mh_fixtures import compliant_meta, run_cli, write_node


def _stale(d, *extra) -> tuple[int, str]:
    return run_cli(["staleness", "--memory-dir", str(d), *extra])


def _superseded_meta(**over) -> dict:
    meta = compliant_meta(status="superseded", volatility="volatile", last_verified="2020-01-01")
    meta.update(
        {"superseded_by": "null", "superseded_reason": "moved", "superseded_date": "2020-02-01"}
    )
    meta.update(over)
    return meta


# ---- listing rules -------------------------------------------------------


def test_staleness_volatile_over_threshold_listed(tmp_path):
    write_node(tmp_path, "ref_old.md", metadata=compliant_meta(volatility="volatile", last_verified="2020-01-01"))
    rc, text = _stale(tmp_path)
    assert rc == 0
    assert "ref_old.md" in text
    assert "待核" in text or "re-verification" in text.lower()


def test_staleness_durable_not_listed(tmp_path):
    write_node(tmp_path, "feedback_old.md", metadata=compliant_meta(volatility="durable", last_verified="2020-01-01"))
    rc, text = _stale(tmp_path)
    assert rc == 0
    # durable is not gated → must not appear in the "needing re-verification" list
    assert "## Volatile memories needing re-verification" not in text or "feedback_old.md" not in text.split("## Skipped")[0]


def test_staleness_superseded_skipped(tmp_path):
    write_node(tmp_path, "feedback_old.md", metadata=_superseded_meta())
    rc, text = _stale(tmp_path)
    assert rc == 0
    assert "superseded" in text.lower()
    # appears under skipped, not under the stale list
    stale_section = text.split("## Skipped")[0]
    assert "feedback_old.md" not in stale_section


def test_staleness_future_dated_listed(tmp_path):
    write_node(tmp_path, "ref_future.md", metadata=compliant_meta(volatility="volatile", last_verified="2099-01-01"))
    rc, text = _stale(tmp_path)
    assert rc == 0
    assert "ref_future.md" in text
    assert "future" in text.lower()


# ---- graceful degradation (never crash) ----------------------------------


def test_staleness_missing_last_verified_graceful_skip(tmp_path):
    meta = compliant_meta(volatility="volatile")
    del meta["last_verified"]
    write_node(tmp_path, "ref_x.md", metadata=meta)
    rc, text = _stale(tmp_path)
    assert rc == 0, "staleness must never gate"
    assert "ref_x.md" in text and ("skip" in text.lower() or "malformed" in text.lower())


def test_staleness_malformed_frontmatter_graceful_skip(tmp_path):
    write_node(tmp_path, "ref_broken.md", raw="not even frontmatter\n")
    rc, text = _stale(tmp_path)
    assert rc == 0
    assert "ref_broken.md" in text  # surfaced as skipped, not crashed


def test_staleness_invalid_calendar_date_graceful_skip(tmp_path):
    # audit round2 gpt-f1: an invalid timestamp-shaped date (PyYAML raises
    # ValueError) must graceful-skip with staleness STILL exiting 0, not crash to 70.
    raw = (
        "---\nname: bad\ndescription: d\nmetadata:\n  type: reference\n"
        "  status: active\n  volatility: volatile\n  last_verified: 2026-02-30\n---\n\nbody\n"
    )
    write_node(tmp_path, "bad.md", raw=raw)
    rc, text = _stale(tmp_path)
    assert rc == 0, f"staleness must always exit 0 even on an invalid date (rc={rc})"
    assert "bad.md" in text  # surfaced as skipped, not crashed


def test_staleness_dirty_corpus_does_not_crash(tmp_path):
    # mix of compliant + malformed + missing-field → still exits 0 cleanly
    write_node(tmp_path, "ok.md", metadata=compliant_meta(volatility="volatile", last_verified="2020-01-01"))
    write_node(tmp_path, "broken.md", raw="---\n:::nope: [\n---\n")
    write_node(tmp_path, "MEMORY.md", raw="# index\n")
    rc, _ = _stale(tmp_path)
    assert rc == 0


# ---- threshold config (--days / env) -------------------------------------


def test_staleness_days_flag_raises_threshold_unlists(tmp_path):
    write_node(tmp_path, "ref_old.md", metadata=compliant_meta(volatility="volatile", last_verified="2020-01-01"))
    rc_listed, t_listed = _stale(tmp_path, "--days", "1")
    rc_unlisted, t_unlisted = _stale(tmp_path, "--days", "100000000")
    assert rc_listed == 0 and rc_unlisted == 0
    assert "ref_old.md" in t_listed.split("## Skipped")[0]
    assert "ref_old.md" not in t_unlisted.split("## Skipped")[0]


def test_staleness_env_threshold_honored(tmp_path, monkeypatch):
    write_node(tmp_path, "ref_old.md", metadata=compliant_meta(volatility="volatile", last_verified="2020-01-01"))
    monkeypatch.setenv("AQG_MEMORY_STALE_DAYS", "100000000")
    rc, text = _stale(tmp_path)
    assert rc == 0
    assert "ref_old.md" not in text.split("## Skipped")[0]


def test_staleness_days_flag_overrides_env(tmp_path, monkeypatch):
    write_node(tmp_path, "ref_old.md", metadata=compliant_meta(volatility="volatile", last_verified="2020-01-01"))
    monkeypatch.setenv("AQG_MEMORY_STALE_DAYS", "100000000")  # env says fresh
    rc, text = _stale(tmp_path, "--days", "1")  # explicit flag wins → stale
    assert rc == 0
    assert "ref_old.md" in text.split("## Skipped")[0]


# ---- dir resolution / exit semantics -------------------------------------


def test_staleness_empty_dir_exit_zero(tmp_path):
    rc, _ = _stale(tmp_path)
    assert rc == 0


def test_staleness_missing_dir_exit_zero(tmp_path):
    rc, text = _stale(tmp_path / "nope")
    assert rc == 0
    assert "no memory dir" in text.lower()


# ---- core boundary (deterministic, injected today) -----------------------


def _assess(tmp_path, filename, today, threshold):
    nodes = [core.parse_node(p) for p in core.iter_node_files(tmp_path)]
    target = next(n for n in nodes if n.path.name == filename)
    return core.assess_staleness(target, today, threshold)


def test_staleness_age_equals_threshold_is_fresh(tmp_path):
    write_node(tmp_path, "ref.md", metadata=compliant_meta(volatility="volatile", last_verified="2026-01-01"))
    verdict, _, age = _assess(tmp_path, "ref.md", dt.date(2026, 4, 1), 90)
    assert age == 90 and verdict == "fresh"  # exactly threshold → not yet stale


def test_staleness_age_exceeds_threshold_is_stale(tmp_path):
    write_node(tmp_path, "ref.md", metadata=compliant_meta(volatility="volatile", last_verified="2026-01-01"))
    verdict, _, age = _assess(tmp_path, "ref.md", dt.date(2026, 4, 2), 90)
    assert age == 91 and verdict == "stale"


def test_staleness_durable_fresh_regardless_of_age(tmp_path):
    write_node(tmp_path, "f.md", metadata=compliant_meta(volatility="durable", last_verified="2000-01-01"))
    verdict, _, _ = _assess(tmp_path, "f.md", dt.date(2026, 1, 1), 90)
    assert verdict == "fresh"


# ---- report-count self-consistency (audit gemini-f1) ---------------------


def test_staleness_durable_counted_as_fresh_not_mislabeled_skipped(tmp_path):
    # gemini-f1: the text label claimed `skipped (durable / ...)` but durable
    # nodes are verdict=fresh and never enter the skipped list — they vanished
    # entirely, contradicting the label. Counts must be self-consistent:
    # nodes_scanned == stale + skipped + fresh, and durable lands in fresh.
    import json

    for i in range(3):
        write_node(tmp_path, f"dur_{i}.md",
                   metadata=compliant_meta(volatility="durable", last_verified="2020-01-01"))
    write_node(tmp_path, "vol_stale.md",
               metadata=compliant_meta(volatility="volatile", last_verified="2020-01-01"))

    rc, text = _stale(tmp_path)
    assert rc == 0
    # the misleading "skipped (durable ...)" wording must be gone
    assert "skipped (durable" not in text
    # the human text output (where the misleading label lived) must carry the same
    # self-consistent counts (audit commit-gate f2)
    assert "- nodes_scanned: 4" in text
    assert "- stale (volatile past horizon): 1" in text
    assert "- skipped (superseded / malformed / missing last_verified): 0" in text
    assert "- fresh (durable / volatile within horizon): 3" in text

    _, text_j = run_cli(["staleness", "--memory-dir", str(tmp_path), "--json"])
    data = json.loads(text_j)
    assert data["nodes_scanned"] == 4
    assert len(data["stale"]) == 1            # the one volatile-stale node
    assert len(data["skipped"]) == 0          # nothing superseded / malformed / missing
    assert data["fresh"] == 3                 # the 3 durable nodes are FRESH, not skipped
    assert data["nodes_scanned"] == len(data["stale"]) + len(data["skipped"]) + data["fresh"]


def test_staleness_skipped_holds_only_undeterminable(tmp_path):
    # superseded + malformed are the genuine "skipped" cases (cannot assess);
    # durable healthy nodes must not be lumped in with them.
    import json

    write_node(tmp_path, "dur.md",
               metadata=compliant_meta(volatility="durable", last_verified="2020-01-01"))
    write_node(tmp_path, "broken.md", raw="not even frontmatter\n")
    _, text_j = run_cli(["staleness", "--memory-dir", str(tmp_path), "--json"])
    data = json.loads(text_j)
    assert data["nodes_scanned"] == 2
    assert len(data["skipped"]) == 1          # only broken.md
    assert data["skipped"][0]["file"] == "broken.md"
    assert data["fresh"] == 1                 # dur.md
