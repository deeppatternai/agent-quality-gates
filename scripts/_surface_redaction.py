#!/usr/bin/env python3
"""Strict redaction boundary gatekeeper for the surface fingerprint (Wave 1-0 P0).

Implements the ENGINEERING_FRAMEWORK.md §6 strict-redaction schema:
the desktop-client surface fingerprint emitted by doctor `--mode session` must be
strictly redacted, allowing only metadata fields such as hash / status / count /
version / exists / host; any raw .env value / token / API key / full path / raw
document text / endpoint URL must raise + block.

Design principles:
- **allowlist-only**: any leaf field not in the allowlist is rejected outright (fail-closed).
- **type-strict**: each allowed field constrains its value type + content (host = hostname only / sha256_first8 = 8 hex / etc.).
- **unified leak scan**: string fields such as version / host plus category names / leaf key names
  all funnel through `_redaction_common` (scan_leaks / check_host_value / scan_identifier),
  fail-CLOSED on _secret_patterns ImportError, and NEVER echo the value (audit 4f0c48c0
  root cause A: eliminate the duplicated and drifting scan logic across guards).
- **key names validated too**: category names / leaf key names pass a safe-name regex + token scan,
  preventing a key name from carrying a token (e.g. category name = "ghp_<token>").
- **no mutation**: purely read-only checks; the fingerprint dict itself is not modified.
- **batched error**: a single raise reports all violations rather than failing fast (so the user sees them all at once).

API:
    assert_safe_fingerprint(fp) -> None  # raises RedactionError on a violation
    check_fingerprint(fp) -> RedactionResult  # non-raising variant

Wave 1-0 scope: this module + self-test. doctor `--mode session` is wired in during Wave 1 implementation.

Exit codes (self-test):
    0 — self-test PASS
    1 — self-test FAIL (assertion error or unexpected exception)

No third-party dependencies; only stdlib + the AQG-internal _secret_patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from _redaction_common import check_host_value, scan_identifier, scan_leaks


# ===== Allowed leaf fields =====
# Exact match: these leaf keys are allowed at any nested level
ALLOWED_LEAF_FIELDS: frozenset[str] = frozenset({
    # boolean / status
    "exists",
    "set",
    "is_directory",
    "verify_root",
    "logged_in",
    # string (restricted content)
    "host",
    "version",
    "sha256_first8",
    # int
    "size_bytes",
    "path_dirs_count",
})

# Suffix match: allow *_count / *_status / *_version / *_size_bytes / *_sha256_first8
ALLOWED_LEAF_SUFFIXES: tuple[str, ...] = (
    "_count",
    "_status",
    "_version",
    "_size_bytes",
    "_sha256_first8",
)

# Allowed status enum values (verify_root / *_status fields)
ALLOWED_STATUS_VALUES: frozenset[str] = frozenset({"pass", "fail", "warn", "ok", "error"})

# Upper bound on field string-value length (guards against leaking a large blob)
MAX_VERSION_LEN = 64
MAX_HOST_LEN = 128

# Safe-name regex (#2): category names / leaf key names allow only ASCII
# alphanumerics + . _ - (1-64 char). Any key name carrying a token / path /
# punctuation / whitespace is rejected -- prevents the key-name-as-leak bypass
# such as a category name = "ghp_<token>".
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class RedactionResult:
    """Return value of the non-raising API.

    is_safe: True only when violations is empty.
    violations: detailed list; each string is "category.field: reason".
    """

    is_safe: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


class RedactionError(Exception):
    """Raised by assert_safe_fingerprint on a violation. All violations are reported at once."""

    def __init__(self, violations: list[str]):
        self.violations = tuple(violations)
        super().__init__(self._format())

    def _format(self) -> str:
        if not self.violations:
            return "fingerprint redaction violation (no detail)"
        lines = [f"fingerprint redaction violation ({len(self.violations)} issue(s)):"]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


def is_leaf_field_allowed(name: str) -> bool:
    """Determine whether the field name is in the allowlist. For external testing / debugging."""
    if name in ALLOWED_LEAF_FIELDS:
        return True
    return any(name.endswith(suf) for suf in ALLOWED_LEAF_SUFFIXES)


def _check_scalar_value(name: str, value: Any, full_path: str, violations: list[str]) -> None:
    """Check a single scalar value's type + content. Append to violations on a hit.

    name: leaf field name (decides which type rule to check against)
    value: the actual value
    full_path: full path used in the error message (e.g., "claude_settings.api_key")
    """
    # bool / None — safe
    if isinstance(value, bool) or value is None:
        return

    # int — must be non-negative (size/count should not be negative)
    if isinstance(value, int):
        if value < 0:
            violations.append(f"{full_path}: negative int not allowed (got {value})")
        return

    # not str / int / bool / None — unsupported
    if not isinstance(value, str):
        violations.append(f"{full_path}: unsupported value type {type(value).__name__}")
        return

    # === string value checked by field-name category ===

    # version field: length-cap + full leak scan (#3 fail-CLOSED + #18)
    # This used to try/except ImportError then fail-OPEN (print warn, no append) —
    # when _secret_patterns was missing a token-as-version passed straight through.
    # Now routed through scan_leaks, which internally appends a violation on
    # ImportError (fail-closed) and NEVER echoes the value (#6).
    if name == "version" or name.endswith("_version"):
        if len(value) > MAX_VERSION_LEN:
            violations.append(
                f"{full_path}: version string too long "
                f"({len(value)} > {MAX_VERSION_LEN})"
            )
            return
        scan_leaks(full_path, value, violations, multiline=False)
        return

    # host field: hostname only (#1) — delegated to the shared helper.
    # The old implementation only rejected ("://","/","?","#"," "), missing
    # userinfo '@' / port ':' / a bare token, so admin:tok@github.com /
    # github.com:443 / a bare ghp_token all passed as host.
    # check_host_value rejects @ : scheme path query whitespace and runs
    # scan_leaks (secret/PII/path), and NEVER echoes the value (#6).
    if name == "host":
        if len(value) > MAX_HOST_LEN:
            violations.append(f"{full_path}: host too long ({len(value)} > {MAX_HOST_LEN})")
            return
        check_host_value(full_path, value, violations)
        return

    # sha256_first8 field: exactly 8 lowercase hex chars (#6: do not echo the raw value)
    if name == "sha256_first8" or name.endswith("_sha256_first8"):
        if not re.match(r"^[0-9a-f]{8}$", value):
            violations.append(f"{full_path}: not a valid 8-hex sha")
        return

    # verify_root / *_status field: enum (#6: do not echo the raw value)
    if name == "verify_root" or name.endswith("_status"):
        if value not in ALLOWED_STATUS_VALUES:
            violations.append(
                f"{full_path}: invalid status enum "
                f"(allowed: {sorted(ALLOWED_STATUS_VALUES)})"
            )
        return

    # other allowed string fields (in theory unreachable, since the allowlist already enumerates string fields)
    violations.append(
        f"{full_path}: string value not expected for field type "
        f"(only version/host/sha256_first8/verify_root/*_status accept strings)"
    )


def _validate_nested_dict(
    category: str,
    leaf_dict: Mapping[str, Any],
    violations: list[str],
) -> None:
    """Check a nested dict (the leaves inside a surface category).

    Every leaf key must be in the allowlist; nested-of-nested is not allowed (flat enforced).
    """
    for key, value in leaf_dict.items():
        full_path = f"{category}.{key}"

        # (#2) the leaf key name itself must also be validated -- prevents a key
        # name carrying a token / path (e.g. {"ghp_<token>_count": 1}, which the
        # old implementation waved through on the _count suffix).
        # safe-name regex first, then scan_identifier; on a hit, reject and stop.
        if not SAFE_NAME_RE.match(str(key)):
            violations.append(f"{category}.<leaf-key>: leaf key name fails safe-name pattern (key suppressed)")
            continue
        pre = len(violations)
        # safe field label (not the raw key) so a token-shape key is detected
        # WITHOUT echoing it into the violation message (NEVER-echo invariant).
        scan_identifier("leaf-key-name", str(key), violations)
        if len(violations) > pre:
            continue

        if isinstance(value, dict):
            violations.append(
                f"{full_path}: nested dict not allowed (only flat leaves)"
            )
            continue

        if not is_leaf_field_allowed(key):
            violations.append(f"{full_path}: field name not in allowlist")
            continue

        _check_scalar_value(key, value, full_path, violations)


def assert_safe_fingerprint(fingerprint: Mapping[str, Any]) -> None:
    """Main API: scan the fingerprint, raise RedactionError on any violation.

    fingerprint structure (per the §6 spec):
        {
            "<category>": {
                "<leaf_field>": <scalar>, ...  # nested dict
            },
            "<top_level_leaf>": <scalar>,  # a top-level leaf is also allowed
        }

    Raises:
        RedactionError: all violations reported at once (no fail-fast)
    """
    if not isinstance(fingerprint, Mapping):
        raise RedactionError([
            f"<root>: fingerprint must be a mapping, got {type(fingerprint).__name__}"
        ])

    violations: list[str] = []

    for category, value in fingerprint.items():
        # (#2) the category name itself is also validated -- the old implementation
        # "allowed any user-defined category name", letting a category name =
        # "ghp_<token>" pass straight through (a key-name-carries-secret bypass).
        # safe-name regex + scan_identifier; on a hit, reject this category and do not descend.
        if not SAFE_NAME_RE.match(str(category)):
            violations.append("<category>: category name fails safe-name pattern (name suppressed)")
            continue
        pre = len(violations)
        # safe field label (not the raw category) — NEVER-echo invariant.
        scan_identifier("category-name", str(category), violations)
        if len(violations) > pre:
            continue

        # type must be dict / scalar
        if isinstance(value, dict):
            # nested category: every leaf must be allowed
            _validate_nested_dict(category, value, violations)
        elif isinstance(value, (str, int, bool, type(None))):
            # top-level scalar: the category name itself must be an allowed leaf field
            if not is_leaf_field_allowed(category):
                violations.append(
                    f"{category}: top-level scalar key not in leaf allowlist "
                    "(must be like *_count / version / sha256_first8 / etc.)"
                )
                continue
            _check_scalar_value(category, value, category, violations)
        else:
            violations.append(
                f"{category}: unsupported value type {type(value).__name__}"
            )

    if violations:
        raise RedactionError(violations)


def check_fingerprint(fingerprint: Mapping[str, Any]) -> RedactionResult:
    """Non-raising variant: returns a RedactionResult holding is_safe + violations.

    Use case: a caller wants to handle violations itself (logging / continue / etc.) rather than catch an exception.
    """
    try:
        assert_safe_fingerprint(fingerprint)
        return RedactionResult(is_safe=True, violations=())
    except RedactionError as exc:
        return RedactionResult(is_safe=False, violations=exc.violations)


# ===== Self-test =====


def _make_clean_fingerprint() -> dict[str, Any]:
    """A compliant example (per the §6 spec example)."""
    return {
        "claude_settings_json": {
            "exists": True,
            "size_bytes": 1234,
            "sha256_first8": "a1b2c3d4",
            "hooks_count": 1,
            "permissions_count": 5,
            "mcp_servers_count": 3,
        },
        "env_AQG_ROOT": {
            "set": True,
            "is_directory": True,
            "verify_root": "pass",
        },
        "gh_auth": {
            "logged_in": True,
            "scopes_count": 3,
            "host": "github.com",
        },
        "path_dirs_count": 12,
        "claude_cli_version": "1.2.3",
    }


def self_test() -> int:
    """Cover the main allowed / forbidden cases of spec §6 + edge cases."""
    # === Clean case: a compliant fingerprint must PASS ===
    clean = _make_clean_fingerprint()
    assert_safe_fingerprint(clean)
    result = check_fingerprint(clean)
    assert result.is_safe and not result.violations, (
        f"clean fingerprint should pass, got {result}"
    )

    # === field-name violations ===
    # raw secret field not in allowlist
    bad_field = dict(clean)
    bad_field["openai_api_key"] = "sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    result = check_fingerprint(bad_field)
    assert not result.is_safe, "raw API key field should fail"
    assert any("openai_api_key" in v for v in result.violations), result.violations

    # full path as a key is not in allowlist
    bad_path = dict(clean)
    bad_path["full_home_path"] = "/Users/example/secret/path"
    result = check_fingerprint(bad_path)
    assert not result.is_safe, "full path field should fail"

    # === host field violations ===
    # host contains a path
    bad_host = _make_clean_fingerprint()
    bad_host["gh_auth"]["host"] = "github.com/orgs/secret"
    result = check_fingerprint(bad_host)
    assert not result.is_safe, "host with path should fail"
    assert any("host" in v for v in result.violations)

    # host contains a scheme
    bad_host_scheme = _make_clean_fingerprint()
    bad_host_scheme["gh_auth"]["host"] = "https://github.com"
    result = check_fingerprint(bad_host_scheme)
    assert not result.is_safe, "host with scheme should fail"

    # === sha256_first8 field violations ===
    # wrong length
    bad_sha = _make_clean_fingerprint()
    bad_sha["claude_settings_json"]["sha256_first8"] = "a1b2c3d4e5"  # 10 chars
    result = check_fingerprint(bad_sha)
    assert not result.is_safe, "sha256_first8 wrong length should fail"

    # not hex
    bad_sha2 = _make_clean_fingerprint()
    bad_sha2["claude_settings_json"]["sha256_first8"] = "ABCDEFGH"  # uppercase
    result = check_fingerprint(bad_sha2)
    assert not result.is_safe, "sha256_first8 uppercase should fail"

    # === status enum violation ===
    bad_status = _make_clean_fingerprint()
    bad_status["env_AQG_ROOT"]["verify_root"] = "unknown"
    result = check_fingerprint(bad_status)
    assert not result.is_safe, "verify_root invalid enum should fail"

    # === version field contains a secret pattern ===
    bad_version = _make_clean_fingerprint()
    # use a synthetic GitHub PAT as the version value (a typical leak scenario)
    bad_version["claude_cli_version"] = "gh" + "p_" + "a" * 32
    result = check_fingerprint(bad_version)
    assert not result.is_safe, "version with embedded secret should fail"
    assert any("secret" in v.lower() for v in result.violations), result.violations

    # version too long
    too_long_version = _make_clean_fingerprint()
    too_long_version["claude_cli_version"] = "x" * 100
    result = check_fingerprint(too_long_version)
    assert not result.is_safe, "version too long should fail"

    # === negative int rejected ===
    bad_int = _make_clean_fingerprint()
    bad_int["claude_settings_json"]["hooks_count"] = -1
    result = check_fingerprint(bad_int)
    assert not result.is_safe, "negative count should fail"

    # === nested-of-nested rejected (flat enforced) ===
    nested_nested = _make_clean_fingerprint()
    nested_nested["claude_settings_json"]["sub_object"] = {"foo": "bar"}
    result = check_fingerprint(nested_nested)
    assert not result.is_safe, "nested dict-of-dict should fail"

    # === assert_safe_fingerprint raise path ===
    try:
        assert_safe_fingerprint(bad_field)
    except RedactionError as exc:
        assert len(exc.violations) >= 1
        assert "openai_api_key" in str(exc)
    else:
        raise AssertionError("assert_safe_fingerprint should raise on violation")

    # === one raise reports all violations (not fail fast) ===
    multi_bad = _make_clean_fingerprint()
    multi_bad["bad1"] = "raw value"  # bad field name
    multi_bad["claude_settings_json"]["sha256_first8"] = "xyz"  # bad sha
    multi_bad["env_AQG_ROOT"]["verify_root"] = "unknown"  # bad enum
    result = check_fingerprint(multi_bad)
    assert not result.is_safe
    assert len(result.violations) >= 3, (
        f"should report all 3 violations, got {result.violations}"
    )

    # === non-mapping input ===
    result = check_fingerprint("not a dict")  # type: ignore[arg-type]
    assert not result.is_safe, "non-mapping input should fail"

    # === top-level leaf field: compliant ===
    only_top_leaf = {"path_dirs_count": 12, "version": "0.2.0"}
    assert_safe_fingerprint(only_top_leaf)

    # top-level scalar but key not in allowlist
    bad_top = {"random_field": "some_value"}
    result = check_fingerprint(bad_top)
    assert not result.is_safe

    # === edge: empty fingerprint OK ===
    assert_safe_fingerprint({})

    # === edge: bool is not int ===
    # Python bool is an int subclass; exists=True / size_bytes=0 should both work
    edge = {"claude_settings_json": {"exists": True, "size_bytes": 0}}
    assert_safe_fingerprint(edge)

    print("OK: _surface_redaction self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
