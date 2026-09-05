#!/usr/bin/env python3
"""Simulation manifest schema + leak guard (Wave 2 #4, framework v2.1 §7 #4).

Implemented per triple-audit (audit_id d7ca6aec) 14 accepted findings:
- parameters allow scalar OR list of scalar (3-auditor major)
- parameters key name sensitive denylist (gpt-5.5 #1 critical)
- bool not treated as int (type(v) is int); NaN/Infinity reject (gpt-5.5 #5)
- multiline fields (description/boundaries) allow newline; inline strict (gemini #2)
- token denylist via _secret_patterns shared regex (gemini #1 critical: word boundary prevents 'sk-' from misfiring on 'task-runner')
- expected_artifacts explicit reject leading /, ../, trailing / (o3 #2)
- SLUG_RE shared constant (o3 #4)

API:
    assert_safe_simulation_manifest(record: dict) -> None
    check_simulation_manifest(record: dict) -> SimulationRedactionResult
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from _redaction_common import scan_identifier, scan_leaks


# ===== Shared constants =====

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
SCHEMA_VERSION_ACCEPTED: frozenset[int] = frozenset({1})

ALLOWED_KINDS: frozenset[str] = frozenset({
    "api", "database", "network", "filesystem", "time", "queue", "other",
})

ALLOWED_ACTORS: frozenset[str] = frozenset({
    "claude", "codex", "gpt-5.5", "gemini", "o3", "human", "ci-bot", "other",
})

ALLOWED_MARKERS: frozenset[str] = frozenset({
    "simulation-mock-manifest", "manual",
})

# ===== Field name and length constraints =====

REQUIRED_TOP_LEVEL: frozenset[str] = frozenset({
    "schema_version", "name", "description", "kind", "duration_seconds", "boundaries",
})
OPTIONAL_TOP_LEVEL: frozenset[str] = frozenset({
    "seed", "parameters", "expected_artifacts", "actor", "marker",
})
ALLOWED_TOP_LEVEL: frozenset[str] = REQUIRED_TOP_LEVEL | OPTIONAL_TOP_LEVEL

MAX_NAME_LEN = 80
MAX_DESCRIPTION_LEN = 800
MAX_BOUNDARIES_LEN = 800
MAX_PARAM_KEY_LEN = 64
MAX_PARAM_VALUE_LEN = 200
MAX_PARAM_LIST_LEN = 50
MAX_ARTIFACTS = 30
MAX_ARTIFACT_PATH_LEN = 200
MAX_DURATION_SECONDS = 24 * 3600  # 1 day sanity bound

PARAM_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# Sensitive parameter key denylist (case-insensitive)
SENSITIVE_PARAM_KEYS: frozenset[str] = frozenset({
    "password", "secret", "token", "api_key", "apikey", "auth",
    "credential", "credentials", "dsn", "conn", "connection_string",
    "cookie", "session_id", "private_key", "privatekey",
})


@dataclass(frozen=True)
class SimulationRedactionResult:
    is_safe: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


class SimulationRedactionError(Exception):
    def __init__(self, violations: list[str]):
        self.violations = tuple(violations)
        super().__init__(self._fmt())

    def _fmt(self) -> str:
        if not self.violations:
            return "simulation manifest redaction violation (no detail)"
        lines = [f"simulation manifest redaction violation ({len(self.violations)} issue(s)):"]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


def _is_strict_int(v: Any) -> bool:
    """Reject bool (subclass of int) and non-int types."""
    return type(v) is int


def _is_finite_number(v: Any) -> bool:
    """Allow int (no bool) or finite float."""
    if type(v) is int:
        return True
    if isinstance(v, float) and not isinstance(v, bool):
        return math.isfinite(v)
    return False


def _check_path_safe(name: str, value: str, violations: list[str]) -> None:
    """Project-relative path validation + token leak scan.

    L1 fix (audit 4f0c48c0 #4): the prior version only ran token detection via
    _check_no_secret. The path charset regex below (`^[A-Za-z0-9._/\\-]+$`)
    already excludes the chars that email / URL / auth-header / IP patterns need
    (@ : ? space etc.), so prose-level scan_leaks would double-report path/PII
    classes. We keep the dedicated path checks (abs / UNC / .. / trailing-slash /
    Win-drive / charset) and run scan_identifier for the residual token-shape
    risk — same fail-closed token belt as prose.
    """
    if not value or len(value) > MAX_ARTIFACT_PATH_LEN:
        violations.append(f"{name}: empty or too long")
        return
    if value.startswith("/") or value.startswith("\\"):
        violations.append(f"{name}: absolute path not allowed")
    if value.startswith("//"):
        violations.append(f"{name}: UNC path not allowed")
    if value.endswith("/") or value.endswith("\\"):
        violations.append(f"{name}: trailing slash (must be file not dir)")
    parts = re.split(r"[/\\]", value)
    if ".." in parts:
        violations.append(f"{name}: contains '..' parent traversal")
    if re.match(r"^[A-Za-z]:[/\\]", value):
        violations.append(f"{name}: Windows drive path not allowed")
    if not re.match(r"^[A-Za-z0-9._/\-]+$", value):
        violations.append(f"{name}: contains disallowed characters")
    scan_identifier(name, value, violations)


def _check_parameter_value(label: str, val: Any, violations: list[str]) -> None:
    """parameter value: scalar or list of scalar; nested dict reject.

    `label` is a key-SUPPRESSED positional label (e.g. "parameters[0]") so a
    token-/secret-shaped parameter KEY is never echoed in a violation message
    (round-3 audit ebd87bef NEVER-echo fix).
    """
    if isinstance(val, dict):
        violations.append(f"{label}: nested dict not allowed (v1)")
        return
    if isinstance(val, list):
        if len(val) > MAX_PARAM_LIST_LEN:
            violations.append(f"{label}: list too long ({len(val)} > {MAX_PARAM_LIST_LEN})")
            return
        for i, item in enumerate(val):
            if isinstance(item, (dict, list)):
                violations.append(f"{label}[{i}]: list-of-list/dict not allowed")
                return
            _check_parameter_scalar(f"{label}[{i}]", item, violations)
        return
    _check_parameter_scalar(label, val, violations)


def _check_parameter_scalar(name: str, val: Any, violations: list[str]) -> None:
    if val is None:
        return
    if isinstance(val, bool):
        return  # bool OK
    if isinstance(val, str):
        if len(val) > MAX_PARAM_VALUE_LEN:
            violations.append(f"{name}: string too long")
            return
        # parameter string value is inline prose — scan_leaks(multiline=False)
        # subsumes the control-char check AND adds path/email/url/ip/header (#4).
        scan_leaks(name, val, violations, multiline=False)
        return
    if _is_finite_number(val):
        return
    violations.append(f"{name}: must be scalar (str/int/float/bool/null)")


def check_simulation_manifest(record: Mapping[str, Any]) -> SimulationRedactionResult:
    violations: list[str] = []

    if not isinstance(record, Mapping):
        return SimulationRedactionResult(
            is_safe=False,
            violations=(f"<root>: must be mapping, got {type(record).__name__}",),
        )

    # Unknown top-level (suppress key text)
    unknown_top = sum(1 for k in record.keys() if k not in ALLOWED_TOP_LEVEL)
    if unknown_top:
        violations.append(f"<root>: {unknown_top} unknown top-level field(s); key names suppressed")

    # Required + non-None
    for req in REQUIRED_TOP_LEVEL:
        if req not in record:
            violations.append(f"<root>.{req}: missing required field")
        elif record[req] is None:
            violations.append(f"<root>.{req}: required field cannot be null")

    # schema_version (strict int)
    sv = record.get("schema_version")
    if sv is not None and (not _is_strict_int(sv) or sv not in SCHEMA_VERSION_ACCEPTED):
        violations.append(f"schema_version: must be int in {sorted(SCHEMA_VERSION_ACCEPTED)}")

    # name (slug, inline)
    name = record.get("name")
    if name is not None:
        if not isinstance(name, str) or not SLUG_RE.match(name):
            violations.append("name: must match slug regex")
        else:
            # name is a slug identifier (SLUG_RE charset excludes newline/control)
            # — scan_identifier adds the token-shape belt (#3).
            scan_identifier("name", name, violations)

    # description (multiline OK) — post-impl convergent CRITICAL: type-check first
    desc = record.get("description")
    if desc is not None:
        if not isinstance(desc, str):
            violations.append(f"description: must be str, got {type(desc).__name__} (prevents dict/list from bypassing redaction)")
        else:
            if len(desc) > MAX_DESCRIPTION_LEN:
                violations.append(f"description: too long ({len(desc)} > {MAX_DESCRIPTION_LEN})")
            # multiline prose: scan_leaks subsumes control-char + adds path/email/
            # url/ip/header + markdown-heading injection guard (#4, #8).
            scan_leaks("description", desc, violations, multiline=True)

    # kind (enum) — #7: isinstance guard before membership (non-hashable → TypeError)
    kind = record.get("kind")
    if kind is not None and (not isinstance(kind, str) or kind not in ALLOWED_KINDS):
        violations.append(f"kind: must be one of {sorted(ALLOWED_KINDS)}")

    # duration_seconds (strict int, bounded)
    dur = record.get("duration_seconds")
    if dur is not None:
        if not _is_strict_int(dur) or dur < 0 or dur > MAX_DURATION_SECONDS:
            violations.append(f"duration_seconds: must be int 0-{MAX_DURATION_SECONDS}")

    # seed (strict int; allow negative)
    seed = record.get("seed")
    if seed is not None and not _is_strict_int(seed):
        violations.append("seed: must be int")

    # parameters (dict; values scalar or list of scalar)
    params = record.get("parameters")
    if params is not None:
        if not isinstance(params, dict):
            violations.append("parameters: must be mapping")
        else:
            for idx_k, (key, val) in enumerate(params.items()):
                label = f"parameters[{idx_k}]"  # positional — never echoes the raw key
                if not isinstance(key, str) or not PARAM_KEY_RE.match(key):
                    violations.append("parameters: invalid key (must match alphanumeric_underscore)")
                    continue
                # (#N4) the KEY itself must pass the token-shape scan — a key like
                # 'npm_<token>' fits PARAM_KEY_RE but is a leaked secret.
                # round-3 ebd87bef: if the key fails the scan, STOP — do NOT let a
                # later message (sensitive-key / value-type) echo a token-shape key.
                pre = len(violations)
                scan_identifier("parameter key", key, violations)
                if len(violations) > pre:
                    continue
                # #14: substring match — a prefixed/suffixed key like `db_password`,
                # `user_token`, `api_key_v2` previously bypassed exact membership.
                key_lc = key.lower()
                if any(sk in key_lc for sk in SENSITIVE_PARAM_KEYS):
                    violations.append(f"{label}: sensitive key name not allowed (key suppressed)")
                    continue
                _check_parameter_value(label, val, violations)

    # expected_artifacts (list of project-relative paths)
    arts = record.get("expected_artifacts")
    if arts is not None:
        if not isinstance(arts, list):
            violations.append("expected_artifacts: must be list")
        elif len(arts) > MAX_ARTIFACTS:
            violations.append(f"expected_artifacts: too many ({len(arts)} > {MAX_ARTIFACTS})")
        else:
            for i, p in enumerate(arts):
                if not isinstance(p, str):
                    violations.append(f"expected_artifacts[{i}]: must be str")
                    continue
                _check_path_safe(f"expected_artifacts[{i}]", p, violations)

    # boundaries (multiline) — post-impl convergent CRITICAL: type-check first
    bnd = record.get("boundaries")
    if bnd is not None:
        if not isinstance(bnd, str):
            violations.append(f"boundaries: must be str, got {type(bnd).__name__} (prevents dict/list from bypassing redaction)")
        else:
            if len(bnd) > MAX_BOUNDARIES_LEN:
                violations.append(f"boundaries: too long")
            # multiline prose: scan_leaks subsumes control-char + adds PII/path/
            # markdown-heading injection guard (#4, #8).
            scan_leaks("boundaries", bnd, violations, multiline=True)

    # actor / marker — #7: isinstance guard before membership (non-hashable → TypeError)
    actor = record.get("actor")
    if actor is not None and (not isinstance(actor, str) or actor not in ALLOWED_ACTORS):
        violations.append(f"actor: must be one of {sorted(ALLOWED_ACTORS)}")
    marker = record.get("marker")
    if marker is not None and (not isinstance(marker, str) or marker not in ALLOWED_MARKERS):
        violations.append(f"marker: must be one of {sorted(ALLOWED_MARKERS)}")

    return SimulationRedactionResult(is_safe=not violations, violations=tuple(violations))


def assert_safe_simulation_manifest(record: Mapping[str, Any]) -> None:
    result = check_simulation_manifest(record)
    if not result.is_safe:
        raise SimulationRedactionError(list(result.violations))


def self_test() -> int:
    valid = {
        "schema_version": 1,
        "name": "api-failure-30pct",
        "description": "simulate 30 percent failure rate on payment API\nmulti-line OK",
        "kind": "api",
        "duration_seconds": 300,
        "seed": 42,
        "parameters": {
            "failure_rate_percent": 30,
            "latency_ms_p50": 200,
            "targets": ["auth_api", "payment_api"],
        },
        "expected_artifacts": ["docs/sim-runs/api-failure.log"],
        "boundaries": "no production touched\nsynthetic only",
        "actor": "claude",
        "marker": "simulation-mock-manifest",
    }
    assert check_simulation_manifest(valid).is_safe, check_simulation_manifest(valid).violations

    # bool as schema_version reject
    bad = dict(valid); bad["schema_version"] = True
    assert not check_simulation_manifest(bad).is_safe

    # NaN duration reject
    bad = dict(valid); bad["duration_seconds"] = float("nan")
    assert not check_simulation_manifest(bad).is_safe

    # sensitive parameter key reject
    bad = dict(valid)
    bad["parameters"] = dict(valid["parameters"]); bad["parameters"]["password"] = "x"
    assert not check_simulation_manifest(bad).is_safe

    # nested dict in parameters reject
    bad = dict(valid)
    bad["parameters"] = dict(valid["parameters"]); bad["parameters"]["nested"] = {"a": 1}
    assert not check_simulation_manifest(bad).is_safe

    # list of dict in parameters reject
    bad = dict(valid)
    bad["parameters"] = dict(valid["parameters"]); bad["parameters"]["nested_list"] = [{"a": 1}]
    assert not check_simulation_manifest(bad).is_safe

    # unknown kind
    bad = dict(valid); bad["kind"] = "unknown"
    assert not check_simulation_manifest(bad).is_safe

    # multiline description with control char (not \n) reject
    bad = dict(valid); bad["description"] = "ok\x00"
    assert not check_simulation_manifest(bad).is_safe

    # description with newline OK (was reject before)
    ok = dict(valid); ok["description"] = "line1\nline2\nline3"
    assert check_simulation_manifest(ok).is_safe

    # 'task-runner' should NOT trigger sk- false positive
    ok = dict(valid); ok["description"] = "this is task-runner with disk-usage"
    assert check_simulation_manifest(ok).is_safe, check_simulation_manifest(ok).violations

    # absolute path in artifacts reject
    bad = dict(valid); bad["expected_artifacts"] = ["/etc/passwd"]
    assert not check_simulation_manifest(bad).is_safe

    # .. parent reject
    bad = dict(valid); bad["expected_artifacts"] = ["docs/../etc"]
    assert not check_simulation_manifest(bad).is_safe

    print("OK: _simulation_redaction self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
