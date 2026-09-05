#!/usr/bin/env python3
"""Generate an evidence closeout skeleton for current project work.

PR-D extension: optionally auto-imports the AQG code construction ledger
(`.aqg/current_ledger.md`) and prints a structured "Code Construction Evidence"
section so closeout doesn't require the owner to re-fill what construction
already captured. Secret patterns in the ledger are redacted in output.

Per PR-A audit D6 deferral + sketch a1: import covers parsed header (task_slug,
path tier, created_at, session_agent, skipped_checks count), 6-step table rows,
Warning Acknowledgements section, and Predicted Objections section.

Backwards compatible: if no construction ledger present (or construction parser
unavailable), the script behaves exactly like v1 (skeleton only).

Transfer Test Pack v1 integration (Q7 from sketch a2 §8): if a Transfer Test
Pack run summary is found, closeout adds a 7th evidence-ledger line surfacing
the threshold_met state. On failure the line is rendered prominently; on pass
it is rendered concisely. Candidate paths are checked in order:
  1. <repo>/.aqg/transfer/last_run_summary.yaml  (canonical)
  2. <repo>/run_summary.yaml                     (README quickstart default)
  3. <repo>/tests/transfer/_last_run_summary.yaml (alternative)
Override via `--transfer-summary <path>`; disable via `--no-transfer-import`.

Exit codes (audit 6529b5d4 P1: closeout ALWAYS exits 0 by design — it is a
reporter, not a gate; missing/malformed/inconsistent evidence is surfaced as a
not-done / FAIL row in the OUTPUT, so automation must read the rendered rows,
not the exit status):
  0: success — closeout skeleton generated; ledger imported if present
  1: reserved — closeout surfaces missing evidence in output, does not raise
  2: usage error — argparse propagates exit 2 on unknown args / bad paths
  3: reserved — config or schema error (not currently emitted)
  70: reserved — internal error placeholder for future use
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


def run(cmd: list[str], timeout: int = 30) -> str:
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        out = proc.stdout.strip()
        return f"exit={proc.returncode}\n{out}"
    except Exception as exc:  # noqa: BLE001
        return f"error={type(exc).__name__}: {exc}"


def git_status(repo: Path) -> str:
    if not repo.exists():
        return f"missing: {repo}"
    return run(["git", "-C", str(repo), "status", "--short", "--branch"])


def find_git_root(start: Path) -> Path:
    result = run(["git", "-C", str(start), "rev-parse", "--show-toplevel"])
    lines = result.splitlines()
    if lines and lines[0] == "exit=0" and len(lines) >= 2 and lines[1]:
        return Path(lines[1])
    return start.resolve()


# ===== PR-D: optional construction ledger import =====
# Required attribute set the parser module must export (audit D1 verification).
_CONSTRUCTION_REQUIRED_API = (
    "_parse_ledger_header",
    "_parse_ledger_steps",
    "_check_secrets",
    "SECRET_PATTERNS",
)
# v0.14.0 Behavior Contract is accessed DEFENSIVELY (getattr), NOT added to the
# required API — so a 0.14.0 closeout paired with a pre-0.14.0 checker still
# imports the core construction ledger (BC render just degrades to skip).


def _try_import_construction_parser():
    """Load construction parser via importlib (PR-D audit D1 fix: pin to file path).

    Avoids sys.path mutation + sys.modules cache shadow by loading the exact
    file via importlib.util. Verifies expected API is present before returning.

    Implementation note: must register module in sys.modules BEFORE exec_module
    so the @dataclass decorator can resolve cls.__module__ → cls.__dict__
    (Python 3.9+ dataclass internals).
    """
    aqg_root = Path(__file__).resolve().parents[3]
    parser_file = (
        aqg_root / "skills" / "aqg-code-construction" / "scripts" / "aqg_construction_check.py"
    )
    if not parser_file.is_file():
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "aqg_construction_check_pinned", str(parser_file)
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module — required for @dataclass to work
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - import errors are diagnostic, not crash
        sys.modules.pop(spec.name, None)
        return None
    for attr in _CONSTRUCTION_REQUIRED_API:
        if not hasattr(module, attr):
            sys.modules.pop(spec.name, None)
            return None
    return module


_CONSTRUCTION = _try_import_construction_parser()


# ===== PR-D audit C3 fix: differentiated import status =====
# Status enum returned by import_construction_ledger so main() can render
# accurate notices (parser_unavailable vs missing vs read_error vs malformed).
class LedgerImportStatus:
    OK = "ok"
    PARSER_UNAVAILABLE = "parser_unavailable"
    MISSING = "missing"
    READ_ERROR = "read_error"
    MALFORMED = "malformed"


# Batch-2 audit 6529b5d4 C4: local fallback secret patterns (distinctive-prefix,
# full-token capture) so redaction is fail-CLOSED even when the construction
# parser is absent. Prefer the construction patterns when available (they are the
# single source of truth, kept in sync), else fall back to this local set.
_LOCAL_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "AWS temporary key"),
    (re.compile(r"gh[posur]_[A-Za-z0-9_]{36,}"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "GitHub fine-grained PAT"),
    (re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{24,}"), "Stripe API key"),
    (re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}"), "OpenAI project key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI API key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "PEM private key",
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=+/-]{10,}"
        ),
        "JWT",
    ),
]


def _active_secret_patterns() -> list[tuple[re.Pattern, str]]:
    """Resolve the secret-pattern set at call time (so it tracks _CONSTRUCTION).

    Construction patterns are the source of truth when the parser is importable;
    otherwise the local fallback keeps redaction fail-closed (audit 6529b5d4 C4).
    """
    if _CONSTRUCTION is not None:
        return _CONSTRUCTION.SECRET_PATTERNS
    return _LOCAL_SECRET_PATTERNS


def redact_secrets(text: Optional[str]) -> str:
    """Replace secret patterns with [REDACTED:<type>] markers (PR-D safety).

    Fail-CLOSED (audit 6529b5d4 C4): always applies a pattern set — the
    construction patterns when available, else the local fallback — so a missing
    construction parser can never silently pass a raw secret through.
    Patterns capture the FULL token (BEGIN..END for PEM keys), so `re.sub`
    replaces the whole secret, not just a header marker.
    """
    if text is None:
        return ""
    redacted = text
    for pattern, name in _active_secret_patterns():
        redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
    return redacted


def _safe_cell(text: Any, max_len: int = 60) -> str:
    """The single sanitizer for any imported/untrusted value rendered into
    output: coerce to str, redact secrets, THEN escape table metacharacters +
    truncate (audit 6529b5d4 C1/C2: redact BEFORE truncate so the regex sees the
    full token; escape pipes/newlines so a cell cannot corrupt the table)."""
    if text is None:
        return ""
    return _truncate(redact_secrets(str(text)), max_len)


def import_construction_ledger(ledger_path: Path) -> dict[str, Any]:
    """Parse construction ledger; return status dict (PR-D audit C3 fix).

    Always returns a dict with `status` field. When status == "ok", `data` is
    populated with parsed sections. Other statuses carry `detail` for the
    user-facing notice.
    """
    if _CONSTRUCTION is None:
        return {
            "status": LedgerImportStatus.PARSER_UNAVAILABLE,
            "data": None,
            "detail": "construction parser module not available (verify skills/aqg-code-construction/scripts/aqg_construction_check.py exists + exports required API)",
        }
    if not ledger_path.exists():
        return {
            "status": LedgerImportStatus.MISSING,
            "data": None,
            "detail": f"no file at {ledger_path}",
        }
    try:
        content = ledger_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "status": LedgerImportStatus.READ_ERROR,
            "data": None,
            "detail": f"cannot read {ledger_path}: {type(exc).__name__}: {exc}",
        }
    header = _CONSTRUCTION._parse_ledger_header(content)
    if header is None:
        return {
            "status": LedgerImportStatus.MALFORMED,
            "data": None,
            "detail": "ledger yaml frontmatter missing or invalid (required: task_slug, path, created_at, session_agent)",
        }
    steps = _CONSTRUCTION._parse_ledger_steps(content)
    warn_match = re.search(
        r"##\s*Warning Acknowledgements.*?\n(.*?)(?=\n##\s|\Z)", content, re.DOTALL
    )
    obj_match = re.search(
        r"##\s*Predicted Objections.*?\n(.*?)(?=\n##\s|\Z)", content, re.DOTALL
    )
    # v0.14.0: parse the Behavior Contract via the SHARED parser (single source of
    # truth — no second parser to drift). Accessed defensively so an older checker
    # (no parse_behavior_contract) degrades to a skipped BC section, not a crash.
    _bc_parser = getattr(_CONSTRUCTION, "parse_behavior_contract", None)
    behavior_contract = _bc_parser(content) if _bc_parser is not None else None
    return {
        "status": LedgerImportStatus.OK,
        "data": {
            "header": header,
            "steps": steps,
            "warning_section": redact_secrets(warn_match.group(1).strip()) if warn_match else "",
            "objection_section": redact_secrets(obj_match.group(1).strip()) if obj_match else "",
            "behavior_contract": behavior_contract,
            "ledger_path": str(ledger_path),
        },
        "detail": "ok",
    }


# ===== Transfer Test Pack v1 integration (Q7 from sketch a2 §8) =====


class TransferImportStatus:
    """Status enum for `import_transfer_summary` parallel to LedgerImportStatus.

    INCONSISTENT (audit d4598abf #1): summary parses but `threshold_met` flag
    contradicts the displayed numerics (e.g. `threshold_met: true` with
    `overall_pass_rate: 0.5` and `threshold_strict: 1.0`). Treated like
    MALFORMED in render — surfaces a TODO row with diagnostic, never a PASS.
    """
    OK_PASSED = "ok_passed"
    OK_FAILED = "ok_failed"
    NOT_FOUND = "not_found"
    READ_ERROR = "read_error"
    MALFORMED = "malformed"
    INCONSISTENT = "inconsistent"


_TRANSFER_SUMMARY_CANDIDATES = (
    Path(".aqg") / "transfer" / "last_run_summary.yaml",
    Path("run_summary.yaml"),
    Path("tests") / "transfer" / "_last_run_summary.yaml",
)


def _resolve_transfer_summary_path(
    explicit: Optional[Path], repo: Path
) -> Optional[Path]:
    """Find the first existing Transfer Test Pack run summary.

    Preference order: explicit `--transfer-summary` > canonical `.aqg/transfer/`
    > `run_summary.yaml` > `tests/transfer/`. Returns None if none exist.
    """
    if explicit is not None:
        return explicit
    for rel in _TRANSFER_SUMMARY_CANDIDATES:
        candidate = repo / rel
        if candidate.is_file():
            return candidate
    return None


def import_transfer_summary(
    summary_path: Optional[Path],
    *,
    was_explicit: bool = False,
) -> dict[str, Any]:
    """Parse a Transfer Test Pack run summary; return status dict.

    Lightweight parse — extracts only the fields needed for the 7th-line
    rendering (run_id, overall_pass_rate, threshold_met, threshold_strict,
    task5_advisory, date). Does NOT cross-import validate_transfer_record_v1
    schema validator: closeout is a reporter, must not fail on schema drift
    in upstream summary file.

    Audit d4598abf #1: also runs local consistency checks on the rendered
    fields (numeric type, range, threshold_met vs overall_pass_rate). A
    mismatch returns INCONSISTENT — never silently OK_PASSED on contradictory
    inputs.

    Audit d4598abf #3: NOT_FOUND result carries `summary_path` + `was_explicit`
    so the renderer can emit accurate diagnostics for explicit-vs-auto cases.
    """
    if summary_path is None:
        return {
            "status": TransferImportStatus.NOT_FOUND,
            "data": None,
            "detail": "no Transfer Test Pack summary found at known candidate paths",
            "summary_path": None,
            "was_explicit": False,
        }
    if not summary_path.exists():
        return {
            "status": TransferImportStatus.NOT_FOUND,
            "data": None,
            "detail": f"no file at {summary_path}",
            "summary_path": str(summary_path),
            "was_explicit": was_explicit,
        }
    try:
        text = summary_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "status": TransferImportStatus.READ_ERROR,
            "data": None,
            "detail": f"cannot read {summary_path}: {type(exc).__name__}: {exc}",
        }
    try:
        import yaml  # type: ignore[import-untyped]
        loaded = yaml.safe_load(text)
    except ImportError:
        return {
            "status": TransferImportStatus.MALFORMED,
            "data": None,
            "detail": "PyYAML unavailable; install pyyaml to enable transfer-pack import",
        }
    except Exception as exc:  # noqa: BLE001 — yaml errors subclass differently
        return {
            "status": TransferImportStatus.MALFORMED,
            "data": None,
            "detail": f"yaml parse error: {type(exc).__name__}: {exc}",
        }
    if not isinstance(loaded, dict):
        return {
            "status": TransferImportStatus.MALFORMED,
            "data": None,
            "detail": f"top-level must be mapping, got {type(loaded).__name__}",
        }
    threshold_met = loaded.get("threshold_met")
    if not isinstance(threshold_met, bool):
        return {
            "status": TransferImportStatus.MALFORMED,
            "data": None,
            "detail": f"threshold_met must be bool, got {type(threshold_met).__name__}",
        }
    overall_pass_rate = loaded.get("overall_pass_rate")
    threshold_strict = loaded.get("threshold_strict")

    # audit d4598abf #1: numeric + range + consistency checks
    if not isinstance(overall_pass_rate, (int, float)) or isinstance(
        overall_pass_rate, bool
    ):
        return {
            "status": TransferImportStatus.MALFORMED,
            "data": None,
            "detail": (
                f"overall_pass_rate must be number, got "
                f"{type(overall_pass_rate).__name__}"
            ),
        }
    if not (0.0 <= float(overall_pass_rate) <= 1.0):
        return {
            "status": TransferImportStatus.MALFORMED,
            "data": None,
            "detail": f"overall_pass_rate out of [0, 1] range: {overall_pass_rate}",
        }
    if not isinstance(threshold_strict, (int, float)) or isinstance(
        threshold_strict, bool
    ):
        return {
            "status": TransferImportStatus.MALFORMED,
            "data": None,
            "detail": (
                f"threshold_strict must be number, got "
                f"{type(threshold_strict).__name__}"
            ),
        }
    # audit 6529b5d4 C3: range-check threshold_strict too (was type-only), so a
    # malformed negative / >1 / NaN / inf threshold cannot compute a false PASS.
    if not (0.0 <= float(threshold_strict) <= 1.0):
        return {
            "status": TransferImportStatus.MALFORMED,
            "data": None,
            "detail": f"threshold_strict out of [0, 1] range: {threshold_strict}",
        }

    data = {
        "run_id": loaded.get("run_id", "<unknown>"),
        "date": loaded.get("date", "<unknown>"),
        "overall_pass_rate": overall_pass_rate,
        "threshold_strict": threshold_strict,
        "threshold_met": threshold_met,
        "task5_advisory": loaded.get("task5_advisory", "<unknown>"),
        "summary_path": str(summary_path),
    }

    # threshold_met must match (overall_pass_rate >= threshold_strict)
    expected_met = float(overall_pass_rate) >= float(threshold_strict)
    if threshold_met != expected_met:
        return {
            "status": TransferImportStatus.INCONSISTENT,
            "data": data,
            "detail": (
                f"threshold_met={threshold_met} contradicts "
                f"overall_pass_rate={overall_pass_rate} >= "
                f"threshold_strict={threshold_strict} → expected={expected_met}"
            ),
        }

    status = (
        TransferImportStatus.OK_PASSED
        if threshold_met
        else TransferImportStatus.OK_FAILED
    )
    return {"status": status, "data": data, "detail": "ok"}


def render_transfer_pack_evidence_row(result: dict[str, Any]) -> Optional[str]:
    """Build the 7th evidence-ledger row for the Transfer Test Pack state.

    Per sketch a2 §8 Q7: failure renders prominently with run id + pass rate
    + threshold (`NOT MET`); pass renders concisely; missing/error renders as
    a TODO row with diagnostic note.

    Audit d4598abf #2 + #3:
    - NOT_FOUND text is path-based, not session-stateful
    - explicit `--transfer-summary` missing path renders the actual path so
      typos are diagnosable (vs auto-discovery's known-candidates message)
    - INCONSISTENT renders TODO with consistency mismatch detail (never PASS)
    """
    # audit 6529b5d4 C2: every imported field + diagnostic is interpolated via
    # _safe_cell (redact secrets + escape table metacharacters). run_id / date /
    # task5_advisory / summary_path / detail are summary- or path-controlled text;
    # overall_pass_rate / threshold_strict are already-validated numerics.
    status = result["status"]
    if status in (TransferImportStatus.OK_PASSED, TransferImportStatus.OK_FAILED):
        d = result["data"]
        run_id = _safe_cell(d["run_id"], 40)
        date = _safe_cell(d["date"], 30)
        advisory = _safe_cell(d["task5_advisory"], 40)
        body = (
            f"run `{run_id}` ({date}) — pass rate {d['overall_pass_rate']} / "
            f"threshold {d['threshold_strict']}; task5 advisory {advisory}"
        )
        if status == TransferImportStatus.OK_PASSED:
            return f"| Transfer Test Pack | {body} | PASS |"
        return f"| **Transfer Test Pack** | **{body}** | **FAIL — threshold NOT MET** |"
    if status == TransferImportStatus.NOT_FOUND:
        if result.get("was_explicit") and result.get("summary_path"):
            # The whole point of this diagnostic is to show the FULL path so a
            # typo is visible — redact + escape but use a generous bound so a
            # normal (long, absolute) path is not truncated past its filename.
            path = _safe_cell(result["summary_path"], 500)
            return (
                f"| Transfer Test Pack | summary not found at explicit path "
                f"`{path}` (check --transfer-summary value) | TODO |"
            )
        # D3 (Codex audit P2-c): Transfer Test Pack is opt-in. When neither a
        # summary was auto-discovered NOR --transfer-summary was passed, omit the
        # row entirely instead of polluting every closeout with a perpetual missing-pack row.
        return None
    if status == TransferImportStatus.INCONSISTENT:
        detail = _safe_cell(result.get("detail", "<no detail>"), 120)
        return (
            f"| **Transfer Test Pack** | **summary INCONSISTENT: {detail}** | "
            f"**TODO — re-run runner; do not trust threshold_met flag** |"
        )
    # READ_ERROR / MALFORMED
    detail = _safe_cell(result.get("detail", "<no detail>"), 120)
    return f"| Transfer Test Pack | summary unreadable: {detail} | TODO |"


def _truncate(text: Optional[str], max_len: int = 60) -> str:
    """Truncate long cells for clean table rendering.

    Note: caller MUST redact_secrets BEFORE calling _truncate (PR-D audit gpt #1
    critical fix: truncating before redaction can hide partial token from regex).
    """
    if text is None:
        return ""
    text = text.replace("\n", " ").replace("|", "\\|")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _bc_exception_is_active(exc: Optional[str]) -> bool:
    """Is the behavior_contract_exception an ACTIVE waiver?

    Reuses the checker's _bc_exception_active (single source of truth) so closeout
    and the checker agree on what counts as a waiver — a raw `null`/`none`/vague
    reason is NOT a waiver. Falls back to the inactive-set check only if the
    construction module is unavailable (getattr-defensive; no drift by default).
    """
    if not exc:
        return False
    fn = getattr(_CONSTRUCTION, "_bc_exception_active", None) if _CONSTRUCTION else None
    if fn is not None:
        return bool(fn(exc))
    return str(exc).strip().lower() not in ("", "null", "none", "~")


def _print_behavior_contract(bc: Any, exc: Optional[str]) -> None:
    """Render the Behavior Contract into durable closeout evidence (v0.14.0).

    Renders the FULL contract (requirement id + normative statement + each
    scenario's id/markers + given/when/then lines), all redacted, so a future
    reader can resolve every cited R<n> after the ephemeral .aqg/ ledger is gone.
    Malformed or waived → a one-line note; never crashes, never dumps raw text.
    Absent contract → silent skip (backward compat with pre-0.14.0 ledgers).
    """
    if _bc_exception_is_active(exc):
        print("### Behavior Contract")
        print()
        print(f"- (waived via behavior_contract_exception: {_safe_cell(exc, 120)})")
        print()
        return
    if bc is None:
        return
    if getattr(bc, "parse_error", None):
        print("### Behavior Contract")
        print()
        print(
            f"- (behavior contract malformed — see checker warnings: "
            f"{_safe_cell(bc.parse_error, 120)})"
        )
        print()
        return
    if not getattr(bc, "present", False) or not getattr(bc, "requirements", ()):
        return  # no contract section → silent skip
    print("### Behavior Contract")
    print()
    try:
        for r in bc.requirements:
            print(f"- **{_safe_cell(getattr(r, 'id', ''), 12)}**: {_safe_cell(getattr(r, 'title', ''), 120)}")
            for line in str(getattr(r, "statement", "")).splitlines():
                if line.strip():
                    print(f"  - {_safe_cell(line, 200)}")
            for s in getattr(r, "scenarios", ()):
                markers = ",".join(sorted(getattr(s, "markers", ()))) or "—"
                print(f"  - {_safe_cell(getattr(s, 'id', ''), 12)} [{markers}]")
                for line in tuple(getattr(s, "lines", ()))[1:]:
                    # strip the raw line's own leading list marker so we don't
                    # render a double bullet ("-   - GIVEN …")
                    step = line.strip().lstrip("-").strip()
                    if step:
                        print(f"    - {_safe_cell(step, 160)}")
    except Exception:  # noqa: BLE001 — render must never crash closeout (audit f3)
        print("- (behavior contract render error — see checker warnings)")
    print()


def print_construction_section(ledger_data: dict[str, Any]) -> None:
    """Print imported construction ledger section.

    PR-D audit gpt #1 critical fix: redact ALL user-controlled header fields
    (task_slug, session_agent, exception). PR-D audit gpt #1 critical fix part 2:
    redact BEFORE truncate so the regex sees the full string.
    """
    h = ledger_data["header"]
    print("## Code Construction Evidence (auto-imported)")
    print()
    # ledger_path is a filesystem path (set by us) → not user-controlled, no redact
    print(f"- ledger: `{ledger_data['ledger_path']}`")
    # All user-controlled header fields go through redact (PR-D audit gpt #1 critical)
    print(f"- task_slug: `{redact_secrets(h.task_slug)}`")
    # h.path is enum (mini|full|plan) validated by parser → no user content; safe
    print(f"- path tier: `{h.path}`")
    # created_at is ISO 8601 validated; safe
    print(f"- created_at: {h.created_at}")
    print(f"- session_agent: {redact_secrets(h.session_agent)}")
    print(f"- 6-step rows present: {len(ledger_data['steps'])}")
    print(f"- skipped_checks count: {len(h.skipped_checks)}")
    if h.objections_diff_coverage_exception:
        print(
            f"- objections diff-coverage exception: {redact_secrets(h.objections_diff_coverage_exception)}"
        )
    print()
    if ledger_data["steps"]:
        print("### 6 Steps")
        print()
        print("| step | evidence | file:line | command/result |")
        print("|---|---|---|---|")
        for s in ledger_data["steps"]:
            # audit 6529b5d4 C1: every cell (incl file:line + step) goes through
            # _safe_cell = redact-before-truncate + table-escape. file_line and
            # step were previously unredacted/unescaped (raw-secret leak path).
            ev = _safe_cell(s.evidence)
            fl = _safe_cell(s.file_line, 40)
            cmd = _safe_cell(s.command_result)
            step_label = _safe_cell(s.step)
            print(f"| {step_label} | {ev} | `{fl}` | `{cmd}` |")
        print()
    # v0.14.0: Behavior Contract (durable full-text render, redacted, malformed-safe)
    _print_behavior_contract(
        ledger_data.get("behavior_contract"),
        getattr(h, "behavior_contract_exception", None),
    )
    if ledger_data["warning_section"]:
        print("### Warning Acknowledgements")
        print()
        print(ledger_data["warning_section"])
        print()
    if ledger_data["objection_section"]:
        print("### Predicted Objections")
        print()
        print(ledger_data["objection_section"])
        print()


def print_construction_missing_notice(
    ledger_path: Path, status: str, detail: str, was_explicit: bool
) -> None:
    """When ledger import did not return ok, print appropriate notice.

    PR-D audit C3 fix: differentiate by status (parser_unavailable vs missing
    vs read_error vs malformed). Implicit (default) MISSING is silent; all
    other failure modes always print a notice so user knows why.
    """
    if status == LedgerImportStatus.MISSING and not was_explicit:
        return  # silent fallback for default missing
    print("## Code Construction Evidence")
    print()
    if status == LedgerImportStatus.MISSING:
        print(f"- ledger NOT FOUND at: `{ledger_path}` (per --construction-ledger arg)")
        print(
            "- Hint: create one via `aqg-code-construction` skill before closeout, OR omit --construction-ledger to skip this section"
        )
    elif status == LedgerImportStatus.PARSER_UNAVAILABLE:
        print(f"- construction parser UNAVAILABLE: {detail}")
        print(
            "- Hint: install or restore the aqg-code-construction skill, or use --no-construction-import to disable this section"
        )
    elif status == LedgerImportStatus.READ_ERROR:
        print(f"- ledger READ ERROR at `{ledger_path}`: {detail}")
        print("- Hint: check file permissions and encoding")
    elif status == LedgerImportStatus.MALFORMED:
        print(f"- ledger MALFORMED at `{ledger_path}`: {detail}")
        print(
            "- Hint: regenerate from `templates/code-construction-ledger.md` and ensure required yaml header fields are set"
        )
    else:
        print(f"- import status: {status} (detail: {detail})")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="<task>", help="short task name")
    parser.add_argument(
        "--repo",
        action="append",
        help="repo path to include in closeout; may be provided multiple times; defaults to current git root",
    )
    parser.add_argument(
        "--construction-ledger",
        help="Path to AQG code construction ledger to auto-import (default: .aqg/current_ledger.md in first repo; PR-D)",
    )
    parser.add_argument(
        "--no-construction-import",
        action="store_true",
        help="Skip construction ledger auto-import even if .aqg/current_ledger.md exists (PR-D)",
    )
    parser.add_argument(
        "--transfer-summary",
        help=(
            "Path to Transfer Test Pack run summary YAML (Q7). "
            "Default: search .aqg/transfer/last_run_summary.yaml then run_summary.yaml then tests/transfer/_last_run_summary.yaml in first --repo"
        ),
    )
    parser.add_argument(
        "--no-transfer-import",
        action="store_true",
        help="Skip Transfer Test Pack 7th-line auto-import even if a summary exists (Q7)",
    )
    args = parser.parse_args()
    repos = (
        [Path(item).expanduser().resolve() for item in args.repo]
        if args.repo
        else [find_git_root(Path.cwd())]
    )

    print("# AQG Evidence Closeout")
    print()
    print(f"- generated_at: {dt.datetime.now(dt.timezone.utc).isoformat()}")
    print(f"- task: {args.task}")
    print(
        "- note: skeleton generator only; fresh test/check/audit evidence must be filled manually from the current session"
    )
    print()
    print("## Repo state")
    for repo in repos:
        print(f"### {repo}")
        print("```")
        print(git_status(repo))
        print("```")
    print()

    # PR-D: optional construction ledger import (audit C3 fix: status-based)
    if not args.no_construction_import:
        was_explicit = args.construction_ledger is not None
        if args.construction_ledger:
            ledger_path = Path(args.construction_ledger).expanduser().resolve()
        else:
            ledger_path = repos[0] / ".aqg" / "current_ledger.md"
        result = import_construction_ledger(ledger_path)
        if result["status"] == LedgerImportStatus.OK:
            print_construction_section(result["data"])
        else:
            print_construction_missing_notice(
                ledger_path,
                status=result["status"],
                detail=result["detail"],
                was_explicit=was_explicit,
            )

    # Transfer Test Pack 7th-line evidence row (Q7 sketch a2 §8)
    transfer_row: Optional[str] = None
    if not args.no_transfer_import:
        if args.transfer_summary:
            transfer_path = Path(args.transfer_summary).expanduser().resolve()
            transfer_result = import_transfer_summary(transfer_path, was_explicit=True)
        else:
            transfer_path = _resolve_transfer_summary_path(None, repos[0])
            transfer_result = import_transfer_summary(transfer_path, was_explicit=False)
        transfer_row = render_transfer_pack_evidence_row(transfer_result)

    print("## Evidence ledger")
    print("| claim | evidence | status |")
    print("|---|---|---|")
    print("| scope completed | <files/PR/issue/status doc> | TODO |")
    print("| verification run | `<command>` -> <exit/result> | TODO |")
    print("| audit adjudicated | <audit id/table or none required> | TODO |")
    print("| durable state updated | <PR body/issue/status/handoff or not needed> | TODO |")
    print(
        "| production boundary | <no prod write/deploy/restart/secrets/raw data, or authorized action evidence> | TODO |"
    )
    print("| remaining blockers | <none or exact blocker> | TODO |")
    if transfer_row is not None:
        print(transfer_row)
    print()
    print("## Final-answer checklist")
    print("- State what changed in one sentence.")
    print("- List only fresh verification evidence.")
    print("- Name deliberate non-actions.")
    print("- Name blockers instead of implying completion.")
    print("- If context is high, include a paste-ready handoff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
