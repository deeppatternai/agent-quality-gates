#!/usr/bin/env python3
"""Run local Agent Quality Gates against a PR body fixture."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from _secret_patterns import secret_counts
from validate_audit_adjudication import validate_text as validate_audit_text
from check_dirty_or_gone_worktree import check_repo
from check_evidence_closeout import evidence_blocks, extract_items, normalize
from check_evidence_closeout import validate_text as validate_evidence_text
from quality_gates_config import ConfigError, load_json_config, normalize_config


EXIT_GATE_FAILURE = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_INTERNAL = 70

# VERSION fallback uses "unknown" instead of a hardcoded version number, to avoid
# displaying a stale old version that would mislead the user when the VERSION file fails to read.
def package_version() -> str:
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return version or "unknown"


VERSION = package_version()

BOUNDARY_LABELS = {
    "production": "production boundary",
    "secrets": "secrets boundary",
    "raw_private_data": "raw private data boundary",
}

CI_ALLOWED_WORKTREE_FAILURES = {
    "detached HEAD",
    "cannot resolve upstream",
    "cannot compare upstream",
}


class UsageError(ValueError):
    """Raised when a runtime input path or argument is invalid."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def static_finding(message: str, suggestion: str, config_key: str) -> dict[str, str]:
    return {
        "message": message,
        "suggestion": suggestion,
        "config_key": config_key,
    }


def gate_result(
    *,
    status: str,
    mode: str,
    enabled: bool,
    findings: list[dict[str, str]] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "mode": mode,
        "enabled": enabled,
        "findings": findings or [],
        "details": details or {},
    }


def gate_blocks_exit(result: dict[str, Any]) -> bool:
    return result["enabled"] and result["mode"] == "blocking" and result["status"] == "fail"


def normalize_status_value(value: str) -> str:
    stripped = value.strip().strip("`").lower()
    match = re.search(r"\bstatus\s*[:=]\s*([a-z-]+)", stripped)
    if match:
        return match.group(1)
    return re.split(r"[\s;,|]+", stripped, maxsplit=1)[0]


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(cell and set(cell) <= {"-", ":", " "} for cell in cells)


def extract_all_evidence_items(pr_body: str) -> dict[str, str]:
    blocks = evidence_blocks(pr_body)
    items = extract_items(blocks)
    for block in blocks:
        for raw in block.splitlines():
            line = raw.strip()
            if line.startswith("|") and line.endswith("|"):
                cells = split_table_row(line)
                if is_separator(cells) or len(cells) < 2:
                    continue
                label = normalize(cells[0])
                value = " | ".join(cells[1:]).strip()
                if label == "item" and normalize(value) in {"evidence", "value"}:
                    continue
                if label:
                    items[label] = value
                continue
            line = re.sub(r"^[-*]\s+", "", line)
            match = re.match(r"^(?:\*\*)?([^:*|]{3,80}?)(?:\*\*)?\s*:\s*(.+)$", line)
            if match:
                label = normalize(match.group(1))
                if label:
                    items[label] = match.group(2).strip()
    return items


def boundary_has_auth_ref(value: str) -> bool:
    return bool(re.search(r"\bauth[_ -]?ref\s*[:=]\s*\S+", value, flags=re.IGNORECASE))


def validate_boundaries(config: dict[str, Any], pr_body: str) -> list[dict[str, str]]:
    items = extract_all_evidence_items(pr_body)
    findings: list[dict[str, str]] = []
    for boundary, rules in config["boundaries"].items():
        if not rules.get("require_explicit_statement"):
            continue
        label = BOUNDARY_LABELS[boundary]
        value = items.get(label)
        config_key = f"boundaries.{boundary}.require_explicit_statement"
        if value is None:
            findings.append(
                static_finding(
                    f"missing explicit {label}",
                    f"add an evidence row for `{label}` with status not-touched, unauthorized, or authorized plus auth_ref",
                    config_key,
                )
            )
            continue
        status = normalize_status_value(value)
        if status not in {"not-touched", "authorized", "unauthorized"}:
            findings.append(
                static_finding(
                    f"{label} has invalid status",
                    "use status not-touched, unauthorized, or authorized",
                    config_key,
                )
            )
            continue
        if status == "authorized" and not boundary_has_auth_ref(value):
            findings.append(
                static_finding(
                    f"{label} authorized status is missing auth_ref",
                    "include auth_ref=<approval-or-record-id> when status is authorized",
                    config_key,
                )
            )
        if status == "unauthorized":
            # WS-8 P2-5a: an unauthorized touch of a protected boundary is exactly
            # what this gate exists to surface — it must not clear the gate silently.
            findings.append(
                static_finding(
                    f"{label} declared unauthorized",
                    "an unauthorized production/secrets/raw-data boundary touch must be "
                    "adjudicated: stop and escalate to the Owner, or obtain authorization "
                    "and set authorized with auth_ref; only relabel not-touched if the "
                    "boundary was in fact never touched and the status was misdeclared",
                    config_key,
                )
            )
    return findings


def extract_audit_skip(pr_body: str, allowed: list[str]) -> tuple[str | None, list[dict[str, str]]]:
    items = extract_all_evidence_items(pr_body)
    value = items.get("audit adjudicated")
    if value is None:
        return None, []

    normalized = value.strip().strip("`").lower()
    reason = None
    for prefix in ("audit-skip:", "audit skip:", "skip:"):
        if normalized.startswith(prefix):
            reason = normalized[len(prefix) :].strip()
            break
    if reason is None and normalized in allowed:
        reason = normalized

    if reason is None:
        return None, []
    if reason in allowed:
        return reason, []
    return None, [
        static_finding(
            "audit skip reason is not allowed by config",
            "use one configured audit_skip_allowed value or provide an audit adjudication table",
            "gates.audit_adjudication.audit_skip_allowed",
        )
    ]


def run_evidence_gate(config: dict[str, Any], pr_body: str) -> dict[str, Any]:
    gate = config["gates"]["evidence_closeout"]
    if not gate["enabled"] or gate["mode"] == "off":
        return gate_result(status="skipped", mode=gate["mode"], enabled=gate["enabled"])

    ok, issues, found = validate_evidence_text(pr_body)
    findings = [
        static_finding(
            issue,
            "complete the PR Evidence Block using templates/pull_request_template.md",
            "gates.evidence_closeout.mode",
        )
        for issue in issues
    ]
    findings.extend(validate_boundaries(config, pr_body))
    status = "pass" if ok and not findings else "fail"
    return gate_result(
        status=status,
        mode=gate["mode"],
        enabled=gate["enabled"],
        findings=findings,
        details={"evidence_items_found": sorted(found)},
    )


def run_audit_gate(config: dict[str, Any], pr_body: str) -> dict[str, Any]:
    gate = config["gates"]["audit_adjudication"]
    if not gate["enabled"] or gate["mode"] == "off":
        return gate_result(status="skipped", mode=gate["mode"], enabled=gate["enabled"])

    allowed_skips = gate.get("audit_skip_allowed", [])
    skip_reason, skip_findings = extract_audit_skip(pr_body, allowed_skips)
    if skip_findings:
        return gate_result(
            status="fail",
            mode=gate["mode"],
            enabled=gate["enabled"],
            findings=skip_findings,
        )
    if skip_reason:
        return gate_result(
            status="skipped",
            mode=gate["mode"],
            enabled=gate["enabled"],
            details={"audit_skip": skip_reason},
        )

    ok, issues, counts = validate_audit_text(pr_body)
    findings = [
        static_finding(
            issue,
            "add a complete Audit Adjudication table or use an allowed structured audit-skip reason",
            "gates.audit_adjudication.mode",
        )
        for issue in issues
    ]
    return gate_result(
        status="pass" if ok else "fail",
        mode=gate["mode"],
        enabled=gate["enabled"],
        findings=findings,
        details={"decision_counts": counts},
    )


def run_worktree_gate(config: dict[str, Any], repo: Path) -> dict[str, Any]:
    gate = config["gates"]["worktree"]
    if not gate["enabled"] or gate["mode"] == "off":
        return gate_result(status="skipped", mode=gate["mode"], enabled=gate["enabled"])

    report = check_repo(repo, fetch=False)
    failures = report.get("failures", [])
    ci_allowed = sorted(set(failures) & CI_ALLOWED_WORKTREE_FAILURES) if gate.get("ci_mode") else []
    hard_failures = [failure for failure in failures if failure not in ci_allowed]
    findings = [
        static_finding(
            f"worktree check failed: {failure}",
            "clean the repo, restore upstream, or adjust CI-safe worktree mode if applicable",
            "gates.worktree.mode",
        )
        for failure in hard_failures
    ]
    details = {
        "path": report.get("path"),
        "branch": report.get("branch"),
        "upstream": report.get("upstream"),
        "dirty": report.get("dirty"),
        "ahead": report.get("ahead"),
        "behind": report.get("behind"),
        "ci_mode": gate.get("ci_mode", False),
        "ci_allowed_failures": ci_allowed,
    }
    return gate_result(
        status="pass" if not hard_failures else "fail",
        mode=gate["mode"],
        enabled=gate["enabled"],
        findings=findings,
        details=details,
    )


def required_file_present(repo: Path, rel: str) -> bool:
    """True iff `rel` names a regular file that physically resides under `repo`.

    Owner-authored paths, but the gate's contract is "a required context file
    lives in-repo", so containment is enforced, not assumed: the candidate is
    resolved (collapsing `..` and following symlinks) and required to stay under
    the resolved repo root and be a regular file. An absolute path, a `..` escape,
    a symlink whose target is outside the repo, or a directory therefore all count
    as absent (fail-closed). resolve()/is_file() never raise for a missing leaf,
    but a symlink loop can raise OSError — treated as absent."""
    try:
        target = (repo / rel).resolve()
        return target.is_file() and target.is_relative_to(repo.resolve())
    except OSError:
        return False


def run_required_context_gate(config: dict[str, Any], repo: Path) -> dict[str, Any]:
    gate = config["gates"]["required_context"]
    if not gate["enabled"] or gate["mode"] == "off":
        return gate_result(status="skipped", mode=gate["mode"], enabled=gate["enabled"])

    declared = config["project"]["required_context_files"]
    missing = [rel for rel in declared if not required_file_present(repo, rel)]
    findings = [
        static_finding(
            f"required context file is missing: {rel}",
            "add the declared context file to the repo, or remove it from project.required_context_files",
            "project.required_context_files",
        )
        for rel in missing
    ]
    return gate_result(
        status="pass" if not missing else "fail",
        mode=gate["mode"],
        enabled=gate["enabled"],
        findings=findings,
        details={"declared": declared, "missing": missing},
    )


def validate_repo_path(repo: Path) -> None:
    if not repo.exists():
        raise UsageError(f"repo path does not exist: {repo}")
    if not repo.is_dir():
        raise UsageError(f"repo path must be a directory: {repo}")
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - convert git invocation problems to usage
        raise UsageError(f"cannot inspect repo path {repo}: {exc}") from exc
    if proc.returncode != 0 or proc.stdout.strip().lower() != "true":
        raise UsageError(f"repo path must be a git worktree: {repo}")


def load_pr_body(path: Path) -> tuple[bytes, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UsageError(f"cannot read PR body file {path}: {exc}") from exc
    return raw, raw.decode("utf-8", errors="replace")


def display_path(path: Path, repo: Path) -> str:
    try:
        resolved = path.resolve()
        repo_resolved = repo.resolve()
        if resolved == repo_resolved:
            return "."
        return str(resolved.relative_to(repo_resolved))
    except (OSError, ValueError):
        return path.name


def write_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    leaked = secret_counts(serialized)
    if leaked:
        raise RuntimeError("redacted JSON still contains secret-like value")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized, encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot write output JSON {path}: {exc}") from exc


def build_payload(config: dict[str, Any], repo: Path, pr_body_path: Path, output_json: Path) -> dict[str, Any]:
    validate_repo_path(repo)
    pr_body_raw, pr_body = load_pr_body(pr_body_path)
    redaction_counts = secret_counts(pr_body)
    gates = {
        "audit_adjudication": run_audit_gate(config, pr_body),
        "evidence_closeout": run_evidence_gate(config, pr_body),
        "worktree": run_worktree_gate(config, repo),
        "required_context": run_required_context_gate(config, repo),
    }
    blocking_failures = [
        name
        for name, result in gates.items()
        if gate_blocks_exit(result)
    ]
    return {
        "ok": not blocking_failures,
        "generated_at": utc_now(),
        "version": 1,
        "project": config["project"],
        "repo": display_path(repo, repo),
        "pr_body": {
            "path": display_path(pr_body_path, repo),
            "sha256": sha256_bytes(pr_body_raw),
            "bytes": len(pr_body_raw),
            "redaction": {
                "secret_like_match_counts": redaction_counts,
                "total_secret_like_matches": sum(redaction_counts.values()),
            },
        },
        "gates": gates,
        "blocking_failures": blocking_failures,
        "output_json": display_path(output_json, repo),
    }


def print_summary(payload: dict[str, Any]) -> None:
    print(f"Agent Quality Gates: {'PASS' if payload['ok'] else 'FAIL'}")
    print(f"project: {payload['project']['name']}")
    print(f"repo: {payload['repo']}")
    print(f"pr_body_sha256: {payload['pr_body']['sha256']}")
    redaction_total = payload["pr_body"]["redaction"]["total_secret_like_matches"]
    print(f"secret_like_matches: {redaction_total}")
    for name, result in payload["gates"].items():
        finding_count = len(result["findings"])
        print(f"- {name}: {result['status']} mode={result['mode']} findings={finding_count}")
        for finding in result["findings"]:
            print(f"  - {finding['message']}. Fix: {finding['suggestion']} ({finding['config_key']})")
    if payload["blocking_failures"]:
        print("blocking_failures: " + ", ".join(payload["blocking_failures"]))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Agent Quality Gates against a local PR body file.")
    parser.add_argument("--version", action="version", version=f"run_quality_gates {VERSION}")
    parser.add_argument("--config", required=True, help="path to quality-gates.json")
    parser.add_argument("--repo", required=True, help="repository path to check")
    parser.add_argument("--pr-body-file", required=True, help="local markdown PR body file")
    parser.add_argument("--output-json", required=True, help="redacted machine-readable JSON output path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        config = normalize_config(load_json_config(Path(args.config).expanduser()))
        payload = build_payload(
            config,
            Path(args.repo).expanduser(),
            Path(args.pr_body_file).expanduser(),
            Path(args.output_json).expanduser(),
        )
        write_json(Path(args.output_json).expanduser(), payload)
        print_summary(payload)
        return 0 if payload["ok"] else EXIT_GATE_FAILURE
    except ConfigError as exc:
        print(f"CONFIG_ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except UsageError as exc:
        print(f"USAGE_ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - adapter should classify unexpected failures
        print(f"INTERNAL_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
