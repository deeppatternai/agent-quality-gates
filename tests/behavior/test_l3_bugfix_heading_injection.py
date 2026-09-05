"""L3-6 WB-03 — column-0 markdown heading injection via inline bugfix fields.

`bugfix_record._render_markdown` emits `symptom` (line ~181) and
`regression_coverage` (line ~197) RAW at column 0 — unlike `root_cause` / `fix` /
`verification` / `boundaries`, which are wrapped in `_fence_multiline`, and unlike
`title` / `affected_area`, which interpolate mid-line. The records land in
`docs/bugfixes/*.md` (git-committed, potentially public after launch).

`scan_leaks`' markdown-heading guard is gated on `multiline=True` and never
covered code fences at all. The two inline fields are validated with
`multiline=False`, so a SINGLE-LINE column-0 block opener (no newline) passed
`check_bugfix_record` and corrupted the rendered doc:
- `"## Injected"` → forged `##` heading at column 0;
- `"```"` / `"~~~"` → fenced-code opener that swallows the following template
  sections (audit c200595b convergent — heading-only was incomplete).
Only the MULTI-line variant (`"a\n## fake"`) was caught — by the inline newline
guard, not the block guard. (`<!--` and URLs are already rejected by scan_leaks.)

Fix: `_starts_markdown_block` guard for the two column-0 inline fields in
`check_bugfix_record` — ATX-precise heading (`#` then space/EOL, so `#42` is
allowed) + fenced-code openers. Low-impact single-line markers (>, -, *, |, 1.)
are deliberately allowed to avoid FP on prose. Stash-proven RED:
`git stash push -- scripts/_bugfix_redaction.py` -> the injection cases pass
is_safe again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _bugfix_redaction as br  # noqa: E402
import bugfix_record as rec  # noqa: E402

_BASE = {
    "schema_version": 1, "slug": "test-bug-fix", "date": "2026-05-03", "actor": "claude",
    "title": "Test bug fix", "affected_area": "scripts / docs", "severity": "medium",
    "backward_compatible": "yes", "symptom": "Test failed under condition X",
    "root_cause": "Off-by-one", "fix": "Adjusted bound", "verification": "pytest -> ok",
    "regression_coverage": "tests/test_x.py::TestX::test_off_by_one",
    "boundaries": "no production touched", "taxonomy": ["algorithm"],
    "regression": False, "marker": "auto-generated-by-bugfix_record",
}


def _record(**over) -> dict:
    return dict(_BASE, **over)


class TestColumn0BlockInjection:
    # audit c200595b convergent: the guard must cover BOTH ATX headings and
    # fenced-code openers (the latter swallows following template sections).
    @pytest.mark.parametrize("field", ["symptom", "regression_coverage"])
    @pytest.mark.parametrize("payload", [
        "## Injected Section", "# H1 Forged", "###### h6", "   ## indented",  # headings
        "```", "~~~", "```python", "~~~~",                                     # code fences
    ])
    def test_leading_block_marker_inline_field_rejected(self, field: str, payload: str) -> None:
        # pre-fix: is_safe=True (forged heading / fence reaches the git-committed doc)
        result = br.check_bugfix_record(_record(**{field: payload}))
        assert not result.is_safe, (field, payload)
        assert any("markdown block" in v for v in result.violations), result.violations

    @pytest.mark.parametrize("field", ["symptom", "regression_coverage"])
    def test_benign_inline_value_still_passes(self, field: str) -> None:
        assert br.check_bugfix_record(_record(**{field: "normal prose, no markdown"})).is_safe

    def test_hash_not_at_start_is_allowed(self) -> None:
        # a '#' mid-value is not a heading at column 0 — must not over-reject
        assert br.check_bugfix_record(_record(symptom="see issue #42 for the repro")).is_safe

    def test_issue_number_at_col0_is_allowed(self) -> None:
        # audit f2: ATX-precise — `#42` is `#` not followed by space → NOT a heading,
        # so a column-0 `#42 reproduces` is legitimate prose and must pass.
        assert br.check_bugfix_record(_record(symptom="#42 reproduces on cold start")).is_safe

    @pytest.mark.parametrize("payload", ["> 100ms latency", "- crashes on boot", "1. run X", "* note"])
    def test_low_impact_markers_intentionally_allowed(self, payload: str) -> None:
        # deliberate scoping: single-line blockquote/list markers are low-impact and
        # FP-prone on legitimate prose, so they are NOT rejected (only heading + fence).
        assert br.check_bugfix_record(_record(symptom=payload)).is_safe

    def test_multiline_injection_still_rejected(self) -> None:
        # unchanged: the inline newline guard already caught the multi-line variant
        assert not br.check_bugfix_record(_record(symptom="line1\n## fake heading")).is_safe


class TestMidLineFieldsNotOverRejected:
    # title / affected_area interpolate MID-LINE in the render
    # (`# Bug Fix Record: <title>`, `- affected area: <aa>`), so a leading '#'
    # is never a column-0 heading there — the fix deliberately does NOT touch them.
    @pytest.mark.parametrize("field", ["title", "affected_area"])
    def test_leading_hash_midline_field_allowed(self, field: str) -> None:
        assert br.check_bugfix_record(_record(**{field: "#42 hotfix"})).is_safe


class TestEndToEndRefusesWrite:
    def test_assert_safe_raises_on_injection(self) -> None:
        with pytest.raises(br.BugfixRedactionError):
            br.assert_safe_bugfix_record(_record(symptom="## Injected"))

    def test_render_of_benign_record_has_no_extra_heading(self) -> None:
        # the benign symptom renders as its own column-0 line but is NOT a heading
        body = rec._render_markdown(_record(symptom="plain symptom"))
        assert "\nplain symptom\n" in body
        # the only '## ' headings are the template's fixed sections
        injected = [ln for ln in body.splitlines() if ln.startswith("#") and "plain symptom" in ln]
        assert injected == []
