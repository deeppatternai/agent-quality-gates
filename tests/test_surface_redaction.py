#!/usr/bin/env python3
"""Pytest/unittest regression for surface fingerprint redaction guardrail (Wave 1-0)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

# isort/lint may move these; keep below sys.path append intentionally
from _surface_redaction import (  # noqa: E402
    RedactionError,
    assert_safe_fingerprint,
    check_fingerprint,
    is_leaf_field_allowed,
    self_test as module_self_test,
)


class SurfaceRedactionRegressionTest(unittest.TestCase):
    """Per-case unittest assertions for high-frequency cases (independent of the module self_test)."""

    def test_module_self_test_passes(self) -> None:
        """Wave 1-0 gate: the module's built-in self_test must PASS (guards against spec drift)."""
        self.assertEqual(module_self_test(), 0)

    def test_clean_fingerprint_passes(self) -> None:
        clean = {
            "claude_settings_json": {
                "exists": True,
                "size_bytes": 1234,
                "sha256_first8": "a1b2c3d4",
                "hooks_count": 1,
            },
            "gh_auth": {"logged_in": True, "host": "github.com", "scopes_count": 3},
            "path_dirs_count": 12,
            "claude_cli_version": "1.2.3",
        }
        # no raise == pass
        assert_safe_fingerprint(clean)
        result = check_fingerprint(clean)
        self.assertTrue(result.is_safe)
        self.assertEqual(result.violations, ())

    def test_raw_api_key_field_rejected(self) -> None:
        """spec §6: raw token / api_key field names are not in the allowlist -> reject."""
        bad = {"openai_api_key": "sk-" + "proj-" + "a" * 30}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)
        self.assertTrue(any("openai_api_key" in v for v in result.violations))

    def test_host_with_path_rejected(self) -> None:
        """spec §6: host = hostname only (no path / scheme / query)."""
        bad = {"gh_auth": {"host": "github.com/orgs/secret"}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_host_with_scheme_rejected(self) -> None:
        bad = {"gh_auth": {"host": "https://github.com"}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_invalid_status_enum_rejected(self) -> None:
        """spec §6: verify_root / *_status restricted to pass/fail/warn/ok/error."""
        bad = {"env_AQG_ROOT": {"verify_root": "unknown"}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_sha256_first8_must_be_8_lowercase_hex(self) -> None:
        for bad_value in ("ABCDEFGH", "a1b2c3d4e5", "xyz", "a1b2c3d", ""):
            bad = {"claude_settings_json": {"sha256_first8": bad_value}}
            result = check_fingerprint(bad)
            self.assertFalse(
                result.is_safe,
                f"sha256_first8={bad_value!r} should be rejected",
            )

    def test_negative_count_rejected(self) -> None:
        bad = {"claude_settings_json": {"hooks_count": -1}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_nested_dict_of_dict_rejected(self) -> None:
        """Enforce flat: nested-of-nested is not allowed."""
        bad = {"claude_settings_json": {"sub": {"foo": "bar"}}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_assert_safe_aggregates_all_violations(self) -> None:
        """On violations, raise once with all of them (no fail-fast) so the user can fix them in one pass."""
        multi_bad = {
            "raw_token_field": "x",  # bad name
            "claude_settings_json": {
                "sha256_first8": "xyz",  # bad sha
                "hooks_count": -2,  # bad int
            },
            "env_AQG_ROOT": {"verify_root": "unknown"},  # bad enum
        }
        with self.assertRaises(RedactionError) as ctx:
            assert_safe_fingerprint(multi_bad)
        self.assertGreaterEqual(
            len(ctx.exception.violations), 4,
            f"should aggregate all 4 violations, got {ctx.exception.violations}",
        )

    def test_version_with_embedded_secret_rejected(self) -> None:
        """The version field is passed through _secret_patterns a second time for secret patterns."""
        bad = {"claude_cli_version": "gh" + "p_" + "a" * 32}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_top_level_scalar_must_be_in_allowlist(self) -> None:
        bad = {"random_string_field": "some_value"}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_top_level_allowed_leaves_pass(self) -> None:
        clean = {"path_dirs_count": 10, "version": "0.2.0"}
        assert_safe_fingerprint(clean)

    def test_empty_fingerprint_is_safe(self) -> None:
        """Boundary: an empty fingerprint is treated as safe (no leak)."""
        assert_safe_fingerprint({})

    def test_non_mapping_input_rejected(self) -> None:
        result = check_fingerprint("not a dict")  # type: ignore[arg-type]
        self.assertFalse(result.is_safe)

    # ===== three-round audit 4f0c48c0 bypass regression (root cause A: unified _redaction_common) =====

    def test_host_with_userinfo_rejected(self) -> None:
        """#1: host userinfo (admin:tok@github.com) must be rejected —
        the old implementation only rejected ('://','/','?','#',' '), missing '@' / ':'."""
        bad = {"gh_auth": {"host": "admin:tok@github.com"}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)
        self.assertTrue(any("host" in v for v in result.violations))

    def test_host_with_port_rejected(self) -> None:
        """#1: host port (github.com:443) must be rejected."""
        bad = {"gh_auth": {"host": "github.com:443"}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)
        self.assertTrue(any("host" in v for v in result.violations))

    def test_host_bare_token_rejected(self) -> None:
        """#1: a bare token used as host must be rejected by the secret scan."""
        bad = {"gh_auth": {"host": "ghp_" + "a" * 36}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_host_plain_hostname_still_passes(self) -> None:
        """Compatibility regression (e): host='github.com' must still PASS."""
        clean = {"gh_auth": {"logged_in": True, "host": "github.com"}}
        assert_safe_fingerprint(clean)
        self.assertTrue(check_fingerprint(clean).is_safe)

    def test_category_name_carrying_token_rejected(self) -> None:
        """#2: a category name = 'ghp_<token>' must be rejected —
        the old implementation allowed arbitrary user-defined category names."""
        bad = {"ghp_" + "a" * 36: {"exists": True}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_leaf_key_name_carrying_token_rejected(self) -> None:
        """#2: a leaf key name carrying a token (even with an allowed _count suffix) must be rejected."""
        bad = {"gh_auth": {"ghp_" + "a" * 36 + "_count": 1}}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_category_name_with_unsafe_chars_rejected(self) -> None:
        """#2: a category name containing path / whitespace or other unsafe characters -> rejected by the safe-name regex."""
        for bad_name in ("../etc/passwd", "has space", "a/b", "x" * 65):
            bad = {bad_name: {"exists": True}}
            result = check_fingerprint(bad)
            self.assertFalse(
                result.is_safe, f"category name {bad_name!r} should be rejected"
            )

    def test_version_secret_fail_closed(self) -> None:
        """#3/#18: a version field containing a token must be rejected (scan_leaks is fail-closed,
        no longer fail-open print-warn-and-pass)."""
        bad = {"claude_cli_version": "ghp_" + "a" * 32}
        result = check_fingerprint(bad)
        self.assertFalse(result.is_safe)

    def test_violation_messages_do_not_echo_value(self) -> None:
        """#6: violation messages do not echo the raw value (host/sha/status slices removed)."""
        cases = [
            {"claude_settings_json": {"sha256_first8": "S3CR3Tvalue"}},
            {"env_x": {"verify_root": "LEAKEDstatus"}},
        ]
        secrets = ("S3CR3T", "LEAKED")
        for fp, secret in zip(cases, secrets):
            result = check_fingerprint(fp)
            self.assertFalse(result.is_safe)
            for v in result.violations:
                self.assertNotIn(
                    secret, v, f"violation message leaked value slice: {v!r}"
                )

    def test_is_leaf_field_allowed_helpers(self) -> None:
        """Predicate exposed for external tests / debugging."""
        # exact match
        self.assertTrue(is_leaf_field_allowed("exists"))
        self.assertTrue(is_leaf_field_allowed("host"))
        self.assertTrue(is_leaf_field_allowed("size_bytes"))
        # suffix match
        self.assertTrue(is_leaf_field_allowed("hooks_count"))
        self.assertTrue(is_leaf_field_allowed("ci_status"))
        self.assertTrue(is_leaf_field_allowed("claude_cli_version"))
        # not in allowlist
        self.assertFalse(is_leaf_field_allowed("api_key"))
        self.assertFalse(is_leaf_field_allowed("password"))
        self.assertFalse(is_leaf_field_allowed("raw_value"))


if __name__ == "__main__":
    unittest.main()
