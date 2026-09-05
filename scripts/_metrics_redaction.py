#!/usr/bin/env python3
"""Local metrics ledger record schema + leak guard (Wave 2 #3, framework Q11).

Implemented per triple-audit (audit_id 6a46a056) 11 accepted findings:
- actor closed enum (gpt-5.5 #5: no free-form input, to prevent username leak)
- L3 RA-01/RA-02 convergence (2026-06-03): free-form string fields (tool_version,
  event_id) delegate to the shared _redaction_common.scan_identifier codepath
  (secret_counts 18-pattern + word-boundary belt + control-char), replacing a
  local stale 13-prefix list that missed npm_/hf_/whsec_/sk_live_/stripe/etc.
- required field non-None check (gpt-5.5 #8)
- violation msg NEVER echoes raw value (gpt-5.5 #6)

API:
    assert_safe_metrics_record(record: dict) -> None  # raise MetricsRedactionError
    check_metrics_record(record: dict) -> MetricsRedactionResult

No third-party dependencies, stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from _redaction_common import scan_identifier


# ===== Top-level + nested allowlists =====

REQUIRED_TOP_LEVEL: frozenset[str] = frozenset({
    "schema_version",
    "ts",
    "tool",
    "result",
})
OPTIONAL_TOP_LEVEL: frozenset[str] = frozenset({
    "duration_ms",
    "exit_code",
    "tool_version",
    "actor",
    "context",
    "marker",
    "event_id",  # A2 audit 299b566d #1: provenance join key (e.g. "transfer-<run_id>")
})
ALLOWED_TOP_LEVEL: frozenset[str] = REQUIRED_TOP_LEVEL | OPTIONAL_TOP_LEVEL

ALLOWED_CONTEXT_FIELDS: frozenset[str] = frozenset({
    "audit_id",
    "audit_panel_size",
    "findings_count",
    "accepted_count",
    "rejected_count",
    "needs_user_decision_count",
    "cwd_sha256_first8",
    "git_branch_status",
})

# ===== Enum values =====

ACCEPTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

ALLOWED_TOOLS: frozenset[str] = frozenset({
    "audit",
    "preflight",
    "closeout",
    "debugging",
    "handoff_manifest",
    "doctor",
    "bugfix_record",
    "install_pre_commit",
    "wip_save",
    "wip_recover",
    "metrics",
    "transfer",  # A2: Transfer Test Pack v1 runner emit (Q7 follow-up)
    "other",
})

ALLOWED_RESULTS: frozenset[str] = frozenset({"pass", "warn", "fail", "error"})

# Closed actor enum (gpt-5.5 #5 accepted: prevent free-form username leak)
ALLOWED_ACTORS: frozenset[str] = frozenset({
    "claude", "codex", "gpt-5.5", "gemini", "o3", "human", "ci-bot", "other",
})

ALLOWED_BRANCH_STATUSES: frozenset[str] = frozenset({
    "present", "detached", "missing", "unknown",
})

ALLOWED_MARKERS: frozenset[str] = frozenset({
    "auto-recorded-by-aqg-metrics", "manual",
})

# ===== Patterns =====

ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+\d{2}:\d{2}|-\d{2}:\d{2}|Z)?$")
# A2 audit 299b566d #1: event_id slug — caller-defined provenance join key
EVENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]{0,127}$")
SHA256_FIRST8_RE = re.compile(r"^[0-9a-f]{8}$")
# Semver-ish
TOOL_VERSION_RE = re.compile(r"^[\w.\-]{1,32}$")

@dataclass(frozen=True)
class MetricsRedactionResult:
    is_safe: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


class MetricsRedactionError(Exception):
    def __init__(self, violations: list[str]):
        self.violations = tuple(violations)
        super().__init__(self._fmt())

    def _fmt(self) -> str:
        if not self.violations:
            return "metrics record redaction violation (no detail)"
        lines = [f"metrics record redaction violation ({len(self.violations)} issue(s)):"]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


def _check_int_count(name: str, value: Any, violations: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        violations.append(f"{name}: must be int not bool")
        return
    if not isinstance(value, int):
        violations.append(f"{name}: must be int, got {type(value).__name__}")
        return
    if value < 0:
        violations.append(f"{name}: must be >= 0")


def _check_required_present(record: Mapping[str, Any], violations: list[str]) -> None:
    """Required fields must be present AND non-None (gpt-5.5 #8 accepted)."""
    for req in REQUIRED_TOP_LEVEL:
        if req not in record:
            violations.append(f"<root>.{req}: missing required field")
        elif record[req] is None:
            violations.append(f"<root>.{req}: required field cannot be null")


def check_metrics_record(record: Mapping[str, Any]) -> MetricsRedactionResult:
    violations: list[str] = []

    if not isinstance(record, Mapping):
        return MetricsRedactionResult(
            is_safe=False,
            violations=(f"<root>: must be mapping, got {type(record).__name__}",),
        )

    # Unknown top-level field (allowlist enforce; guard against adding a new field and forgetting to audit it).
    # Post-impl dual-audit gpt-5.5 #4: do not echo key text — in JSON mode a user-controlled key may contain a secret.
    unknown_top = sum(1 for key in record.keys() if key not in ALLOWED_TOP_LEVEL)
    if unknown_top:
        violations.append(
            f"<root>: {unknown_top} unknown top-level field(s) (not in allowlist); "
            f"key names suppressed to avoid leak"
        )

    # Required + non-None
    _check_required_present(record, violations)

    # schema_version
    sv = record.get("schema_version")
    if sv is not None:
        if not isinstance(sv, int) or isinstance(sv, bool):
            violations.append(f"schema_version: must be int, got {type(sv).__name__}")
        elif sv not in ACCEPTED_SCHEMA_VERSIONS:
            violations.append(f"schema_version: must be one of {sorted(ACCEPTED_SCHEMA_VERSIONS)}")

    # ts
    ts = record.get("ts")
    if ts is not None:
        if not isinstance(ts, str) or not ISO_TS_RE.match(ts):
            violations.append("ts: must be ISO 8601 datetime")

    # tool enum
    tool = record.get("tool")
    if tool is not None and (not isinstance(tool, str) or tool not in ALLOWED_TOOLS):
        violations.append(f"tool: must be one of {sorted(ALLOWED_TOOLS)}")

    # result enum
    res = record.get("result")
    if res is not None and (not isinstance(res, str) or res not in ALLOWED_RESULTS):
        violations.append(f"result: must be one of {sorted(ALLOWED_RESULTS)}")

    # actor closed enum
    actor = record.get("actor")
    if actor is not None and (not isinstance(actor, str) or actor not in ALLOWED_ACTORS):
        violations.append(f"actor: must be one of {sorted(ALLOWED_ACTORS)} (closed enum)")

    # marker enum
    marker = record.get("marker")
    if marker is not None and (not isinstance(marker, str) or marker not in ALLOWED_MARKERS):
        violations.append(f"marker: must be one of {sorted(ALLOWED_MARKERS)}")

    # tool_version
    tv = record.get("tool_version")
    if tv is not None:
        if not isinstance(tv, str) or not TOOL_VERSION_RE.match(tv):
            violations.append("tool_version: must match ^[\\w.\\-]{1,32}$")
        else:
            scan_identifier("tool_version", tv, violations)

    # event_id (A2 audit 299b566d #1): provenance join key
    eid = record.get("event_id")
    if eid is not None:
        if not isinstance(eid, str) or not EVENT_ID_RE.match(eid):
            violations.append(
                f"event_id: must match {EVENT_ID_RE.pattern}"
            )
        else:
            scan_identifier("event_id", eid, violations)

    # int counts
    for cnt_field in ("duration_ms", "exit_code"):
        _check_int_count(cnt_field, record.get(cnt_field), violations)

    # context (nested)
    ctx = record.get("context")
    if ctx is not None:
        if not isinstance(ctx, dict):
            violations.append(f"context: must be mapping, got {type(ctx).__name__}")
        else:
            unknown_ctx = sum(1 for key in ctx.keys() if key not in ALLOWED_CONTEXT_FIELDS)
            if unknown_ctx:
                # Post-impl dual-audit gpt-5.5 #4: do not echo key text
                violations.append(
                    f"context: {unknown_ctx} unknown field(s); key names suppressed"
                )
            # int counts in context
            for cnt_field in (
                "audit_panel_size", "findings_count", "accepted_count",
                "rejected_count", "needs_user_decision_count",
            ):
                _check_int_count(f"context.{cnt_field}", ctx.get(cnt_field), violations)
            # 8-hex audit_id / cwd_sha256_first8
            for hex_field in ("audit_id", "cwd_sha256_first8"):
                v = ctx.get(hex_field)
                if v is not None:
                    if not isinstance(v, str) or not SHA256_FIRST8_RE.match(v):
                        violations.append(f"context.{hex_field}: must be 8 lowercase hex chars")
            # branch status enum
            bs = ctx.get("git_branch_status")
            if bs is not None and (not isinstance(bs, str) or bs not in ALLOWED_BRANCH_STATUSES):
                violations.append(
                    f"context.git_branch_status: must be one of {sorted(ALLOWED_BRANCH_STATUSES)}"
                )

    return MetricsRedactionResult(is_safe=not violations, violations=tuple(violations))


def assert_safe_metrics_record(record: Mapping[str, Any]) -> None:
    result = check_metrics_record(record)
    if not result.is_safe:
        raise MetricsRedactionError(list(result.violations))


def self_test() -> int:
    valid = {
        "schema_version": 1,
        "ts": "2026-05-03T12:34:56+00:00",
        "tool": "audit",
        "result": "pass",
        "duration_ms": 1234,
        "exit_code": 0,
        "tool_version": "0.2.6",
        "actor": "claude",
        "context": {
            "audit_id": "deadbeef",
            "audit_panel_size": 3,
            "findings_count": 14,
            "accepted_count": 12,
            "rejected_count": 2,
            "cwd_sha256_first8": "12345678",
            "git_branch_status": "present",
        },
        "marker": "auto-recorded-by-aqg-metrics",
    }
    assert check_metrics_record(valid).is_safe

    # required null reject (gpt-5.5 #8)
    bad = dict(valid); bad["ts"] = None
    assert not check_metrics_record(bad).is_safe

    # missing required
    bad = dict(valid); del bad["tool"]
    assert not check_metrics_record(bad).is_safe

    # unknown top-level
    bad = dict(valid); bad["leak_field"] = "x"
    assert not check_metrics_record(bad).is_safe

    # actor closed enum (gpt-5.5 #5)
    bad = dict(valid); bad["actor"] = "jeff"
    assert not check_metrics_record(bad).is_safe
    ok = dict(valid); ok["actor"] = "gpt-5.5"
    assert check_metrics_record(ok).is_safe

    # token mid-string (gemini #3 substring fix)
    bad = dict(valid); bad["tool_version"] = "0.2.6-ghp_xxx"
    assert not check_metrics_record(bad).is_safe

    # violation msg never echoes value (gpt-5.5 #6)
    leaked_token = "ghp_FAKE_TOKEN_FOR_TEST_xxx"
    bad = dict(valid); bad["tool_version"] = leaked_token[:32]  # fit length
    result = check_metrics_record(bad)
    assert not result.is_safe
    for v in result.violations:
        assert leaked_token[:8] not in v, f"raw value leaked in violation: {v}"

    # context unknown field
    bad = dict(valid); bad["context"] = dict(valid["context"]); bad["context"]["secret"] = "x"
    assert not check_metrics_record(bad).is_safe

    # invalid enum
    bad = dict(valid); bad["tool"] = "unknown_tool"
    assert not check_metrics_record(bad).is_safe

    # raise API
    try:
        assert_safe_metrics_record({"schema_version": 99})
        raise AssertionError
    except MetricsRedactionError:
        pass

    # L3 RA-01/RA-02: tokens the OLD local 13-prefix list missed are now caught via
    # the shared scan_identifier (secret_counts 18-pattern + word-boundary belt).
    for tok in ("whsec_" + "a" * 24, "sk_live_" + "a" * 20):  # fit tool_version <=32
        bad = dict(valid); bad["tool_version"] = tok
        assert not check_metrics_record(bad).is_safe, f"tool_version {tok[:6]!r} must be caught"
    for tok in ("npm_" + "a" * 34, "hf_" + "a" * 33):  # fit event_id <=128
        bad = dict(valid); bad["event_id"] = tok
        assert not check_metrics_record(bad).is_safe, f"event_id {tok[:4]!r} must be caught"
    # valid record still passes (no over-rejection regression)
    assert check_metrics_record(valid).is_safe, check_metrics_record(valid).violations

    print("OK: _metrics_redaction self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
