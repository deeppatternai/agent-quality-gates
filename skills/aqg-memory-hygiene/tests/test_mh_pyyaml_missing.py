"""PyYAML-missing environment contract (the one runtime-dep branch that had no
unit test until the review follow-up). Both behaviors were probe-confirmed; these
lock them against regression:

- `validate` treats a missing DECLARED dependency as a USAGE error (exit 2 +
  "cannot run"), NOT a verdict that every node is invalid.
- `staleness` graceful-skips every node and STILL exits 0 (it never gates), with
  a skip reason that names PyYAML so the user is not misled into reading an empty
  stale list as "everything is fresh".

Simulated by shadowing `yaml` in sys.modules with None so `import yaml` raises
ImportError in-process (CPython convention), matching a real uninstalled PyYAML.
"""

from __future__ import annotations

import json
import sys

from _mh_fixtures import compliant_meta, run_cli, write_node


def test_validate_pyyaml_missing_exits_2_not_false_violations(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta())  # a compliant node
    rc, text = run_cli(["validate", "--memory-dir", str(tmp_path)])
    assert rc == 2, "a missing declared dep is a usage error (2), not a schema verdict"
    assert "cannot run" in text.lower()
    assert "pyyaml" in text.lower()


def test_staleness_pyyaml_missing_graceful_skips_all_exit_0(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    write_node(tmp_path, "ref.md",
               metadata=compliant_meta(volatility="volatile", last_verified="2020-01-01"))
    rc, _ = run_cli(["staleness", "--memory-dir", str(tmp_path)])
    assert rc == 0, "staleness must always exit 0, even without PyYAML"

    _, text_j = run_cli(["staleness", "--memory-dir", str(tmp_path), "--json"])
    data = json.loads(text_j)
    assert len(data["stale"]) == 0           # unparseable → never flagged stale
    assert len(data["skipped"]) == 1         # the node graceful-skips
    assert "pyyaml" in data["skipped"][0]["reason"].lower()
