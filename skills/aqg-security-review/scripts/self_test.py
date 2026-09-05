#!/usr/bin/env python3
"""Minimal self-test for aqg_security_review.py.

Verifies the helper imports cleanly, prints OWASP / CWE / secure-defaults
sections, the closeout-importable skeleton, and supports the `--json` and
`--list` flags. Mirrors the structure of `aqg-evidence-closeout/scripts/self_test.py`.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import aqg_security_review as review


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    saved = sys.argv
    try:
        sys.argv = ["aqg_security_review.py", *argv]
        with redirect_stdout(out), redirect_stderr(err):
            rc = review.main()
    finally:
        sys.argv = saved
    return rc, out.getvalue(), err.getvalue()


def test_default_prints_all_three_tables_and_skeleton() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rc, text, _ = _run_main(["--repo", str(Path(tmp))])
    assert rc == 0, f"main returned {rc}"
    assert "OWASP Top 10" in text, "missing OWASP section"
    assert "CWE Top 25" in text, "missing CWE section"
    assert "Secure-by-default" in text, "missing secure-defaults section"
    assert "security_review:" in text, "missing closeout skeleton"


def test_list_owasp_only_omits_other_sections() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rc, text, _ = _run_main(["--repo", str(Path(tmp)), "--list", "owasp"])
    assert rc == 0
    assert "OWASP Top 10" in text
    assert "CWE Top 25" not in text, "CWE leaked when --list=owasp"
    assert "Secure-by-default" not in text, "defaults leaked when --list=owasp"


def test_json_emits_parseable_payload_with_all_data() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rc, text, _ = _run_main(["--repo", str(Path(tmp)), "--json"])
    assert rc == 0
    payload = json.loads(text)
    assert "owasp_top_10" in payload and len(payload["owasp_top_10"]) == 10, \
        "OWASP must have exactly 10 categories"
    assert "cwe_top_25" in payload and len(payload["cwe_top_25"]) == 27, \
        "CWE Top 25 (2023 base, 25) + 2024 net-new (CWE-200, CWE-400) = 27"
    assert "secure_defaults" in payload and len(payload["secure_defaults"]) == 8, \
        "secure-defaults must have 8 categories"
    assert "ledger_skeleton" in payload and "security_review" in payload["ledger_skeleton"]


def test_owasp_each_category_has_at_least_three_patterns() -> None:
    for entry in review.OWASP_TOP_10:
        assert len(entry["patterns"]) >= 3, \
            f"OWASP {entry['id']} must have ≥3 patterns (Owner spec); got {len(entry['patterns'])}"


def test_secure_defaults_has_eight_categories_each_with_lib() -> None:
    assert len(review.SECURE_DEFAULTS) == 8, "must cover 8 secure-default categories"
    for d in review.SECURE_DEFAULTS:
        assert len(d["libraries"]) >= 1, f"category {d['category']} must list ≥1 library"
        for lib in d["libraries"]:
            assert lib.get("name") and lib.get("ecosystem"), \
                f"library entry missing name/ecosystem in {d['category']}"


def test_unknown_surface_warns_but_does_not_crash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rc, _, err = _run_main(["--repo", str(Path(tmp)), "--surface", "auth,bogus"])
    assert rc == 0, "unknown surface should warn, not fail"
    assert "unknown surfaces" in err, "should warn on unknown surface"


def test_repo_path_does_not_inject_yaml_keys() -> None:
    """C1 (audit 3907d9af): a crafted --repo path must not inject YAML keys into
    the closeout skeleton the agent pastes into `.aqg/current_ledger.md`."""
    malicious = "/tmp/x\ninjected_key: HACKED\nevil: true"
    rc, text, _ = _run_main(["--repo", malicious])
    assert rc == 0, f"main returned {rc}"
    # repo is rendered via json.dumps → one quoted scalar with \n escaped, so the
    # injected lines must NOT appear as standalone (newline-prefixed) YAML keys.
    assert "\ninjected_key: HACKED" not in text, "newline in --repo injected a YAML key"
    assert "\nevil: true" not in text, "newline in --repo injected a second YAML key"
    try:
        import re as _re
        import yaml as _yaml
        m = _re.search(r"```yaml\n(.*?)\n```", text, _re.DOTALL)
        assert m, "no yaml block found in output"
        data = _yaml.safe_load(m.group(1))
        assert "injected_key" not in data and "evil" not in data, "injection leaked to top level"
        assert "injected_key" not in data["security_review"], "injection leaked into block"
    except ImportError:
        pass  # PyYAML optional; string assertions above already gate the fix


def test_all_invalid_surfaces_exits_nonzero() -> None:
    """P2: a non-empty --surface that filters to nothing must not fail-open with
    surfaces_audited: []."""
    with tempfile.TemporaryDirectory() as tmp:
        rc, _, _ = _run_main(["--repo", str(Path(tmp)), "--surface", "typo,bogus"])
    assert rc != 0, "all-invalid --surface should exit nonzero, not emit an empty checklist"


def test_no_phantom_closeout_pipe_in_output() -> None:
    """P3: SKILL.md disclaims aqg_closeout ingestion; the helper must not
    advertise piping to it."""
    with tempfile.TemporaryDirectory() as tmp:
        rc, text, _ = _run_main(["--repo", str(Path(tmp))])
    assert rc == 0
    assert "pipe to aqg_closeout" not in text, "advertises non-existent closeout ingestion"


def test_pyyaml_not_labeled_stdlib() -> None:
    """P5: yaml.safe_load is a PyYAML (third-party) API, not stdlib."""
    found = False
    for d in review.SECURE_DEFAULTS:
        for lib in d["libraries"]:
            if "yaml.safe_load" in lib["name"]:
                found = True
                assert "stdlib" not in lib["ecosystem"].lower(), "PyYAML mislabeled as stdlib"
    assert found, "yaml.safe_load entry missing from secure-defaults"


def test_owasp_cwe_carry_legacy_edition_label() -> None:
    """P7a: hardcoded 2021/2023 catalogs must be labeled legacy with a pointer to
    the current release (OWASP 2025 / CWE 2025)."""
    with tempfile.TemporaryDirectory() as tmp:
        rc, text, _ = _run_main(["--repo", str(Path(tmp))])
    assert rc == 0
    assert "2025" in text, "no pointer to the current OWASP 2025 edition"
    assert "legacy" in text.lower(), "no legacy-edition label on the 2021/2023 tables"


if __name__ == "__main__":
    test_default_prints_all_three_tables_and_skeleton()
    test_list_owasp_only_omits_other_sections()
    test_json_emits_parseable_payload_with_all_data()
    test_owasp_each_category_has_at_least_three_patterns()
    test_secure_defaults_has_eight_categories_each_with_lib()
    test_unknown_surface_warns_but_does_not_crash()
    test_repo_path_does_not_inject_yaml_keys()
    test_all_invalid_surfaces_exits_nonzero()
    test_no_phantom_closeout_pipe_in_output()
    test_pyyaml_not_labeled_stdlib()
    test_owasp_cwe_carry_legacy_edition_label()
    print("OK: aqg_security_review self-test passed (11 tests)")
