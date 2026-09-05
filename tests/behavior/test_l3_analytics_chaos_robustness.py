"""L3-5b robustness suite for aqg_gate_analytics.py + aqg_chaos.py.

Both scripts are manual-only DX tools (no workflow references them), so these
gaps are crash-on-malformed-input / silent-data-loss DX bugs, not availability
risks. Each repro FAILS against pre-fix source (stash-proven):
`git stash push -- scripts/aqg_gate_analytics.py scripts/aqg_chaos.py` -> RED.

gate_analytics (aggregate_window + _count_in_window_reports + main):
- DR-11 (MED): a nightly case with a non-string `expected_skill`/`status`
  (e.g. a JSON list/dict) was used directly as a dict key → `TypeError:
  unhashable type`, aborting the whole aggregation. Fix: coerce non-str/empty
  to "unknown" (unhashable-safe). Sibling: a non-dict `case` element raised
  AttributeError on `case.get` → guarded with isinstance.
- DR-10 (LOW): valid-but-non-dict top-level JSON (e.g. `[1,2,3]`) passed the
  json.loads try, then `data.get(...)` raised AttributeError and aborted the
  run. Both aggregate_window and the sibling _count_in_window_reports now skip
  non-dict top-level payloads.
- DR-13 (MED): a negative --window-days put the cutoff in the FUTURE, silently
  dropping every report and exiting 0 with "(no skills)" — masking real data as
  "no data". Fix: reject <0 at the CLI as a usage error.
- DR-12 (LOW): an absurdly large --window-days overflowed timedelta/datetime
  arithmetic (OverflowError traceback). Fix: clamp the cutoff to datetime.min on
  overflow → "look back forever" = include all history, no crash.

chaos (_chaos_redact + _build_no_leak_set):
- DR-17 (MED): the 4 KiB stream cap is measured in BYTES but sliced in CHARS
  (`out[:MAX_STREAM_BYTES]`), so multibyte (CJK) output blew ~3x past the cap
  (5000 CJK chars = 15 KiB → "capped" at 12 KiB). Fix: slice the encoded bytes,
  decode with errors="ignore" to drop a partial trailing char.
- DR-19 (LOW): Path.home() raises RuntimeError when HOME is unset AND the uid
  has no passwd entry (minimal containers/CI), aborting leak-set setup. Fix:
  guard with try/except → skip the home token instead of crashing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import aqg_gate_analytics as aga  # noqa: E402
import aqg_chaos as chaos  # noqa: E402

_NOW = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)


def _write_report(reports: Path, name: str, payload: object) -> None:
    reports.mkdir(parents=True, exist_ok=True)
    (reports / name).write_text(json.dumps(payload), encoding="utf-8")


def _in_window(payload_cases: list) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-05-03T12:00:00Z",  # 1 day before _NOW
        "case_results": payload_cases,
    }


# --------------------------------------------------------------------------- #
# DR-11 — non-hashable / non-dict case data must not crash aggregation
# --------------------------------------------------------------------------- #
class TestDr11UnhashableCaseData:
    def test_list_expected_skill_does_not_crash(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        _write_report(
            reports, "nightly-1.json",
            _in_window([{"expected_skill": ["a", "b"], "status": "behavior_pass"}]),
        )
        # pre-fix: TypeError unhashable type: 'list'
        result = aga.aggregate_window(reports, days=28, now=_NOW)
        # the non-str skill is bucketed under "unknown", not crashed on
        assert result == {"unknown": {"behavior_pass": 1}}, result

    def test_dict_status_does_not_crash(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        _write_report(
            reports, "nightly-1.json",
            _in_window([{"expected_skill": "aqg-x", "status": {"k": 1}}]),
        )
        # pre-fix: TypeError unhashable type: 'dict'
        result = aga.aggregate_window(reports, days=28, now=_NOW)
        assert result == {"aqg-x": {"unknown": 1}}, result

    def test_non_dict_case_element_skipped(self, tmp_path: Path) -> None:
        # sibling: a bare string in case_results → pre-fix AttributeError
        reports = tmp_path / "reports"
        _write_report(
            reports, "nightly-1.json",
            _in_window(["not a dict", {"expected_skill": "aqg-x", "status": "behavior_pass"}]),
        )
        result = aga.aggregate_window(reports, days=28, now=_NOW)
        assert result == {"aqg-x": {"behavior_pass": 1}}, result

    def test_non_list_case_results_skipped(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        payload = _in_window([])
        payload["case_results"] = {"not": "a list"}
        _write_report(reports, "nightly-1.json", payload)
        result = aga.aggregate_window(reports, days=28, now=_NOW)
        assert result == {}, result

    def test_normal_case_still_aggregated(self, tmp_path: Path) -> None:
        # GREEN guard: the coercion must not change well-formed behavior.
        reports = tmp_path / "reports"
        _write_report(
            reports, "nightly-1.json",
            _in_window([
                {"expected_skill": "aqg-x", "status": "behavior_pass"},
                {"expected_skill": "aqg-x", "status": "behavior_fail"},
            ]),
        )
        result = aga.aggregate_window(reports, days=28, now=_NOW)
        assert result == {"aqg-x": {"behavior_pass": 1, "behavior_fail": 1}}, result

    def test_missing_skill_still_buckets_unknown(self, tmp_path: Path) -> None:
        # preserve the original `or "unknown"` behavior for missing/empty.
        reports = tmp_path / "reports"
        _write_report(
            reports, "nightly-1.json",
            _in_window([{"status": "behavior_pass"}, {"expected_skill": "", "status": "behavior_pass"}]),
        )
        result = aga.aggregate_window(reports, days=28, now=_NOW)
        assert result == {"unknown": {"behavior_pass": 2}}, result


# --------------------------------------------------------------------------- #
# DR-10 — non-dict top-level JSON must be skipped, not abort the run
# --------------------------------------------------------------------------- #
class TestDr10NonDictTopLevel:
    def test_list_top_level_skipped_not_crash(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        _write_report(reports, "nightly-bad.json", [1, 2, 3])  # valid JSON, wrong shape
        _write_report(
            reports, "nightly-good.json",
            _in_window([{"expected_skill": "aqg-x", "status": "behavior_pass"}]),
        )
        # pre-fix: AttributeError 'list' object has no attribute 'get'
        result = aga.aggregate_window(reports, days=28, now=_NOW)
        assert result == {"aqg-x": {"behavior_pass": 1}}, result  # good one survives

    def test_count_in_window_skips_non_dict(self, tmp_path: Path) -> None:
        # sibling path in the reports counter
        reports = tmp_path / "reports"
        _write_report(reports, "nightly-bad.json", "a bare string")
        _write_report(
            reports, "nightly-good.json",
            _in_window([{"expected_skill": "aqg-x", "status": "behavior_pass"}]),
        )
        count = aga._count_in_window_reports(reports, days=28, now=_NOW)
        assert count == 1, count  # only the dict-shaped report counts


# --------------------------------------------------------------------------- #
# audit 72e0206e (convergent gpt-5.5 + gemini): non-str `generated_at` is the
# sibling one field deeper. The crash claim was REFUTED locally — _parse_iso8601's
# `except (ValueError, AttributeError)` already absorbed every non-str value
# json.loads can produce (json never yields bytes) — but an explicit isinstance
# guard at the choke point makes the boundary self-documenting + refactor-proof.
# These pin the no-crash/skip CONTRACT; they pass both before and after the
# isinstance addition (they would RED only if _parse_iso8601 stopped tolerating
# non-str entirely), so they are contract guards, not stash-RED differentiators.
# --------------------------------------------------------------------------- #
class TestNonStrGeneratedAt:
    @pytest.mark.parametrize("bad_ts", [None, 123, 1.5, True, ["x"], {"k": 1}])
    def test_non_str_generated_at_skipped_in_aggregate(self, tmp_path: Path, bad_ts) -> None:
        reports = tmp_path / "reports"
        _write_report(reports, "nightly-1.json", {
            "schema_version": 1,
            "generated_at": bad_ts,
            "case_results": [{"expected_skill": "aqg-x", "status": "behavior_pass"}],
        })
        result = aga.aggregate_window(reports, days=28, now=_NOW)
        assert result == {}, result  # unparseable timestamp → report skipped, no crash

    @pytest.mark.parametrize("bad_ts", [None, 123, ["x"], {"k": 1}])
    def test_non_str_generated_at_skipped_in_count(self, tmp_path: Path, bad_ts) -> None:
        reports = tmp_path / "reports"
        _write_report(reports, "nightly-1.json", {"generated_at": bad_ts, "case_results": []})
        assert aga._count_in_window_reports(reports, days=28, now=_NOW) == 0

    def test_parse_iso8601_returns_none_for_non_str(self) -> None:
        for bad in (None, 123, 1.5, True, ["x"], {"k": 1}):
            assert aga._parse_iso8601(bad) is None, bad
        # a well-formed string still parses
        assert aga._parse_iso8601("2026-05-03T12:00:00Z") is not None


# --------------------------------------------------------------------------- #
# DR-13 — negative --window-days must be a usage error, not silent data loss
# --------------------------------------------------------------------------- #
class TestDr13NegativeWindow:
    def test_negative_window_is_usage_error(self, tmp_path: Path, capsys) -> None:
        reports = tmp_path / "reports"
        _write_report(
            reports, "nightly-1.json",
            {
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "case_results": [{"expected_skill": "aqg-x", "status": "behavior_pass"}],
            },
        )
        # pre-fix: rc=0 (EXIT_OK) with "(no skills)" — data silently dropped
        rc = aga.main(["--reports-dir", str(reports), "--window-days", "-1"])
        assert rc == aga.EXIT_USAGE, rc
        assert "window-days" in capsys.readouterr().err

    def test_zero_window_is_allowed(self, tmp_path: Path) -> None:
        # 0 is a degenerate but valid window (cutoff == now); not a usage error.
        reports = tmp_path / "reports"
        (reports).mkdir()
        rc = aga.main(["--reports-dir", str(reports), "--window-days", "0"])
        assert rc == aga.EXIT_OK, rc


# --------------------------------------------------------------------------- #
# DR-12 — absurd --window-days must clamp (include all), not overflow-crash
# --------------------------------------------------------------------------- #
class TestDr12HugeWindow:
    def test_huge_window_includes_all_no_crash(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        _write_report(
            reports, "nightly-1.json",
            _in_window([{"expected_skill": "aqg-x", "status": "behavior_pass"}]),
        )
        # pre-fix: OverflowError (Python int too large to convert to C int)
        result = aga.aggregate_window(reports, days=10**12, now=_NOW)
        assert result == {"aqg-x": {"behavior_pass": 1}}, result

    def test_subtraction_overflow_window_clamps(self, tmp_path: Path) -> None:
        # days within timedelta range but the SUBTRACTION underflows datetime.min.
        reports = tmp_path / "reports"
        _write_report(
            reports, "nightly-1.json",
            _in_window([{"expected_skill": "aqg-x", "status": "behavior_pass"}]),
        )
        result = aga.aggregate_window(reports, days=900000, now=_NOW)
        assert result == {"aqg-x": {"behavior_pass": 1}}, result

    def test_window_cutoff_helper_clamps_to_aware_min(self) -> None:
        cutoff = aga._window_cutoff(_NOW, 10**12)
        assert cutoff.tzinfo is not None  # aware, comparable to gen_at
        assert cutoff == datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# DR-17 — _chaos_redact cap is bytes, so it must slice bytes
# --------------------------------------------------------------------------- #
_TRUNC_MARKER = "\n... (truncated)"


class TestDr17ByteCap:
    def test_cjk_output_respects_byte_cap(self) -> None:
        cjk = "中" * 5000  # 15000 bytes
        out = chaos._chaos_redact(cjk, real_paths=())
        nbytes = len(out.encode("utf-8"))
        # pre-fix: out[:4096] chars of CJK ≈ 12304 bytes (3x over)
        assert nbytes <= chaos.MAX_STREAM_BYTES + len(_TRUNC_MARKER.encode("utf-8")), nbytes
        assert out.endswith(_TRUNC_MARKER)

    def test_truncation_does_not_split_multibyte_char(self) -> None:
        # result must be valid UTF-8 (errors="ignore" drops a partial trailing char)
        out = chaos._chaos_redact("中" * 5000, real_paths=())
        body = out[: -len(_TRUNC_MARKER)]
        assert "�" not in body  # no replacement char from a split sequence
        body.encode("utf-8")  # round-trips cleanly

    def test_short_ascii_unchanged(self) -> None:
        out = chaos._chaos_redact("hello world", real_paths=())
        assert out == "hello world"

    def test_ascii_at_cap_boundary_truncates(self) -> None:
        out = chaos._chaos_redact("a" * (chaos.MAX_STREAM_BYTES + 100), real_paths=())
        assert len(out.encode("utf-8")) <= chaos.MAX_STREAM_BYTES + len(_TRUNC_MARKER)
        assert out.endswith(_TRUNC_MARKER)

    def test_redaction_still_applied_before_cap(self) -> None:
        secret = "/Users/real/checkout"
        out = chaos._chaos_redact(f"path={secret} ok", real_paths=(secret,))
        assert secret not in out
        assert "<REDACTED-PATH:" in out

    def test_real_path_straddling_byte_cut_is_not_leaked(self) -> None:
        # audit 72e0206e concern #3: a real path positioned ON the 4 KiB cut
        # boundary must not leak a fragment. Redaction replaces it BEFORE the cap,
        # so the cut can only split the placeholder, never reveal the path.
        secret = "/Users/real/secret"
        text = "A" * (chaos.MAX_STREAM_BYTES - 6) + secret + "B" * 100
        out = chaos._chaos_redact(text, real_paths=(secret,))
        assert secret not in out
        assert len(out.encode("utf-8")) <= chaos.MAX_STREAM_BYTES + len(_TRUNC_MARKER)


# --------------------------------------------------------------------------- #
# DR-19 — _build_no_leak_set must tolerate an unresolvable home dir
# --------------------------------------------------------------------------- #
class TestDr19HomeGuard:
    def test_unresolvable_home_does_not_crash(self, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("Could not determine home directory")

        monkeypatch.setattr(chaos.Path, "home", staticmethod(_boom))
        # pre-fix: RuntimeError propagates out of leak-set setup
        leak_set = chaos._build_no_leak_set(("/scenario/token",))
        assert "/scenario/token" in leak_set  # scenario tokens still collected

    def test_normal_home_still_collected(self, monkeypatch) -> None:
        # audit 72e0206e f2: hermetic — pin a controlled home instead of the host's
        # real Path.home(), so this passes regardless of the runner's HOME/passwd.
        monkeypatch.setattr(chaos.Path, "home", staticmethod(lambda: Path("/home/tester")))
        leak_set = chaos._build_no_leak_set(("/scenario/tok",))
        assert "/home/tester" in leak_set  # real home still collected when resolvable
        assert "/scenario/tok" in leak_set  # and scenario tokens alongside it
