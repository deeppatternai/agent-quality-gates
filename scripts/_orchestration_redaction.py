#!/usr/bin/env python3
"""Orchestration manifest schema + leak guard (Wave 2 #5, framework v2.1 §7 #5).

Implemented per triple-audit (audit_id d7ca6aec) 14 accepted findings:
- on_fail enum allows only abort/continue (3-auditor major: drop retry to resolve ambiguity)
- prompt_ref optional field (2-auditor major: intent 200 char insufficient for reproducibility)
- step inputs dedupe (gpt-5.5 #6: A→[B,B] reject)
- timeout_seconds clamp 1-86400 (o3 #6 nit)
- token denylist via _secret_patterns regex (gemini #1 critical)
- shared SLUG_RE constant
- DAG cycle check via Kahn

API:
    assert_safe_orchestration_manifest(record: dict) -> None
    check_orchestration_manifest(record: dict) -> OrchestrationRedactionResult
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from _redaction_common import scan_identifier, scan_leaks


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
SCHEMA_VERSION_ACCEPTED: frozenset[int] = frozenset({1})

ALLOWED_ACTORS: frozenset[str] = frozenset({
    "claude", "codex", "gpt-5.5", "gemini", "o3", "human", "ci-bot", "other",
})

ALLOWED_ON_FAIL: frozenset[str] = frozenset({"abort", "continue"})

ALLOWED_MARKERS: frozenset[str] = frozenset({
    "orchestration-helper-manifest", "manual",
})

REQUIRED_TOP_LEVEL: frozenset[str] = frozenset({
    "schema_version", "name", "description", "steps", "boundaries",
})
OPTIONAL_TOP_LEVEL: frozenset[str] = frozenset({
    "actor", "marker",
})
ALLOWED_TOP_LEVEL: frozenset[str] = REQUIRED_TOP_LEVEL | OPTIONAL_TOP_LEVEL

REQUIRED_STEP_FIELDS: frozenset[str] = frozenset({
    "id", "actor", "intent", "inputs", "timeout_seconds", "retry_max", "on_fail",
})
OPTIONAL_STEP_FIELDS: frozenset[str] = frozenset({"prompt_ref"})
ALLOWED_STEP_FIELDS: frozenset[str] = REQUIRED_STEP_FIELDS | OPTIONAL_STEP_FIELDS

MAX_NAME_LEN = 80
MAX_DESCRIPTION_LEN = 800
MAX_BOUNDARIES_LEN = 800
MAX_INTENT_LEN = 200
MAX_STEPS = 30
MAX_INPUTS_PER_STEP = 20
MAX_RETRY = 5
MIN_TIMEOUT = 1
MAX_TIMEOUT = 24 * 3600  # 1 day
MAX_PROMPT_REF_LEN = 200


@dataclass(frozen=True)
class OrchestrationRedactionResult:
    is_safe: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


class OrchestrationRedactionError(Exception):
    def __init__(self, violations: list[str]):
        self.violations = tuple(violations)
        super().__init__(self._fmt())

    def _fmt(self) -> str:
        if not self.violations:
            return "orchestration manifest redaction violation (no detail)"
        lines = [f"orchestration manifest redaction violation ({len(self.violations)} issue(s)):"]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


def _is_strict_int(v: Any) -> bool:
    return type(v) is int


def _check_path_safe(name: str, value: str, violations: list[str]) -> None:
    """Project-relative path validation + token leak scan.

    L1 fix (audit 4f0c48c0 #5): the prior version only ran token detection.
    The path charset regex below (`^[A-Za-z0-9._/\\-]+$`) already excludes the
    chars that email / URL / auth-header / IP patterns need (@ : ? space etc.),
    so the prose-level `scan_leaks` would be redundant for those classes. We
    therefore keep the dedicated path checks (abs / UNC / .. / trailing-slash /
    charset) and run `scan_identifier` for the residual token-shape risk — same
    fail-closed token belt as prose, without double-reporting path/PII classes.
    """
    if not value or len(value) > MAX_PROMPT_REF_LEN:
        violations.append(f"{name}: empty or too long")
        return
    if value.startswith("/") or value.startswith("\\"):
        violations.append(f"{name}: absolute path not allowed")
    if value.startswith("//"):
        violations.append(f"{name}: UNC path not allowed")
    if value.endswith("/") or value.endswith("\\"):
        violations.append(f"{name}: trailing slash not allowed")
    parts = re.split(r"[/\\]", value)
    if ".." in parts:
        violations.append(f"{name}: contains '..' parent traversal")
    if not re.match(r"^[A-Za-z0-9._/\-]+$", value):
        violations.append(f"{name}: contains disallowed characters")
    scan_identifier(name, value, violations)


def _check_dag_no_cycle(steps: list[dict], violations: list[str]) -> None:
    """Kahn's algorithm DAG check. Steps already validated unique ids + valid input refs."""
    indegree: dict[str, int] = {}
    edges: dict[str, set[str]] = {}
    ids = []
    for step in steps:
        if not isinstance(step, dict):
            return  # caller already reported type error
        sid = step.get("id")
        if not isinstance(sid, str):
            return
        ids.append(sid)
        indegree.setdefault(sid, 0)
        edges.setdefault(sid, set())
    for step in steps:
        sid = step.get("id")
        inputs = step.get("inputs", [])
        if not isinstance(inputs, list):
            continue
        # dedupe inputs (post-impl gpt-5.5 #6)
        seen = set()
        for inp in inputs:
            if not isinstance(inp, str):
                continue
            if inp in seen:
                continue
            seen.add(inp)
            if inp not in indegree:
                violations.append(f"steps.{sid}.inputs: references unknown step id")
                continue
            if inp == sid:
                violations.append(f"steps.{sid}.inputs: self-loop not allowed")
                continue
            edges[inp].add(sid)
            indegree[sid] = indegree.get(sid, 0) + 1
    # Kahn topological sort
    queue = [n for n in ids if indegree[n] == 0]
    visited = 0
    while queue:
        n = queue.pop(0)
        visited += 1
        for child in edges.get(n, set()):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(ids):
        violations.append("steps: DAG cycle detected (some inputs form a cycle)")


def _check_step(step: Any, idx: int, violations: list[str]) -> None:
    if not isinstance(step, dict):
        violations.append(f"steps[{idx}]: must be mapping")
        return

    # Unknown fields
    unknown = sum(1 for k in step.keys() if k not in ALLOWED_STEP_FIELDS)
    if unknown:
        violations.append(f"steps[{idx}]: {unknown} unknown field(s); key names suppressed")

    # Required + non-None
    for req in REQUIRED_STEP_FIELDS:
        if req not in step:
            violations.append(f"steps[{idx}].{req}: missing required field")
        elif step[req] is None:
            violations.append(f"steps[{idx}].{req}: required field cannot be null")

    sid = step.get("id")
    if sid is not None:
        if not isinstance(sid, str) or not SLUG_RE.match(sid):
            violations.append(f"steps[{idx}].id: must match slug regex")
        else:
            # (#N3) step id fits SLUG_RE but a token-shape value still passes —
            # scan it like name/slug elsewhere.
            scan_identifier(f"steps[{idx}].id", sid, violations)

    actor = step.get("actor")
    # #7: enum membership on a non-hashable (dict/list) value raises TypeError
    # in `x not in frozenset`. Guard with isinstance(str) first so a malformed
    # value is rejected as a violation rather than crashing the guard.
    if actor is not None and (not isinstance(actor, str) or actor not in ALLOWED_ACTORS):
        violations.append(f"steps[{idx}].actor: must be one of {sorted(ALLOWED_ACTORS)}")

    intent = step.get("intent")
    if intent is not None:
        if not isinstance(intent, str):
            violations.append(f"steps[{idx}].intent: must be str, got {type(intent).__name__} (prevents dict/list from bypassing redaction)")
        else:
            if len(intent) > MAX_INTENT_LEN:
                violations.append(f"steps[{idx}].intent: too long ({len(intent)} > {MAX_INTENT_LEN})")
            # intent is an inline (single-line) prose field — scan_leaks(multiline=False)
            # subsumes the prior control-char check AND adds path/email/url/ip/header.
            scan_leaks(f"steps[{idx}].intent", intent, violations, multiline=False)

    inputs = step.get("inputs")
    if inputs is not None:
        if not isinstance(inputs, list):
            violations.append(f"steps[{idx}].inputs: must be list")
        elif len(inputs) > MAX_INPUTS_PER_STEP:
            violations.append(f"steps[{idx}].inputs: too many ({len(inputs)} > {MAX_INPUTS_PER_STEP})")
        else:
            # Dedupe check (post-impl gpt-5.5 #6).
            # Post-impl gemini #3 major: don't return early — break after appending
            # so subsequent step field validation (timeout/retry/on_fail/prompt_ref)
            # still runs.
            unique = set()
            dup_reported = False
            for i, inp in enumerate(inputs):
                if not isinstance(inp, str):
                    violations.append(f"steps[{idx}].inputs[{i}]: must be str")
                    continue
                if inp in unique:
                    if not dup_reported:
                        violations.append(f"steps[{idx}].inputs: duplicate id in inputs")
                        dup_reported = True
                    continue  # don't add to unique, but keep validating other fields
                unique.add(inp)

    timeout = step.get("timeout_seconds")
    if timeout is not None:
        if not _is_strict_int(timeout) or timeout < MIN_TIMEOUT or timeout > MAX_TIMEOUT:
            violations.append(f"steps[{idx}].timeout_seconds: must be int {MIN_TIMEOUT}-{MAX_TIMEOUT}")

    retry = step.get("retry_max")
    if retry is not None:
        if not _is_strict_int(retry) or retry < 0 or retry > MAX_RETRY:
            violations.append(f"steps[{idx}].retry_max: must be int 0-{MAX_RETRY}")

    on_fail = step.get("on_fail")
    # #7: isinstance guard before enum membership (non-hashable → TypeError).
    if on_fail is not None and (not isinstance(on_fail, str) or on_fail not in ALLOWED_ON_FAIL):
        violations.append(f"steps[{idx}].on_fail: must be one of {sorted(ALLOWED_ON_FAIL)}")

    pref = step.get("prompt_ref")
    if pref is not None:
        if not isinstance(pref, str):
            violations.append(f"steps[{idx}].prompt_ref: must be str")
        else:
            _check_path_safe(f"steps[{idx}].prompt_ref", pref, violations)


def check_orchestration_manifest(record: Mapping[str, Any]) -> OrchestrationRedactionResult:
    violations: list[str] = []

    if not isinstance(record, Mapping):
        return OrchestrationRedactionResult(
            is_safe=False,
            violations=(f"<root>: must be mapping, got {type(record).__name__}",),
        )

    unknown_top = sum(1 for k in record.keys() if k not in ALLOWED_TOP_LEVEL)
    if unknown_top:
        violations.append(f"<root>: {unknown_top} unknown top-level field(s); key names suppressed")

    for req in REQUIRED_TOP_LEVEL:
        if req not in record:
            violations.append(f"<root>.{req}: missing required field")
        elif record[req] is None:
            violations.append(f"<root>.{req}: required field cannot be null")

    sv = record.get("schema_version")
    if sv is not None and (not _is_strict_int(sv) or sv not in SCHEMA_VERSION_ACCEPTED):
        violations.append(f"schema_version: must be int in {sorted(SCHEMA_VERSION_ACCEPTED)}")

    name = record.get("name")
    if name is not None:
        if not isinstance(name, str) or not SLUG_RE.match(name):
            violations.append("name: must match slug regex")
        else:
            # name is a slug identifier (charset already excludes newline/control
            # via SLUG_RE) — scan_identifier adds the token-shape belt (#3).
            scan_identifier("name", name, violations)

    desc = record.get("description")
    if desc is not None:
        if not isinstance(desc, str):
            violations.append(f"description: must be str, got {type(desc).__name__} (prevents dict/list from bypassing redaction)")
        else:
            if len(desc) > MAX_DESCRIPTION_LEN:
                violations.append("description: too long")
            # multiline prose: scan_leaks subsumes the control-char check AND adds
            # path/email/url/ip/header + markdown-heading injection guard (#5, #8).
            scan_leaks("description", desc, violations, multiline=True)

    steps = record.get("steps")
    if steps is not None:
        if not isinstance(steps, list):
            violations.append("steps: must be list")
        elif not steps:
            violations.append("steps: must have at least 1 step")
        elif len(steps) > MAX_STEPS:
            violations.append(f"steps: too many ({len(steps)} > {MAX_STEPS})")
        else:
            ids = [s.get("id") for s in steps if isinstance(s, dict)]
            if len(set(ids)) != len(ids):
                violations.append("steps: duplicate step id")
            for i, step in enumerate(steps):
                _check_step(step, i, violations)
            # DAG check only if all steps have valid id structure
            if all(isinstance(s, dict) and isinstance(s.get("id"), str) for s in steps):
                _check_dag_no_cycle(steps, violations)

    bnd = record.get("boundaries")
    if bnd is not None:
        if not isinstance(bnd, str):
            violations.append(f"boundaries: must be str, got {type(bnd).__name__} (prevents dict/list from bypassing redaction)")
        else:
            if len(bnd) > MAX_BOUNDARIES_LEN:
                violations.append("boundaries: too long")
            # multiline prose: scan_leaks subsumes control-char + adds PII/path/
            # markdown-heading injection guard (#5, #8).
            scan_leaks("boundaries", bnd, violations, multiline=True)

    actor = record.get("actor")
    # #7: isinstance guard before enum membership (non-hashable → TypeError).
    if actor is not None and (not isinstance(actor, str) or actor not in ALLOWED_ACTORS):
        violations.append(f"actor: must be one of {sorted(ALLOWED_ACTORS)}")

    marker = record.get("marker")
    # #7: isinstance guard before enum membership (non-hashable → TypeError).
    if marker is not None and (not isinstance(marker, str) or marker not in ALLOWED_MARKERS):
        violations.append(f"marker: must be one of {sorted(ALLOWED_MARKERS)}")

    return OrchestrationRedactionResult(is_safe=not violations, violations=tuple(violations))


def assert_safe_orchestration_manifest(record: Mapping[str, Any]) -> None:
    result = check_orchestration_manifest(record)
    if not result.is_safe:
        raise OrchestrationRedactionError(list(result.violations))


def self_test() -> int:
    valid = {
        "schema_version": 1,
        "name": "review-implement-audit",
        "description": "claude reviews PR\ncodex implements\ngpt-5.5 audits",
        "steps": [
            {"id": "review", "actor": "claude", "intent": "review PR design", "inputs": [],
             "timeout_seconds": 600, "retry_max": 0, "on_fail": "abort"},
            {"id": "implement", "actor": "codex", "intent": "implement changes", "inputs": ["review"],
             "timeout_seconds": 1800, "retry_max": 1, "on_fail": "abort",
             "prompt_ref": "docs/prompts/implement.md"},
            {"id": "audit", "actor": "gpt-5.5", "intent": "audit final diff", "inputs": ["implement"],
             "timeout_seconds": 300, "retry_max": 0, "on_fail": "continue"},
        ],
        "boundaries": "AQG validates schema only",
        "marker": "orchestration-helper-manifest",
    }
    r = check_orchestration_manifest(valid)
    assert r.is_safe, r.violations

    # on_fail = retry rejected (was in old design)
    bad = dict(valid); bad["steps"] = [dict(s) for s in valid["steps"]]
    bad["steps"][0]["on_fail"] = "retry"
    assert not check_orchestration_manifest(bad).is_safe

    # duplicate step id
    bad = dict(valid); bad["steps"] = [dict(s) for s in valid["steps"]]
    bad["steps"][1]["id"] = "review"
    assert not check_orchestration_manifest(bad).is_safe

    # duplicate inputs in step
    bad = dict(valid); bad["steps"] = [dict(s) for s in valid["steps"]]
    bad["steps"][1]["inputs"] = ["review", "review"]
    assert not check_orchestration_manifest(bad).is_safe

    # cycle: A→B→A
    bad = dict(valid); bad["steps"] = [
        {"id": "a", "actor": "claude", "intent": "x", "inputs": ["b"],
         "timeout_seconds": 1, "retry_max": 0, "on_fail": "abort"},
        {"id": "b", "actor": "claude", "intent": "x", "inputs": ["a"],
         "timeout_seconds": 1, "retry_max": 0, "on_fail": "abort"},
    ]
    assert not check_orchestration_manifest(bad).is_safe

    # self-loop A→A
    bad = dict(valid); bad["steps"] = [
        {"id": "a", "actor": "claude", "intent": "x", "inputs": ["a"],
         "timeout_seconds": 1, "retry_max": 0, "on_fail": "abort"},
    ]
    assert not check_orchestration_manifest(bad).is_safe

    # timeout out of bounds
    bad = dict(valid); bad["steps"] = [dict(s) for s in valid["steps"]]
    bad["steps"][0]["timeout_seconds"] = 100000
    assert not check_orchestration_manifest(bad).is_safe

    # retry_max negative
    bad = dict(valid); bad["steps"] = [dict(s) for s in valid["steps"]]
    bad["steps"][0]["retry_max"] = -1
    assert not check_orchestration_manifest(bad).is_safe

    # retry_max > MAX_RETRY
    bad = dict(valid); bad["steps"] = [dict(s) for s in valid["steps"]]
    bad["steps"][0]["retry_max"] = 99
    assert not check_orchestration_manifest(bad).is_safe

    # reject bool as a timeout (gpt-5.5 #5)
    bad = dict(valid); bad["steps"] = [dict(s) for s in valid["steps"]]
    bad["steps"][0]["timeout_seconds"] = True
    assert not check_orchestration_manifest(bad).is_safe

    # description multiline OK (was reject before)
    ok = dict(valid); ok["description"] = "line1\nline2\nline3"
    assert check_orchestration_manifest(ok).is_safe

    # prompt_ref absolute path reject
    bad = dict(valid); bad["steps"] = [dict(s) for s in valid["steps"]]
    bad["steps"][1]["prompt_ref"] = "/etc/passwd"
    assert not check_orchestration_manifest(bad).is_safe

    # 'task-runner' false positive check
    ok = dict(valid); ok["description"] = "this task-runner uses disk-usage tools"
    assert check_orchestration_manifest(ok).is_safe, check_orchestration_manifest(ok).violations

    print("OK: _orchestration_redaction self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
