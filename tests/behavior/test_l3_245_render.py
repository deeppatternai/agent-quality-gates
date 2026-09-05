"""L3 #245 — render reconciliation banner (4-format) + typed-separation invariant.

ADR §4.2 / §6: the repo-reality banner is rendered in-band in every format from a
RepoReality passed as a SEPARATE presentation parameter. The invariant: a
RepoReality NEVER enters the ProjectView and NEVER changes `_defect_groups(view)`
or the defect projection — the banner is purely additive, sitting above the
defect section.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _sub in ("contracts",):
    _p = str(_ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ledger.projection import DefectEntry, ProjectView  # noqa: E402
from ledger.render import _defect_groups, render  # noqa: E402
from ledger.repo_reality import RepoReality  # noqa: E402

SINCE = "2026-06-01T12:00:00Z"


def _defect(did="D-1", status="open", severity="critical", title="Leak"):
    return DefectEntry(
        defect_id=did, status=status, severity=severity, title=title,
        first_seq=1, last_seq=2, occurred_at="2026-06-01T10:00:00Z", sources=("eaf",),
    )


def _view(*, event_count=2, defects=(), warnings=(), has_low_fidelity=False):
    return ProjectView(
        project_id="o/r", event_count=event_count, progress=(), defects=defects,
        handoffs=(), sources=(), last_activity=SINCE,
        has_low_fidelity=has_low_fidelity, warnings=warnings,
    )


def test_banner_renders_in_all_formats():
    view = _view(event_count=16, defects=(_defect(),))
    rr = RepoReality(commits=12, prs=3, notes=("(shallow clone — count may be truncated)",),
                     since=SINCE, truncated=False)
    for fmt in ("text", "markdown", "html"):
        out = render(view, fmt, repo_reality=rr)
        assert "12 commits" in out, (fmt, out)
        assert "3 PRs" in out, (fmt, out)
        assert "coverage gap" in out, (fmt, out)
        assert "shallow clone" in out, (fmt, out)
    data = json.loads(render(view, "json", repo_reality=rr))
    assert data["repo_reality"]["commits"] == 12
    assert data["repo_reality"]["prs"] == 3
    assert data["repo_reality"]["since"] == SINCE


def test_no_repo_reality_is_backward_compatible():
    view = _view(defects=(_defect(),))
    for fmt in ("text", "markdown", "html"):
        out = render(view, fmt)
        assert "coverage gap" not in out, fmt
    data = json.loads(render(view, "json"))
    assert "repo_reality" not in data            # default JSON shape unchanged


def test_typed_separation_banner_does_not_touch_defects():
    view = _view(event_count=16, defects=(_defect(),))
    rr = RepoReality(commits=12, prs=3, notes=(), since=SINCE, truncated=False)
    groups_before = _defect_groups(view)
    text_with = render(view, "text", repo_reality=rr)
    text_without = render(view, "text")
    groups_after = _defect_groups(view)
    # the view is never mutated by rendering a banner
    assert groups_before == groups_after
    assert not hasattr(view, "repo_reality")
    # the defect section (from "Open Defects" onward) is byte-identical — the banner
    # is additive and sits ABOVE it, so the projection output cannot be perturbed.
    cut = "== Open Defects"
    assert text_with[text_with.index(cut):] == text_without[text_without.index(cut):]
    assert "D-1" in text_with and "D-1" in text_without


def test_banner_unavailable_prs_distinct_from_zero():
    view = _view(event_count=1)
    rr = RepoReality(commits=12, prs=None, notes=("PRs: unavailable (not-authenticated)",),
                     since=SINCE, truncated=False)
    out = render(view, "text", repo_reality=rr)
    assert "12 commits" in out
    assert "PRs: unavailable" in out             # None ≠ a real 0
    assert "not-authenticated" in out
    data = json.loads(render(view, "json", repo_reality=rr))
    assert data["repo_reality"]["prs"] is None


def test_truncated_pr_count_renders_as_lower_bound():
    # gpt-f3: a truncated PR count is a lower bound → render ">=N PRs", not an exact N.
    view = _view(event_count=5)
    rr = RepoReality(commits=12, prs=1000, notes=("(PR count truncated at 1000)",),
                     since=SINCE, truncated=True)
    out = render(view, "text", repo_reality=rr)
    assert ">=1000 PRs" in out
    assert "truncated at 1000" in out
