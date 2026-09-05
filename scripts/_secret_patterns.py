#!/usr/bin/env python3
"""Shared secret pattern definitions and counting.

Design principles:
- Single source of truth: the 3 CLI scripts (run_quality_gates, render_pr_comment,
  post_pr_comment) all import from here, avoiding drift.
- Conservative compatibility: the original 9 built-in pattern names and regexes are
  kept unchanged, new types are added as additional keys, so existing callers and
  tests do not regress.
- Extensible: inject additional regexes via the `AQG_EXTRA_SECRET_PATTERNS` environment
  variable (JSON: `{"name": "regex"}`). A bad regex only produces a stderr warning and
  is ignored, without affecting the built-in patterns.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Pattern


# Built-in patterns: keep the original 9 + add 9 more (adjusted after dual-audit review)
# Naming convention: snake_case, compatible with the original script
#
# Boundary-handling convention (unified after audit 633f922b f1):
# - Leading/trailing anchors always use lookbehind `(?<![A-Za-z0-9])` / lookahead `(?![A-Za-z0-9])`,
#   NOT `\b`. Reason: `_` is a word char (`\w`), so `\b` does not hold between `_` and a letter,
#   which would miss underscore-delimited real secrets ('AWS_KEY_AKIA…' / 'STRIPE_KEY_sk_live_…'). The lookaround
#   excludes only [A-Za-z0-9], matching the _redaction_common belt, and still blocks alnum-adjacent FPs.
# - For hyphen-containing base64url-style tokens (jwt, discord) the trailing end uses a negative lookahead
#   `(?![A-Za-z0-9_-])` (additionally excluding `-`), covering real secrets whose base64url tail contains `-`.
BUILTIN_PATTERNS: dict[str, Pattern[str]] = {
    # === Original 9 (PR-L3-1b tightens aws/slack/openai/bearer — see the per-line RC-* comments) ===
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    # RC-03 (PR-L3-1b): + ASIA (STS temp) / ABIA (STS bearer) / ACCA (context cred);
    # identifier prefixes (AIDA user / AROA role / AGPA group) stay EXCLUDED.
    # Anchor with lookbehind/lookahead, NOT \b: \b treats '_' as a word char, so \b
    # would MISS '_AKIA…' / 'AKIA…_' (audit 9b6283dd f1). Lookarounds exclude only
    # [A-Za-z0-9], matching the _redaction_common belt + detecting underscore-delimited keys.
    "aws_access_key": re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # RC-07 (PR-L3-1b): + xoxe- (refresh) / xoxc- (session/browser) / xapp- (app-level)
    # / xwfp- (workflow) tokens. Lookbehind, not \b, so underscore-delimited tokens
    # (SLACK_TOKEN_xoxb…) are still caught (audit 9b6283dd f1). xwfp- added per audit
    # 633f922b gpt-5.5 f2 (official Slack token docs citation).
    "slack_token": re.compile(r"(?<![A-Za-z0-9])(?:xox[baprsec]|xapp|xwfp)-[A-Za-z0-9-]+"),
    # RC-05 (PR-L3-1b): lookbehind stops the 'risk-proj-…' hyphenated-word FP (a letter
    # before sk- blocks the match) WITHOUT the \b underscore-FN bug — '…_sk-proj-…' is
    # still detected (audit 9b6283dd f1). A real token after a boundary char still matches.
    "openai_key": re.compile(r"(?<![A-Za-z0-9])sk-(?:proj|svcacct)-[A-Za-z0-9_-]{20,}|(?<![A-Za-z0-9])sk-[A-Za-z0-9]{32,}"),
    "anthropic_key": re.compile(r"sk-ant-(?:api03|admin01|sid01|sid)-[A-Za-z0-9_-]{50,}"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "service_account": re.compile(r'"type"\s*:\s*"service_account"'),
    # RC-04 (PR-L3-1b): two alternatives. (a) The original Authorization: Bearer form
    # matches ANY 20+ char token (incl. all-lowercase — RFC 6750 b64token allows it), so
    # no token previously caught is dropped (audit 633f922b: structured-only gating was a
    # FN regression). (b) A BARE "Bearer <token>" requires a digit/uppercase via lookahead
    # so a plain-English hyphenated phrase ("Bearer authentication-module-…") is not a FP
    # (audit 9b6283dd gemini f3); bare path uses lookbehind for underscore parity (audit
    # f1). Residual: a hyphenated phrase carrying a digit (…-v2-…) can still match the bare
    # path — accepted (LOW, fail-safe: redaction noise, not a missed secret).
    "bearer_token": re.compile(
        r"Authorization:\s*Bearer\s+[A-Za-z0-9._-]{20,}"
        r"|(?<![A-Za-z0-9])Bearer\s+(?=[A-Za-z0-9._-]*[0-9A-Z])[A-Za-z0-9._-]{20,}"
    ),
    # === Added 9 (dropped the originally planned twilio_account_sid — that is an identifier, not a secret) ===
    # Stripe secret / restricted key — covers live + test; a test-mode key can still access test resources.
    # Lookbehind not \b (audit 633f922b f1): \b misses 'STRIPE_KEY_sk_live_…' (underscore).
    "stripe_secret_key": re.compile(r"(?<![A-Za-z0-9])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"),
    # Stripe webhook signing secret — relates to production data
    "stripe_webhook_secret": re.compile(r"(?<![A-Za-z0-9])whsec_[A-Za-z0-9]{20,}"),
    # AWS secret access key — intentionally label-anchored: only matches when
    # an `aws_secret_access_key` label precedes the value (`AWS_SECRET_ACCESS_KEY=<40-char>`).
    # This is BY DESIGN to avoid false-positives on any 40-char base64 string;
    # a bare 40-char key without the label is NOT matched. (The label group, not
    # the captured key, is what triggers the match.)
    "aws_secret_access_key": re.compile(
        r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"
    ),
    # Discord bot token — three base64url segments; first segment width 24-26 chars covers the 19-digit snowflake,
    # last segment width 27-38 chars covers newer tokens; trailing negative lookahead prevents a hyphen-boundary miss.
    # Leading lookbehind not \b (audit 633f922b f1): underscore-adjacent parity.
    "discord_bot_token": re.compile(
        r"(?<![A-Za-z0-9])[MN][A-Za-z\d]{23,25}\.[\w-]{6,7}\.[\w-]{27,38}(?![A-Za-z0-9_-])"
    ),
    # Discord MFA-style user token
    "discord_mfa_token": re.compile(r"(?<![A-Za-z0-9])mfa\.[A-Za-z0-9_-]{84}(?![A-Za-z0-9_-])"),
    # Generic JWT — trailing negative lookahead fixes the hyphen boundary; leading lookbehind not \b (audit 633f922b f1)
    "jwt_token": re.compile(
        r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])"
    ),
    # HuggingFace user / org token — lookbehind/lookahead not \b: 'hf_' starts with a
    # word char, so \b misses 'HF_TOKEN_hf_…' (audit 633f922b f1).
    "huggingface_token": re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}(?![A-Za-z0-9])"),
    # AccountKey inside an Azure Storage connection string
    "azure_storage_key": re.compile(r"AccountKey=[A-Za-z0-9+/]{40,}={0,2}"),
    # npm publish token — lookbehind/lookahead not \b: 'npm_' starts with a word char,
    # so \b misses 'NPM_TOKEN_npm_…' (audit 633f922b f1).
    "npm_token": re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{30,}(?![A-Za-z0-9])"),
}


def _is_ci_environment() -> bool:
    """Detect whether we are in a CI / GitHub Actions context.

    In these environments the PR body text may be controlled by an external
    contributor (fork PR), so an arbitrary regex injected via
    AQG_EXTRA_SECRET_PATTERNS combined with the PR text can become a ReDoS attack
    surface. Python `re` has no per-match timeout; the lowest-cost defense is to
    disable extras by default in CI and only enable them when the user explicitly
    opts in.
    """
    return bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))


def _load_extra_patterns() -> dict[str, Pattern[str]]:
    """Read custom regexes from AQG_EXTRA_SECRET_PATTERNS.

    Format: a JSON object `{"name": "regex_string"}`. Mind JSON's backslash
    escaping: to express `\\d` you must write `"\\\\d"`; to express `\\b` you must
    write `"\\\\b"`, otherwise JSON treats `\\b` as the backspace (\\x08) character.

    Security constraints:
    - In CI (`GITHUB_ACTIONS=1` / `CI=1`) extras are disabled by default to prevent
      ReDoS (the PR body may be controlled by an external contributor). Set
      `AQG_ALLOW_EXTRA_PATTERNS_IN_CI=1` to explicitly opt in.
    - Bad JSON / bad regex / a name colliding with a built-in: only a stderr warning
      and skip, no exception raised.
    - A compiled regex containing the `\\x08` (backspace) character usually means the
      user did not double-escape \\b, so a targeted hint is given.

    Returns a freshly built dict; the caller decides how to merge it with the
    built-in patterns.
    """
    raw = os.environ.get("AQG_EXTRA_SECRET_PATTERNS")
    if not raw:
        return {}

    if _is_ci_environment() and not os.environ.get("AQG_ALLOW_EXTRA_PATTERNS_IN_CI"):
        print(
            "WARN: AQG_EXTRA_SECRET_PATTERNS is set in a CI environment "
            "(GITHUB_ACTIONS or CI); ignored to avoid ReDoS exposure on "
            "external PR text. Set AQG_ALLOW_EXTRA_PATTERNS_IN_CI=1 to opt in.",
            file=sys.stderr,
        )
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"WARN: AQG_EXTRA_SECRET_PATTERNS is not valid JSON ({exc.msg}); "
            "ignoring extras. Note: JSON requires double-escape — write "
            r'"\\d" not "\d", "\\b" not "\b".',
            file=sys.stderr,
        )
        return {}
    if type(data) is not dict:
        print(
            "WARN: AQG_EXTRA_SECRET_PATTERNS must be a JSON object; ignoring extras",
            file=sys.stderr,
        )
        return {}

    extras: dict[str, Pattern[str]] = {}
    for name, regex in data.items():
        if type(name) is not str or not name.strip():
            print(
                "WARN: AQG_EXTRA_SECRET_PATTERNS has non-string or empty key; "
                "skipping",
                file=sys.stderr,
            )
            continue
        if type(regex) is not str:
            print(
                f"WARN: AQG_EXTRA_SECRET_PATTERNS[{name!r}] regex must be a "
                "string; skipping",
                file=sys.stderr,
            )
            continue
        if name in BUILTIN_PATTERNS:
            print(
                f"WARN: AQG_EXTRA_SECRET_PATTERNS[{name!r}] collides with a "
                "built-in name; skipping (built-in wins)",
                file=sys.stderr,
            )
            continue
        # Detect the common mistake of a user not double-escaping the backslash: JSON parses \b as \x08
        if "\x08" in regex:
            print(
                f"WARN: AQG_EXTRA_SECRET_PATTERNS[{name!r}] regex contains "
                r'literal \x08 (backspace); did you mean "\\b" (double-escape '
                r'in JSON)? Skipping.',
                file=sys.stderr,
            )
            continue
        try:
            extras[name] = re.compile(regex)
        except re.error as exc:
            print(
                f"WARN: AQG_EXTRA_SECRET_PATTERNS[{name!r}] is invalid regex "
                f"({exc}); skipping",
                file=sys.stderr,
            )
    return extras


def get_patterns() -> dict[str, Pattern[str]]:
    """Return the merged dict of built-in + extras.

    Re-reads env on every call so that monkey-patching environment variables in
    tests takes effect immediately. In production the CLI calls this once per
    single-shot process, so performance is not a bottleneck.
    """
    merged = dict(BUILTIN_PATTERNS)
    merged.update(_load_extra_patterns())
    return merged


# === RC-01 (#236): document-size cap to bound regex-scan cost (fail-closed) ===
# secret_counts scans whole DOCUMENTS (PR bodies, comments, serialized gate JSON)
# with this token bank. The BUILTIN patterns are prefix/literal-anchored and
# stay ~linear even on large input, so this cap is primarily a fail-closed
# document-size backstop — notably for user-injected AQG_EXTRA_SECRET_PATTERNS,
# which CAN be quadratic/catastrophic (extras are already disabled in CI). The
# quadratic _EMAIL_RE that motivated RC-01 lives on the per-field path
# (_redaction_common._FIELD_SCAN_MAX_CHARS, a tighter 8 KiB cap), NOT here.
# An input longer than this cap is NOT scanned (cost stays O(1)) and is reported
# fail-CLOSED via SCAN_CAP_EXCEEDED_KEY, so a count-gating caller still blocks;
# truncate-and-scan would be fail-OPEN (a secret past the cap silently certified
# clean). 256 KiB is far above any legitimate document (GitHub PR/issue body
# <= 64 KiB), so real scans never change.
SECRET_SCAN_MAX_CHARS = 262_144  # 256 KiB
SCAN_CAP_EXCEEDED_KEY = "__input_exceeds_scan_cap__"


def secret_counts(text: str, *, patterns: dict[str, Pattern[str]] | None = None) -> dict[str, int]:
    """Count the hits of each secret pattern in text, returning {name: count} (matched entries only).

    `patterns` defaults to a dynamic read via get_patterns() on every call (including
    runtime env changes); tests can explicitly inject a fixed patterns dict to isolate
    the effect of environment variables.

    RC-01 (#236): an input longer than SECRET_SCAN_MAX_CHARS is NOT scanned;
    it returns the fail-CLOSED sentinel {SCAN_CAP_EXCEEDED_KEY: 1} so every
    count-gating caller treats it as a leak rather than certifying it clean.
    """
    if len(text) > SECRET_SCAN_MAX_CHARS:
        return {SCAN_CAP_EXCEEDED_KEY: 1}
    active = patterns if patterns is not None else get_patterns()
    counts: dict[str, int] = {}
    for name, pattern in active.items():
        count = len(pattern.findall(text))
        if count:
            counts[name] = count
    return counts


# Backward compatibility: keep the module-level name, but each access resolves env as of import time.
# Current callers all use `from _secret_patterns import secret_counts` (function-level),
# not a direct import of SECRET_PATTERNS; it is kept only for external-script compatibility.
SECRET_PATTERNS = get_patterns()


def self_test() -> int:
    """Offline self-test: covers a minimal positive case for each pattern plus the added security constraints."""
    # Ensure the test environment has no CI interference (doctor itself may run in CI)
    os.environ.pop("AQG_EXTRA_SECRET_PATTERNS", None)
    saved_ci = os.environ.pop("CI", None)
    saved_gh = os.environ.pop("GITHUB_ACTIONS", None)
    try:
        # Build synthetic samples from fragments so the repository's own
        # secret scan stays clean while runtime self-tests still exercise the
        # exact sensitive shapes.
        samples = {
            # Original 9 sanity checks
            "github_token": "gh" + "p_" + "a" * 32,
            "aws_access_key": "AK" + "IA" + "1234567890ABCDEF",
            "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----",
            "slack_token": "xo" + "xb-" + "1234-5678-abcdef",
            "openai_key": "sk-" + "proj-" + "a" * 30,
            "anthropic_key": "sk-ant-" + "api03-" + "a" * 60,
            "google_api_key": "AI" + "za" + "B" * 35,
            "service_account": '"type": "' + "service_" + 'account"',
            "bearer_token": "Authorization: " + "Bearer " + "abcDEF123ghijklmnopqrstu",
            # Added 9 (dropped twilio_account_sid — that is an identifier, not a secret)
            "stripe_secret_key": "sk_test_" + "a" * 24,
            "stripe_webhook_secret": "whsec_" + "a" * 24,
            "aws_secret_access_key": 'AWS_SECRET_ACCESS_KEY="' + "a" * 40 + '"',
            "discord_bot_token": "M" + "a" * 24 + "." + "a" * 6 + "." + "a" * 30,
            "discord_mfa_token": "mfa." + "a" * 84,
            "jwt_token": "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
            "huggingface_token": "hf_" + "a" * 35,
            "azure_storage_key": "AccountKey=" + "a" * 80 + "==",
            "npm_token": "npm_" + "a" * 36,
        }
        for name, sample in samples.items():
            counts = secret_counts(sample, patterns=BUILTIN_PATTERNS)
            assert name in counts, f"{name} should match its own sample, got {counts}"

        # Boundary regression (gemini audit finding): a JWT ending in a hyphen must not be missed by \b
        jwt_ending_with_hyphen = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 11 + "-"
        counts = secret_counts(jwt_ending_with_hyphen, patterns=BUILTIN_PATTERNS)
        assert counts.get("jwt_token") == 1, (
            f"JWT ending with hyphen should match (gemini audit): got {counts}"
        )

        # Boundary regression: a Discord token ending in a hyphen
        discord_ending_with_hyphen = "M" + "a" * 24 + "." + "a" * 6 + "." + "a" * 29 + "-"
        counts = secret_counts(discord_ending_with_hyphen, patterns=BUILTIN_PATTERNS)
        assert counts.get("discord_bot_token") == 1, (
            f"Discord ending with hyphen should match: got {counts}"
        )

        # Twilio Account SID is no longer detected (gpt-5.5 audit finding: an identifier, not a secret)
        counts = secret_counts("AC" + "a" * 32, patterns=BUILTIN_PATTERNS)
        assert "twilio_account_sid" not in counts, (
            "Twilio SID is identifier-only and should NOT trigger secret detection"
        )

        # Text with no secret should not produce a false positive
        benign = "this is a normal sentence with no tokens, only words"
        assert secret_counts(benign, patterns=BUILTIN_PATTERNS) == {}

        # === PR-L3-1b detection-gap regressions (RC-03/04/05/07; audits 9b6283dd + 633f922b) ===
        # RC-03: AWS temp/credential prefixes detected; identifier prefixes excluded.
        for _pre in ("ASIA", "ABIA", "ACCA"):
            assert secret_counts(_pre + "B" * 16, patterns=BUILTIN_PATTERNS).get("aws_access_key") == 1, _pre
        for _pre in ("AIDA", "AROA", "AGPA"):
            assert "aws_access_key" not in secret_counts(_pre + "B" * 16, patterns=BUILTIN_PATTERNS), _pre
        # f1: lookbehind/lookahead (not \b) keeps underscore-delimited keys detected
        # while still excluding alphanumeric-adjacent ones.
        _akey = "AK" + "IA" + "1234567890ABCDEF"
        assert secret_counts("AWS_KEY_" + _akey, patterns=BUILTIN_PATTERNS).get("aws_access_key") == 1
        assert secret_counts(_akey + "_log", patterns=BUILTIN_PATTERNS).get("aws_access_key") == 1
        assert "aws_access_key" not in secret_counts("x" + _akey, patterns=BUILTIN_PATTERNS)
        # f1 sister parity (audit 633f922b): EVERY \b-anchored pattern moved to lookaround,
        # so underscore-delimited stripe/jwt/hf/npm secrets are detected too.
        assert secret_counts("STRIPE_KEY_" + "sk_live_" + "a" * 24, patterns=BUILTIN_PATTERNS).get("stripe_secret_key") == 1
        assert secret_counts("HF_TOKEN_" + "hf_" + "a" * 35, patterns=BUILTIN_PATTERNS).get("huggingface_token") == 1
        assert secret_counts("NPM_TOKEN_" + "npm_" + "a" * 36, patterns=BUILTIN_PATTERNS).get("npm_token") == 1
        _jwt = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
        assert secret_counts("JWT_" + _jwt, patterns=BUILTIN_PATTERNS).get("jwt_token") == 1
        # RC-04 (audit 633f922b): Authorization: path matches ANY 20+ token incl. all-lowercase
        # (RFC 6750 b64token) — no FN regression; bare path needs a digit/uppercase; phrases clean.
        assert secret_counts("Authorization: Bearer " + "a" * 24, patterns=BUILTIN_PATTERNS).get("bearer_token") == 1
        assert secret_counts("Bearer " + "ABCDEF1234567890GHIJ", patterns=BUILTIN_PATTERNS).get("bearer_token") == 1
        assert "bearer_token" not in secret_counts(
            "Bearer authentication-module-with-long-name", patterns=BUILTIN_PATTERNS
        )
        assert "bearer_token" not in secret_counts("Bearer of bad news", patterns=BUILTIN_PATTERNS)
        # RC-05: lookbehind stops 'risk-proj-…' FP; underscore-delimited + real tokens hit.
        assert "openai_key" not in secret_counts(
            "risk-proj-ection-and-management-system-v2", patterns=BUILTIN_PATTERNS
        )
        assert secret_counts("sk-proj-" + "a" * 24, patterns=BUILTIN_PATTERNS).get("openai_key") == 1
        assert secret_counts("OPENAI_KEY_sk-proj-" + "a" * 24, patterns=BUILTIN_PATTERNS).get("openai_key") == 1
        # RC-07: slack refresh / session / app-level / workflow token prefixes.
        for _pre in ("xoxe", "xoxc", "xapp", "xwfp"):
            assert secret_counts(_pre + "-1234-5678-abcd", patterns=BUILTIN_PATTERNS).get("slack_token") == 1, _pre

        # === extras injection (non-CI path) ===
        os.environ["AQG_EXTRA_SECRET_PATTERNS"] = json.dumps({"my_token": r"MYTKN_[A-Z0-9]{16}"})
        extras = _load_extra_patterns()
        assert "my_token" in extras, extras
        counts = secret_counts("found MYTKN_ABCDEFGHIJKLMNOP here")
        assert counts.get("my_token") == 1, counts

        # Bad JSON does not crash
        os.environ["AQG_EXTRA_SECRET_PATTERNS"] = "{not json"
        assert _load_extra_patterns() == {}

        # Bad regex does not crash
        os.environ["AQG_EXTRA_SECRET_PATTERNS"] = json.dumps({"bad": "[invalid"})
        assert _load_extra_patterns() == {}

        # A name colliding with a built-in is rejected
        os.environ["AQG_EXTRA_SECRET_PATTERNS"] = json.dumps({"github_token": r"foo"})
        extras = _load_extra_patterns()
        assert "github_token" not in extras

        # \x08 backspace (user wrote \b without double-escaping) is detected and rejected
        os.environ["AQG_EXTRA_SECRET_PATTERNS"] = json.dumps({"oops": "\bsecret"})
        extras = _load_extra_patterns()
        assert "oops" not in extras, "regex with \\x08 should be rejected"

        # === Extras disabled by default in a CI environment (gpt-5.5 audit finding: ReDoS defense) ===
        os.environ["AQG_EXTRA_SECRET_PATTERNS"] = json.dumps({"x": "y"})
        os.environ["GITHUB_ACTIONS"] = "true"
        assert _load_extra_patterns() == {}, "extras should be ignored in CI by default"

        # After opt-in, CI accepts them too
        os.environ["AQG_ALLOW_EXTRA_PATTERNS_IN_CI"] = "1"
        extras = _load_extra_patterns()
        assert "x" in extras, "AQG_ALLOW_EXTRA_PATTERNS_IN_CI=1 should re-enable extras"

    finally:
        os.environ.pop("AQG_EXTRA_SECRET_PATTERNS", None)
        os.environ.pop("AQG_ALLOW_EXTRA_PATTERNS_IN_CI", None)
        os.environ.pop("GITHUB_ACTIONS", None)
        if saved_ci is not None:
            os.environ["CI"] = saved_ci
        if saved_gh is not None:
            os.environ["GITHUB_ACTIONS"] = saved_gh
    print("OK: _secret_patterns self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
