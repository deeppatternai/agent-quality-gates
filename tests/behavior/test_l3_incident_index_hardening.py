"""L3 INC-TITLE-LEAK + DR-05/06 regression for aqg_incident_index.py.

- INC-TITLE-LEAK (MED, security): `index` validated a PLACEHOLDER title
  (`metadata.get("_title_for_validation", "placeholder")`) while rendering the REAL
  H1 (`_extract_title`) verbatim into the git-committed INDEX.md (md-cell-escaped
  only). The record-write guard `assert_safe_incident_record` runs only on the write
  path, so a secret in an incident's H1 reached INDEX.md (public after launch). Fix:
  validate the real title at index time → an unsafe title fails `check_incident_record`
  and the record is skipped.
- Filename sibling leak (audit 42209122, gpt f1 + gemini f2 convergent): `_filename`
  is rendered as the INDEX.md link target/label; the filename charset whitelist still
  admits a lowercase token-shaped slug (`ghp_<20 lc>`), so a secret in the filename
  bypassed leak detection. Fix: leak-scan the filename before indexing.
- DR-05 (MED) / DR-06 (LOW): `record --force` and `index` used `write_text` (non-atomic)
  → a crash mid-write leaves a truncated file. Fix: `_atomic_write` (mkstemp temp +
  os.replace).

INC-TITLE/filename-leak repros + the `_atomic_write` helper FAIL against pre-fix source:
`git stash push -- scripts/aqg_incident_index.py` -> RED.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import aqg_incident_index as aii  # noqa: E402

_SECRET = "ghp_" + "A" * 36  # GitHub-token-shaped literal (title test)
_SECRET_SLUG = "ghp_" + "a" * 30  # lowercase: matches filename charset + gh-token


def _incident_text(*, title: str = "db connection timeout") -> str:
    return (
        "<!--\n"
        "AQG_INCIDENT_RECORD\n"
        "schema_version: 1\n"
        "slug: test-incident\n"
        "date: 2026-06-03\n"
        "actor: claude\n"
        "severity: P3\n"
        "detection_source: manual\n"
        "impact_scope: internal\n"
        "-->\n"
        f"# Incident Record: {title}\n\nsome body text\n"
    )


def _incidents_dir(tmp_path: Path, *, filename: str, title: str) -> Path:
    incidents = tmp_path / "incidents"
    incidents.mkdir(exist_ok=True)
    (incidents / filename).write_text(_incident_text(title=title), encoding="utf-8")
    return incidents


# --------------------------------------------------------------------------- #
# INC-TITLE-LEAK + filename sibling — nothing unredacted reaches INDEX.md
# --------------------------------------------------------------------------- #
class TestIncidentIndexLeak:
    def test_secret_h1_not_rendered_into_index(self, tmp_path: Path) -> None:
        incidents = _incidents_dir(
            tmp_path, filename="2026-06-03-test-incident.md",
            title=f"leaked {_SECRET} token",
        )
        body = aii._render_index(aii._scan_incidents(incidents))
        assert _SECRET not in body

    def test_secret_in_filename_not_rendered_into_index(self, tmp_path: Path) -> None:
        # sibling path: clean title/metadata but a token-shaped FILENAME slug
        incidents = _incidents_dir(
            tmp_path, filename=f"2026-06-03-{_SECRET_SLUG}.md",
            title="clean title",
        )
        records = aii._scan_incidents(incidents)  # pre-fix: filename indexed
        body = aii._render_index(records)
        assert _SECRET_SLUG not in body
        assert all(_SECRET_SLUG not in r.get("_filename", "") for r in records)

    def test_clean_incident_still_indexed(self, tmp_path: Path) -> None:
        # baseline: a realistic clean incident IS indexed (fix must not over-skip)
        incidents = _incidents_dir(
            tmp_path, filename="2026-06-03-test-incident.md",
            title="Database connection pool exhausted during peak load",
        )
        records = aii._scan_incidents(incidents)
        assert len(records) == 1
        assert "Database connection pool" in records[0]["_title"]


# --------------------------------------------------------------------------- #
# DR-05 / DR-06 — atomic write helper + it is actually wired into the commands
# --------------------------------------------------------------------------- #
class TestAtomicWrite:
    def test_writes_content_and_leaves_no_temp(self, tmp_path: Path) -> None:
        p = tmp_path / "out.md"
        aii._atomic_write(p, "hello")
        assert p.read_text(encoding="utf-8") == "hello"
        assert [q.name for q in tmp_path.iterdir()] == ["out.md"]

    def test_replaces_existing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "out.md"
        p.write_text("old", encoding="utf-8")
        aii._atomic_write(p, "new")
        assert p.read_text(encoding="utf-8") == "new"

    def test_cleans_temp_on_replace_error(self, tmp_path, monkeypatch) -> None:
        # if os.replace fails after the temp is written, no temp residue is left
        p = tmp_path / "out.md"

        def boom(*_a, **_k):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(aii.os, "replace", boom)
        try:
            aii._atomic_write(p, "x")
        except OSError:
            pass
        assert [q.name for q in tmp_path.iterdir()] == []  # temp cleaned up
        assert not p.exists()

    def test_cmd_index_routes_through_atomic_write(self, tmp_path, monkeypatch) -> None:
        # proves _cmd_index uses _atomic_write (a revert to write_text would fail this)
        incidents = _incidents_dir(
            tmp_path, filename="2026-06-03-test-incident.md", title="clean title",
        )
        out = tmp_path / "INDEX.md"
        calls: list[Path] = []
        orig = aii._atomic_write
        monkeypatch.setattr(
            aii, "_atomic_write", lambda pth, b: (calls.append(pth), orig(pth, b))[1]
        )
        rc = aii._cmd_index(argparse.Namespace(dir=str(incidents), output=str(out)))
        assert rc == aii.EXIT_OK
        assert out in calls
        assert out.is_file()
