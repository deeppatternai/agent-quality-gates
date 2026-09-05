"""L3-6b — manifest-validator + wip_recover input robustness (manual-only DX).

- WB-01/02 (MED): `validate_orchestration_manifest.py` / `validate_simulation_manifest.py`
  `_load` caught only (ValueError, JSONDecodeError). Deeply nested JSON overflows the
  decoder's recursion guard with a `RecursionError` (a RuntimeError subclass, NOT caught),
  so the CLI emitted an uncaught traceback instead of a clean EXIT_INVALID. Fix: catch
  RecursionError → "invalid JSON: nested too deeply".
- WB-05/06 (LOW): the same `_load` used `read_text` with no size cap, so a giant regular
  file was read whole into memory before validation. Fix: bounded read (1 MiB) → reject
  oversized manifests early. (FIFOs are already rejected by main's is_file check.)
- WB-04 (LOW): `wip_recover._filter_by_redaction` lazily imports
  `aqg_doctor._extract_field_from_violation` WITHOUT the try/except that guards the
  `_wip_redaction` import just above it. A missing/broken aqg_doctor crashed the
  SessionStart recovery path (absorbed by the hook's `|| true`, but defensively asymmetric).
  Fix: guard the import → degrade to a generic message, still skipping the unsafe snapshot.

Stash-proven RED: `git stash push -- scripts/validate_orchestration_manifest.py
scripts/validate_simulation_manifest.py scripts/wip_recover.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import validate_orchestration_manifest as vom  # noqa: E402
import validate_simulation_manifest as vsm  # noqa: E402
import wip_recover as wr  # noqa: E402
import _wip_redaction  # noqa: E402

_VALIDATORS = [vom, vsm]


@pytest.mark.parametrize("mod", _VALIDATORS, ids=["orchestration", "simulation"])
class TestManifestRobustness:
    def test_deeply_nested_json_is_clean_invalid_not_traceback(self, mod, tmp_path, capsys) -> None:
        # Version-robust contract: deeply nested input must yield a clean
        # EXIT_INVALID with NO uncaught traceback. Whether it raises RecursionError
        # ("nested too deeply") depends on the interpreter's json C-recursion limit
        # (Python 3.9 trips at ~1000; 3.12+ on Linux parses far deeper, then the
        # list fails the isinstance(dict) check → "must be a JSON object"). BOTH are
        # clean; the pre-fix bug was an UNCAUGHT RecursionError traceback. The
        # deterministic handler coverage is in test_recursion_error_from_load_is_caught.
        manifest = tmp_path / "deep.json"
        manifest.write_text("[" * 20000 + "]" * 20000, encoding="utf-8")
        rc = mod.main([str(manifest)])  # must NOT raise
        assert rc == mod.EXIT_INVALID, rc
        err = capsys.readouterr().err
        assert ("nested too deeply" in err) or ("must be a JSON object" in err), err
        assert "Traceback" not in err

    def test_recursion_error_from_load_is_caught(self, mod, tmp_path, monkeypatch, capsys) -> None:
        # Deterministically exercise the `except RecursionError` handler on EVERY
        # interpreter (the real-depth test only trips it where the C-recursion limit
        # is low enough). Pre-fix: RecursionError propagates out of main() → error.
        manifest = tmp_path / "x.json"
        manifest.write_text("{}", encoding="utf-8")

        def _boom(_path):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(mod, "_load", _boom)
        rc = mod.main([str(manifest)])
        assert rc == mod.EXIT_INVALID, rc
        assert "nested too deeply" in capsys.readouterr().err

    def test_oversized_manifest_rejected_early(self, mod, tmp_path, capsys) -> None:
        manifest = tmp_path / "big.json"
        manifest.write_text('{"x":"' + "A" * (2 * 1024 * 1024) + '"}', encoding="utf-8")
        rc = mod.main([str(manifest)])
        assert rc == mod.EXIT_INVALID, rc
        assert "exceeds" in capsys.readouterr().err  # pre-fix: read whole, "missing field"

    def test_valid_small_manifest_still_loads(self, mod, tmp_path, capsys) -> None:
        # GREEN guard: a syntactically valid (if schema-incomplete) manifest still
        # reaches the redaction validator (not rejected by the read bound).
        manifest = tmp_path / "ok.json"
        manifest.write_text('{"hello": "world"}', encoding="utf-8")
        rc = mod.main([str(manifest)])
        # schema-incomplete → EXIT_INVALID, but via the validator (no "exceeds"/traceback)
        err = capsys.readouterr().err
        assert rc in (mod.EXIT_OK, mod.EXIT_INVALID)
        assert "exceeds" not in err and "nested too deeply" not in err

    def test_at_cap_boundary(self, mod, tmp_path) -> None:
        # Boundary precision via _load directly (parse is cheap; avoids the
        # full redaction scan main() would run on a 1 MiB value).
        cap = mod._MAX_MANIFEST_BYTES
        wrapper = len(b'{"x":"') + len(b'"}')  # == 8
        under = tmp_path / "under.json"
        under.write_bytes(b'{"x":"' + b"A" * (cap - wrapper) + b'"}')  # == cap
        assert under.stat().st_size == cap
        mod._load(under)  # exactly at cap → must NOT raise
        over = tmp_path / "over.json"
        over.write_bytes(b'{"x":"' + b"A" * (cap - wrapper + 1) + b'"}')  # == cap + 1
        with pytest.raises(ValueError, match="exceeds"):
            mod._load(over)


class TestWipRecoverImportGuard:
    _RAW = ("symptom: leak-value-xyz", "actor: bad-actor-abc")

    def _unsafe_entry(self, monkeypatch):
        class _Unsafe:
            is_safe = False
            violations = TestWipRecoverImportGuard._RAW

        monkeypatch.setattr(_wip_redaction, "check_wip_snapshot", lambda s: _Unsafe())
        return wr.ScannedSnapshot(
            path=Path("/tmp/snap.json"), snapshot={"a": 1}, mtime_iso="2026-01-01T00:00:00Z"
        )

    def test_aqg_doctor_import_failure_degrades_not_crash(self, monkeypatch, capsys) -> None:
        # WB-04: force the lazy aqg_doctor import to fail (pre-fix: ImportError
        # propagates out of _filter_by_redaction).
        monkeypatch.setitem(sys.modules, "aqg_doctor", None)
        entry = self._unsafe_entry(monkeypatch)
        out = wr._filter_by_redaction([entry])
        assert out == []  # unsafe snapshot still skipped (security preserved)
        err = capsys.readouterr().err
        # audit f2: generic count emitted, raw violation values NOT echoed
        assert "2 violation(s)" in err
        assert "leak-value-xyz" not in err and "bad-actor-abc" not in err

    def test_aqg_doctor_extraction_raises_degrades(self, monkeypatch, capsys) -> None:
        # audit f1: a NON-ImportError raise from the diagnostic helper must also
        # degrade (broadened `except Exception`), not abort recovery.
        import types

        fake = types.ModuleType("aqg_doctor")

        def _boom(_v):
            raise ValueError("malformed violation string")

        fake._extract_field_from_violation = _boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "aqg_doctor", fake)
        entry = self._unsafe_entry(monkeypatch)
        out = wr._filter_by_redaction([entry])
        assert out == []  # still skipped, no crash
        err = capsys.readouterr().err
        assert "2 violation(s)" in err
        assert "leak-value-xyz" not in err

    def test_safe_snapshot_kept(self, monkeypatch) -> None:
        class _Safe:
            is_safe = True
            violations = ()

        monkeypatch.setattr(_wip_redaction, "check_wip_snapshot", lambda s: _Safe())
        entry = wr.ScannedSnapshot(
            path=Path("/tmp/snap.json"), snapshot={"a": 1}, mtime_iso="2026-01-01T00:00:00Z"
        )
        assert wr._filter_by_redaction([entry]) == [entry]
