"""RepoReality — opt-in repo-reality reconciliation data (issue #245).

PRESENTATION-ONLY. A RepoReality is NOT part of the deterministic ledger
projection: it NEVER becomes a ProjectView field and NEVER enters
`_defect_groups(view)` (the ADR §4.2 typed-separation invariant). It carries ONLY
integer counts + our own generated note strings — never any VCS free-text (commit
/ PR titles / bodies), which by construction removes the prompt-injection surface
(ADR §2 design reversal: the banner shows numbers, not text).

Collected by skills/aqg-project-status/scripts/collect_repo_reality.py (which owns
the git/gh subprocess, sanitize, and graceful degrade); rendered as an in-band
reconciliation banner by contracts/ledger/render.py. Pure stdlib — NO subprocess
here (this module stays a passive data + presentation-text contract).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepoReality:
    """Real repo activity counts since the ledger's last-activity timestamp.

    - `commits` / `prs`: int when the source succeeded; None when that source
      degraded (rendered as `unavailable (<reason>)`, kept DISTINCT from a real 0).
    - `notes`: OUR closed-set diagnostic strings (all-time / shallow / truncation /
      `unavailable (<closed-reason-code>)`). NEVER raw VCS text or raw stderr.
    - `since`: the UTC ISO timestamp the counts are measured from; None → all-time.
    - `truncated`: True when the PR query hit its row limit (count is a lower bound).
    """
    commits: "int | None"
    prs: "int | None"
    notes: "tuple[str, ...]"
    since: "str | None"
    truncated: bool = False


def reconciliation_lines(rr: "RepoReality", event_count: int) -> "tuple[str, ...]":
    """Build the reconciliation banner as plain text lines (numbers + our notes
    only — no VCS free-text). Format-agnostic: each renderer in render.py escapes
    + wraps these lines for its own format. The first line is the headline
    coverage-gap summary; any RepoReality.notes follow as their own lines.

    Examples:
      "ledger covers 16 events; git shows 12 commits / 3 PRs since
       2026-06-03T12:00:00Z — coverage gap: absence of an event != no work"
      "ledger covers 1 event; git shows 12 commits / PRs: unavailable
       (not-authenticated) (all-time) — coverage gap: absence of an event != no work"
    """
    commits = str(rr.commits) if rr.commits is not None else "unavailable"
    commits_frag = f"{commits} commits"
    if rr.prs is None:
        prs_frag = "PRs: unavailable"
    elif rr.truncated:
        prs_frag = f">={rr.prs} PRs"   # a truncated count is a lower bound (audit gpt-f3)
    else:
        prs_frag = f"{rr.prs} PRs"
    window = f"since {rr.since}" if rr.since else "all-time"
    events = f"{event_count} event" + ("" if event_count == 1 else "s")
    headline = (
        f"ledger covers {events}; git shows {commits_frag} / {prs_frag} {window} "
        "— coverage gap: absence of an event != no work"
    )
    return (headline, *rr.notes)
