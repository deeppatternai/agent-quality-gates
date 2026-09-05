#!/usr/bin/env python3
"""Strict WIP snapshot schema + redaction gatekeeper (Wave 1 P0 #7).

Why a separate module:
WIP snapshot fields (cwd_status / git_branch_sha256_first8 / cwd_sha256_first8 /
saved_at_iso / trigger etc.) do not all fit the doctor surface allowlist in
`_surface_redaction.py` (e.g. saved_at_iso is not there). Triple-audit o3 #2
accepted (modified): write a separate minimal _wip_redaction, but additionally
assert that fields are 100% on the allowlist (so a newly added field cannot
escape audit).

API:
    assert_safe_wip_snapshot(snap: dict) -> None  # raises WipRedactionError on a violation
    check_wip_snapshot(snap: dict) -> WipRedactionResult  # non-raising version

No third-party dependencies, stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from _redaction_common import scan_identifier


# ===== Top-level required + optional field allowlist =====

REQUIRED_TOP_LEVEL: frozenset[str] = frozenset({
    "schema_version",
    "session_id",
    "saved_at_iso",
    "trigger",
    "cwd_sha256_first8",
})

OPTIONAL_TOP_LEVEL: frozenset[str] = frozenset({
    "cwd_status",
    "recent_open_pr_count",
    "todo_state",
    "marker",
})

ALLOWED_TOP_LEVEL: frozenset[str] = REQUIRED_TOP_LEVEL | OPTIONAL_TOP_LEVEL

# ===== Nested field allowlist =====

ALLOWED_CWD_STATUS_FIELDS: frozenset[str] = frozenset({
    "exists",
    "is_directory",
    "git_branch_sha256_first8",
    "git_is_default_branch",
    "git_branch_status",  # detached/present/missing
    "git_dirty",
    "git_ahead_count",
    "git_behind_count",
    "modified_files_count",
    "untracked_files_count",
})

ALLOWED_TODO_STATE_FIELDS: frozenset[str] = frozenset({
    "items_count",
    "in_progress_count",
    "completed_count",
    "pending_count",
})

# ===== Value restrictions =====

ALLOWED_TRIGGERS: frozenset[str] = frozenset({"PreCompact", "manual", "test"})
ALLOWED_GIT_BRANCH_STATUS: frozenset[str] = frozenset({"detached", "present", "missing", "unknown"})
ALLOWED_MARKERS: frozenset[str] = frozenset({"auto-saved-precompact", "manual-save", "test"})

# session_id strictly alphanumeric + - + _ (1-64 char) — prevent path traversal
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Post-impl dual-audit gpt-5.5 #2 accepted: session_id must also pass the secret denylist
# (regex pattern is not enough; a token like 'ghp_xxx' is also valid charset). token-shape scanning
# is now uniformly delegated to _redaction_common.scan_identifier (substring belt + _secret_patterns,
# audit 4f0c48c0 root cause A); this module no longer maintains a local prefix list.

# saved_at_iso: RFC 3339 ish (format accepted by datetime.fromisoformat)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+\d{2}:\d{2}|-\d{2}:\d{2}|Z)?$")

# sha256_first8: 8 lowercase hex
SHA256_FIRST8_RE = re.compile(r"^[0-9a-f]{8}$")

# schema_version currently only 1
ACCEPTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

# upper bound on allowed marker length (prevent leak)
MAX_MARKER_LEN = 128


@dataclass(frozen=True)
class WipRedactionResult:
    is_safe: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


class WipRedactionError(Exception):
    def __init__(self, violations: list[str]):
        self.violations = tuple(violations)
        super().__init__(self._fmt())

    def _fmt(self) -> str:
        if not self.violations:
            return "WIP snapshot redaction violation (no detail)"
        lines = [f"WIP snapshot redaction violation ({len(self.violations)} issue(s)):"]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


def _check_int_count(value: Any, full_path: str, violations: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        violations.append(f"{full_path}: count must be int not bool")
        return
    if not isinstance(value, int):
        violations.append(f"{full_path}: count must be int, got {type(value).__name__}")
        return
    if value < 0:
        violations.append(f"{full_path}: count must be >= 0, got {value}")


def _check_bool(value: Any, full_path: str, violations: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, bool):
        violations.append(f"{full_path}: must be bool, got {type(value).__name__}")


def _check_sha256_first8(value: Any, full_path: str, violations: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        violations.append(f"{full_path}: must be str, got {type(value).__name__}")
        return
    if not SHA256_FIRST8_RE.match(value):
        violations.append(f"{full_path}: must be 8 lowercase hex chars")


def check_wip_snapshot(snap: Mapping[str, Any]) -> WipRedactionResult:
    """Non-raising version. Returns all violations at once."""
    violations: list[str] = []

    if not isinstance(snap, Mapping):
        return WipRedactionResult(
            is_safe=False,
            violations=(f"<root>: must be mapping, got {type(snap).__name__}",),
        )

    # === unknown top-level fields (allowlist enforce; prevent new fields skipping audit) ===
    for key in snap.keys():
        if key not in ALLOWED_TOP_LEVEL:
            violations.append(f"<root>.{key}: unknown top-level field (not in allowlist)")

    # === required fields ===
    for req in REQUIRED_TOP_LEVEL:
        if req not in snap:
            violations.append(f"<root>.{req}: missing required field")

    # === schema_version ===
    sv = snap.get("schema_version")
    if sv is not None:
        if not isinstance(sv, int) or isinstance(sv, bool):
            violations.append(f"schema_version: must be int, got {type(sv).__name__}")
        elif sv not in ACCEPTED_SCHEMA_VERSIONS:
            violations.append(
                f"schema_version: must be one of {sorted(ACCEPTED_SCHEMA_VERSIONS)}"
            )

    # === session_id ===
    sid = snap.get("session_id")
    if sid is not None:
        if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
            violations.append(
                "session_id: must match ^[A-Za-z0-9_-]{1,64}$ (prevents path traversal)"
            )
        else:
            # (#5/#15) uniformly routes through scan_identifier (substring token
            # scan) — the old _looks_like_token used startswith, so a token embedded
            # in the middle like session_id="sess-ghp_xxx" went undetected.
            # SESSION_ID_RE already constrains charset; the residual risk is a
            # token-shape embedded in valid chars, which scan_identifier's substring
            # belt can catch.
            scan_identifier("session_id", sid, violations)

    # === saved_at_iso ===
    iso = snap.get("saved_at_iso")
    if iso is not None:
        if not isinstance(iso, str) or not ISO_DATE_RE.match(iso):
            violations.append("saved_at_iso: must match RFC3339 ish format")

    # === trigger ===
    # (#7) isinstance guard precedes `not in frozenset` — a non-str (list/dict) gets a clear
    # type violation instead of falling into the enum-mismatch branch.
    trg = snap.get("trigger")
    if trg is not None:
        if not isinstance(trg, str):
            violations.append(f"trigger: must be str, got {type(trg).__name__}")
        elif trg not in ALLOWED_TRIGGERS:
            violations.append(
                f"trigger: must be one of {sorted(ALLOWED_TRIGGERS)}"
            )

    # === cwd_sha256_first8 ===
    _check_sha256_first8(snap.get("cwd_sha256_first8"), "cwd_sha256_first8", violations)

    # === marker ===
    marker = snap.get("marker")
    if marker is not None:
        if not isinstance(marker, str):
            violations.append(f"marker: must be str, got {type(marker).__name__}")
        elif len(marker) > MAX_MARKER_LEN:
            violations.append(f"marker: too long ({len(marker)} > {MAX_MARKER_LEN})")
        elif marker not in ALLOWED_MARKERS:
            violations.append(
                f"marker: must be one of {sorted(ALLOWED_MARKERS)}"
            )

    # === recent_open_pr_count ===
    _check_int_count(snap.get("recent_open_pr_count"), "recent_open_pr_count", violations)

    # === cwd_status (nested) ===
    cwd_status = snap.get("cwd_status")
    if cwd_status is not None:
        if not isinstance(cwd_status, dict):
            violations.append(
                f"cwd_status: must be mapping, got {type(cwd_status).__name__}"
            )
        else:
            for key in cwd_status.keys():
                if key not in ALLOWED_CWD_STATUS_FIELDS:
                    violations.append(
                        f"cwd_status.{key}: unknown field (not in allowlist)"
                    )
            _check_bool(cwd_status.get("exists"), "cwd_status.exists", violations)
            _check_bool(cwd_status.get("is_directory"), "cwd_status.is_directory", violations)
            _check_sha256_first8(
                cwd_status.get("git_branch_sha256_first8"),
                "cwd_status.git_branch_sha256_first8",
                violations,
            )
            _check_bool(
                cwd_status.get("git_is_default_branch"),
                "cwd_status.git_is_default_branch",
                violations,
            )
            gbs = cwd_status.get("git_branch_status")
            if gbs is not None:
                if not isinstance(gbs, str):
                    violations.append(
                        f"cwd_status.git_branch_status: must be str, "
                        f"got {type(gbs).__name__}"
                    )
                elif gbs not in ALLOWED_GIT_BRANCH_STATUS:
                    violations.append(
                        f"cwd_status.git_branch_status: must be one of "
                        f"{sorted(ALLOWED_GIT_BRANCH_STATUS)}"
                    )
            _check_bool(cwd_status.get("git_dirty"), "cwd_status.git_dirty", violations)
            for cnt_field in (
                "git_ahead_count",
                "git_behind_count",
                "modified_files_count",
                "untracked_files_count",
            ):
                _check_int_count(
                    cwd_status.get(cnt_field), f"cwd_status.{cnt_field}", violations
                )

    # === todo_state (nested) ===
    todo = snap.get("todo_state")
    if todo is not None:
        if not isinstance(todo, dict):
            violations.append(f"todo_state: must be mapping, got {type(todo).__name__}")
        else:
            for key in todo.keys():
                if key not in ALLOWED_TODO_STATE_FIELDS:
                    violations.append(
                        f"todo_state.{key}: unknown field (not in allowlist)"
                    )
            for cnt_field in ALLOWED_TODO_STATE_FIELDS:
                _check_int_count(todo.get(cnt_field), f"todo_state.{cnt_field}", violations)

    return WipRedactionResult(is_safe=not violations, violations=tuple(violations))


def assert_safe_wip_snapshot(snap: Mapping[str, Any]) -> None:
    """Raises WipRedactionError if there are violations, reporting them all at once."""
    result = check_wip_snapshot(snap)
    if not result.is_safe:
        raise WipRedactionError(list(result.violations))


def self_test() -> int:
    """Quick sanity self-test."""
    # complete valid snapshot
    valid = {
        "schema_version": 1,
        "session_id": "abc123def456",
        "saved_at_iso": "2026-05-03T12:34:56+00:00",
        "trigger": "PreCompact",
        "cwd_sha256_first8": "deadbeef",
        "cwd_status": {
            "exists": True,
            "is_directory": True,
            "git_branch_sha256_first8": "12345678",
            "git_is_default_branch": False,
            "git_branch_status": "present",
            "git_dirty": True,
            "git_ahead_count": 1,
            "git_behind_count": 0,
            "modified_files_count": 3,
            "untracked_files_count": 2,
        },
        "recent_open_pr_count": 1,
        "todo_state": {
            "items_count": 5,
            "in_progress_count": 1,
            "completed_count": 4,
            "pending_count": 0,
        },
        "marker": "auto-saved-precompact",
    }
    r = check_wip_snapshot(valid)
    assert r.is_safe, f"valid should pass: {r.violations}"

    # missing required field
    bad = dict(valid); del bad["session_id"]
    assert not check_wip_snapshot(bad).is_safe

    # unknown top-level field (prevent new fields skipping audit)
    bad = dict(valid); bad["secret_field"] = "anything"
    assert not check_wip_snapshot(bad).is_safe

    # session_id path traversal
    bad = dict(valid); bad["session_id"] = "../etc/passwd"
    assert not check_wip_snapshot(bad).is_safe

    # session_id token-shape — prefix (post-impl dual-audit gpt-5.5 #2)
    bad = dict(valid); bad["session_id"] = "ghp_FAKE_TOKEN_SHAPED_xxx"
    assert not check_wip_snapshot(bad).is_safe

    # session_id token-shape — MID-string embed (#15: old startswith missed it)
    bad = dict(valid); bad["session_id"] = "sess-ghp_" + "a" * 16
    assert not check_wip_snapshot(bad).is_safe, "mid-string embedded token (#15)"

    # unknown trigger
    bad = dict(valid); bad["trigger"] = "Stop"
    assert not check_wip_snapshot(bad).is_safe

    # cwd_status with an unknown field added
    bad = dict(valid)
    bad["cwd_status"] = dict(valid["cwd_status"])
    bad["cwd_status"]["raw_branch_name"] = "leaked-branch"
    assert not check_wip_snapshot(bad).is_safe

    # raise version
    try:
        assert_safe_wip_snapshot({"schema_version": 99})
        raise AssertionError("should have raised")
    except WipRedactionError:
        pass

    print("OK: _wip_redaction self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
