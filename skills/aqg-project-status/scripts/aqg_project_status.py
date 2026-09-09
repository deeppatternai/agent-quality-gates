#!/usr/bin/env python3
"""Render a project's AQG ledger as a human-facing status report.

Repo/production read-only — but NOT literally write-free: to present current state
it drains the local ledger inbox into the local events.jsonl on view (`--no-drain`
to skip). It never writes a report file or touches the user's repo / production.

Loads the project's append-only events.jsonl, projects it into a ProjectView
(contracts/ledger/projection.py), and renders it (contracts/ledger/render.py).
Default output is HTML (DesignSpec §5.8). The script is canonical English
(§5.7) — translating the report to a requested language is a presentation-layer
step the skill's caller (the LLM) does on the rendered output, not here.

To present CURRENT state, the script first drains the local ledger inbox into the
local append-only events.jsonl (a local, flock-guarded, never-fail consumer step —
events auto-pushed by the EAF exporter / the v2 capture hook reach the report this
way). `--no-drain` skips it for a strictly-read view. The drain is the ONLY write,
and it stays inside the local ledger: the report is written ONLY to stdout (redirect
to save — `... --format html > report.html`); the skill never writes a report file
and never touches upstream / production / secrets / the user's repo (audit ed629637
f1: a built-in --output flag was an unguarded write vector).

Exit codes:
  0: success — report rendered to stdout (an empty / missing ledger is still a
     successful, empty report)
  1: reserved — this skill surfaces ledger state in the report, not via exit code
  2: usage error — argparse exit 2 on bad args, incl. --json combined with
     --format (mutually exclusive); also the How-To-Run wrapper's exit when it
     cannot source _aqg_context.sh
  3: config error — cannot resolve the AQG contracts (AQG_ROOT / partial checkout)
     or the project_id
  70: backstop — unexpected internal error while building the report
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path


def _aqg_root() -> Path:
    """Use this sourced invocation's physical pin, otherwise the explicit root."""
    physical = Path(__file__).resolve().parents[3]
    pinned = os.environ.get("AQG_SKILL_ROOT")
    if pinned and Path(pinned).expanduser() == physical:
        return physical
    env = os.environ.get("AQG_ROOT")
    if env:
        return Path(env).expanduser()
    return physical


def _bootstrap_contracts() -> None:
    """Put <aqg_root>/contracts on sys.path so `import ledger.*` resolves. Exit 3
    with an actionable message if the contracts package is not found."""
    contracts = _aqg_root() / "contracts"
    if not (contracts / "ledger" / "projection.py").is_file():
        sys.stderr.write(
            f"ERROR: cannot find the AQG ledger contracts under {contracts}. "
            "Fix: export AQG_ROOT=/path/to/agent-quality-gates checkout.\n"
        )
        raise SystemExit(3)
    contracts_s = str(contracts)
    sys.path[:] = [p for p in sys.path if p != contracts_s]
    sys.path.insert(0, contracts_s)

    loaded = sys.modules.get("ledger")
    if loaded is None:
        return
    origins = []
    loaded_file = getattr(loaded, "__file__", None)
    if loaded_file:
        origins.append(Path(loaded_file).resolve())
    origins.extend(Path(p).resolve() for p in getattr(loaded, "__path__", []) or [])
    contracts_root = contracts.resolve()
    if origins and any(_outside_contracts(origin, contracts_root) for origin in origins):
        for name in list(sys.modules):
            if name == "ledger" or name.startswith("ledger."):
                del sys.modules[name]


def _outside_contracts(path: Path, contracts_root: Path) -> bool:
    try:
        path.relative_to(contracts_root)
    except ValueError:
        return True
    return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aqg_project_status.py",
        description="Render a project's AQG ledger as a human status report "
                    "(repo/production read-only; drains the local ledger on view, --no-drain to skip).",
    )
    parser.add_argument(
        "--repo", default=".",
        help="repo path to resolve project_id from (default: cwd); ignored when --project-id is set",
    )
    parser.add_argument(
        "--project-id", help="explicit project_id (overrides --repo resolution)",
    )
    # --format and --json are mutually exclusive: passing both is a usage error
    # (exit 2) rather than a silent override (audit ed629637 f3). --format has no
    # argparse default so the group stays unset when neither is given; main()
    # applies the html default afterwards.
    fmt_group = parser.add_mutually_exclusive_group()
    fmt_group.add_argument(
        "--format", choices=["html", "markdown", "text", "json"],
        help="output format (default: html, per DesignSpec §5.8)",
    )
    fmt_group.add_argument(
        "--json", action="store_true",
        help="shortcut for --format json (machine-readable ProjectView)",
    )
    parser.add_argument(
        "--no-drain", action="store_true",
        help="skip draining the local inbox before rendering (strictly-read view of "
             "already-stored events; by default the report drains first so it reflects "
             "events auto-pushed since the last drain)",
    )
    parser.add_argument(
        "--with-repo-reality", action="store_true",
        help="opt-in: append an in-band repo-reality reconciliation banner — REAL "
             "commit/PR counts since the ledger's last activity (git rev-list local + "
             "gh pr list when available; read-only, bounded timeout, degrades to a note). "
             "Off by default → zero git/gh subprocess (the report stays purely local).",
    )
    # §4.2 injection-proof translation (language-follows). The script is the
    # DETERMINISTIC security gate; the LLM translation is the calling session's step.
    tr_group = parser.add_mutually_exclusive_group()
    tr_group.add_argument(
        "--emit-translation-segments", action="store_true",
        help="emit the producer free-text segments to translate (JSON {project_id, "
             "segments}) and exit — the ONLY thing the translation LLM should see (no "
             "numbers/status/dates). The session translates them, then re-runs with "
             "--apply-translation.",
    )
    tr_group.add_argument(
        "--apply-translation", metavar="FILE",
        help="apply a session-produced translations file (JSON {segments, "
             "translations, target_lang}) under the §4.2 fact-token guard: a "
             "translation that fabricates/tampers a number/%%/date/status → rejected, "
             "English canonical delivered. Pair with --lang.",
    )
    parser.add_argument(
        "--lang", metavar="CODE",
        help="target language code for --apply-translation (e.g. zh, es); falls back "
             "to the file's target_lang when omitted",
    )
    return parser


def _drain_inbox() -> list[str]:
    """Drain the local ledger inbox into each project's events.jsonl so the report
    reflects events pushed since the last drain (EAF exporter / v2 capture hook).

    A LOCAL consumer step: append-only into the local ledger, single-consumer
    flock-guarded (a concurrent drain is safe — one wins, the rest skip), never-fail.
    `consume_inbox` does not raise (lock-held / poison / I/O are reported, not thrown);
    the broad guard is a belt-and-suspenders backstop so a partial-checkout import or
    any unexpected error degrades to "render the already-stored state" rather than
    failing the report.

    Returns data-quality note strings for any events that did NOT make it into this
    view — lock-skipped / deferred / dead-lettered, or a drain that errored. Each
    note is mirrored to stderr (the existing pipe/log channel); main() ALSO renders
    them in-band in the report so a consumer who only reads the report (redirected /
    piped) still sees that events were dropped (#183 B7; EAF telemetry_errors)."""
    from datetime import datetime, timezone

    notes: list[str] = []
    try:
        from ledger.store import consume_inbox

        report = consume_inbox(recorded_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:  # aqg: top-level boundary — a drain hiccup must not block the report
        notes.append(f"inbox drain skipped ({exc}); rendering stored state")
    else:
        # consume_inbox REPORTS lock-held / deferred / dead-lettered outcomes (it does
        # not raise them), so the broad guard above would miss them — surface each so a
        # silently-stale or poison-dropping drain is visible. The report still renders.
        if report.skipped_locked:
            notes.append(
                "inbox drain skipped — another consumer holds the lock; "
                "rendering current stored state"
            )
        if report.deferred:
            notes.append(
                f"{len(report.deferred)} inbox event(s) deferred (transient I/O); "
                "they will drain on a later view"
            )
        if report.failed:
            notes.append(
                f"{len(report.failed)} inbox event(s) dead-lettered (unparseable / "
                "invalid schema); see the ledger failed/ dir"
            )

    for note in notes:
        sys.stderr.write(f"note: {note}.\n")
    return notes


def _collect_repo_reality(repo: str, since: "str | None"):
    """Opt-in repo-reality collection (issue #245). LAZY-imports the collector so the
    DEFAULT path never imports it and never spawns a git/gh subprocess (acceptance #1).

    `since` is the ledger's last-activity timestamp (UTC per ledger convention; None →
    all-time). ALWAYS returns a RepoReality — repo-reality is presentation-only and must
    NEVER block or fail the report (ADR §4.3). The collector itself never raises; this
    guard catches a partial-checkout import failure and still surfaces it IN-BAND as an
    `unavailable (collector-error)` banner rather than silently vanishing (audit gpt-f1)."""
    try:
        this_dir = str(Path(__file__).resolve().parent)
        if this_dir not in sys.path:
            sys.path.insert(0, this_dir)
        from collect_repo_reality import collect_repo_reality

        return collect_repo_reality(Path(repo), since)
    except Exception as exc:  # aqg: top-level boundary — repo-reality never blocks the report
        sys.stderr.write(
            f"note: repo-reality collection skipped ({type(exc).__name__}); "
            "rendering ledger-only report.\n"
        )
        # gpt-f1: still surface IN-BAND so a report-only consumer sees the skip — the
        # banner renders with an unavailable note rather than silently vanishing.
        from ledger.repo_reality import RepoReality

        return RepoReality(
            commits=None,
            prs=None,
            notes=("repo-reality: unavailable (collector-error)",),
            since=since,
            truncated=False,
        )


def _load_translations(path: str) -> dict:
    """Read the session's {segments, translations, target_lang} file. Defensive: a
    missing / malformed / non-object file yields {} → the fact-token gate fails
    closed to English canonical (never crashes)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _render_translated(view, fmt, now, args) -> str:
    """§4.2 apply path: replay the session's translations through the deterministic
    fact-token guard (translate_report). The guard runs REGARDLESS of who produced
    the translations — a fabricated fact, a stale/incomplete file, or a wrong-shape
    map all fall back to the English canonical render. Notes go to stderr; the
    delivered report is always safe."""
    from ledger.translate import translate_report

    provided = _load_translations(args.apply_translation)
    # Schema-validate the file's TYPES, not just that it is a JSON object: a
    # non-string target_lang or a non-list segments/translations must degrade to the
    # English canonical, never raise (audit c6050ee7 gpt-f2 / gemini-f4 / grok-f5).
    segs = provided.get("segments")
    trans = provided.get("translations")
    file_lang = provided.get("target_lang")
    if not (isinstance(segs, list) and isinstance(trans, list)):
        segs, trans = [], []
    target = (args.lang or (file_lang if isinstance(file_lang, str) else "") or "").strip()
    mapping = {s: t for s, t in zip(segs, trans) if isinstance(s, str)}

    def session_translator(current_segments, _lang):
        # an unmatched current segment → None → translate_report's shape check fails
        # closed (a stale / incomplete file delivers English, never a half-translation).
        return [mapping.get(s) for s in current_segments]

    result = translate_report(view, fmt, target_lang=target, translator=session_translator, now=now)
    if result.fell_back:
        sys.stderr.write(
            "note: translation withheld, delivering English canonical (§4.2 fact "
            f"guard); reasons: {'; '.join(result.violations[:5])}\n"
        )
    elif not result.translated and target:
        sys.stderr.write(f"note: nothing to translate for '{target}'; English canonical.\n")
    return result.report


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    _bootstrap_contracts()
    try:
        from ledger.paths import project_events_path
        from ledger.project_id import project_id_from_repo
        from ledger.projection import project_log
        from ledger.render import render
    except ImportError as exc:
        # _bootstrap_contracts confirmed projection.py exists, but a partial /
        # corrupt checkout can still miss a sibling module (audit ed629637 f2).
        sys.stderr.write(
            f"ERROR: incomplete AQG ledger contracts ({exc}). "
            "Fix: re-checkout / export AQG_ROOT=/path/to/agent-quality-gates.\n"
        )
        return 3

    fmt = "json" if args.json else (args.format or "html")

    try:
        project_id = args.project_id or project_id_from_repo(args.repo)
    except Exception as exc:  # noqa: BLE001 - resolution must not crash the CLI
        sys.stderr.write(f"ERROR: cannot resolve project_id: {exc}\n")
        return 3

    # Drain the inbox so the report reflects current state (events auto-pushed since
    # the last drain). Never-fail + flock-guarded; --no-drain → strictly-read view.
    # drain_notes describe events that did NOT make it in (lock / deferred / poison /
    # drain error) — rendered in-band below so a report-only consumer sees the drop.
    drain_notes = _drain_inbox() if not args.no_drain else []

    events_path = project_events_path(project_id)
    # Data-quality notes surface BOTH on stderr (existing channel) AND in-band in the
    # report, so a consumer reading only the report (redirected / piped) still sees
    # them — the #183 B7 pattern. The "no ledger yet" note was previously stderr-only;
    # it is folded in here too (G3 / acceptance #5).
    data_quality_notes = list(drain_notes)
    if not events_path.is_file():
        no_ledger = (
            f"no ledger yet for '{project_id}' (looked at {events_path}); "
            "rendering an empty report. Pass --project-id if this is the wrong project"
        )
        sys.stderr.write(f"note: {no_ledger}.\n")
        data_quality_notes.append(no_ledger)

    try:
        view = project_log(events_path, project_id=project_id)
        # Prepend the data-quality notes to the view's warnings (frozen view → a new
        # copy). Prepended so these few, important notes are never truncated by render's
        # _MAX_WARNINGS cap, and surface in every format incl. JSON.
        if data_quality_notes:
            view = replace(view, warnings=tuple(data_quality_notes) + view.warnings)
    except Exception as exc:  # aqg: top-level boundary — projection is defensive; backstop only
        sys.stderr.write(f"ERROR: failed to build report: {exc}\n")
        return 70

    # Opt-in repo-reality (issue #245): collected AFTER the view (its `since` is the
    # view's last-activity timestamp) in its OWN guard, so a collection hiccup degrades
    # to a ledger-only report — never a non-zero exit (the report must always render,
    # ADR §4.3). repo_reality is a SEPARATE presentation parameter to render(); it is
    # never a field of `view` and never feeds the defect projection (typed-separation
    # invariant, ADR §4.2). Default path: this whole block is skipped → zero subprocess.
    # `now` = report-generation time, the anchor for the business report's time-based
    # signals (health aged-decision / per-source staleness, a4 §5.5/§6.5). Real wall
    # clock here (the report reflects "as of now"); render() injects it so the renderer
    # itself stays deterministic + unit-testable with a fixed now.
    from datetime import datetime, timezone
    report_now = datetime.now(timezone.utc)

    # §4.2 step 1: emit the producer free-text segments for the session to translate
    # (the ONLY thing the LLM sees — no facts). Format-independent; ignores --format.
    if args.emit_translation_segments:
        from ledger.translate import collect_segments
        segments = collect_segments(view)
        sys.stdout.write(json.dumps(
            {"project_id": project_id, "segments": list(segments)},
            ensure_ascii=False, indent=2,
        ) + "\n")
        return 0

    repo_reality = _collect_repo_reality(args.repo, view.last_activity) if args.with_repo_reality else None

    # §4.2 step 3: apply a session-produced translation under the deterministic
    # fact-token guard (any fabricated number/%/date/status → English canonical).
    if args.apply_translation:
        try:
            report = _render_translated(view, fmt, report_now, args)
        except Exception as exc:  # aqg: top-level boundary — translation must fail closed
            sys.stderr.write(f"ERROR: failed to build translated report: {exc}\n")
            return 70
    else:
        try:
            report = render(view, fmt, repo_reality=repo_reality, now=report_now)
        except Exception as exc:  # aqg: top-level boundary — render is defensive; backstop only
            sys.stderr.write(f"ERROR: failed to build report: {exc}\n")
            return 70

    # stdout only — never writes a file (so it can never clobber the ledger or a
    # forbidden path). Redirect stdout to save: `... > report.html`.
    sys.stdout.write(report if report.endswith("\n") else report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
