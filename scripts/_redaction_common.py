#!/usr/bin/env python3
"""Shared leak-scan helper for all redaction guards (L1 three-audit 4f0c48c0 fix).

WHY THIS MODULE EXISTS — root-cause A of audit 4f0c48c0 (2026-06-01):
Before this, secret/PII/path scanning was duplicated and DIVERGENT across guards:
  - bugfix/incident re-implemented a local 13-prefix _TOKEN_PREFIXES list
  - orchestration/simulation/surface used _secret_patterns (token-only, no path/PII)
This drift produced multiple false-NEGATIVE bypasses (a real secret/PII/path PASSES
the guard) — three-audit findings #1-6, #16, #18.

This module is the SINGLE leak-scan codepath. Every guard string field — prose,
identifier (slug/name/id), user-controlled key, and host — funnels through here.

INVARIANTS (do not regress):
- fail-CLOSED: if _secret_patterns cannot be imported, append a violation
  (never silently skip — that was the _surface_redaction fail-open bug #18).
- NEVER echo the matched value: violation messages carry field-name + pattern
  CATEGORY only, so the rejection message itself cannot leak the secret.
- substring (not startswith / not whitespace-anchored): a token embedded mid-string
  or a path after a punctuation char must still be caught (#4, #15).

API:
    scan_leaks(field, value, violations, *, multiline=False) -> None
    check_host_value(field, value, violations) -> None
    scan_identifier(field, value, violations) -> None

stdlib + AQG _secret_patterns only.
"""

from __future__ import annotations

import re


# ===== Token-shape prefix belt (word-boundary backstop; complements _secret_patterns) =====
# _secret_patterns.secret_counts is the primary, word-boundary-aware detector
# (18 patterns). This prefix belt is a backstop that also catches a token embedded
# in the MIDDLE of an otherwise-valid string (#15: startswith was the wip bug) —
# e.g. a real token after a quote/bracket/hyphen/space.
#
# WORD-BOUNDARY anchor (audit 4f0c48c0 follow-up, 2026-06-01): the prefix must be
# preceded by start-of-string OR a non-[A-Za-z0-9] char. A bare substring belt
# (`prefix in value`) over-fired: 'sk-' matched inside 'ta>sk-<runner' / 'di>sk-<usage',
# a documented false-positive (gemini #1 critical) that the primary detector's
# word boundaries already avoid. Anchoring the belt restores parity with
# _secret_patterns WITHOUT losing mid-string real tokens (those are preceded by a
# boundary char like '-' or a quote). Prefixes are matched longest-first so the
# most specific category (e.g. 'sk-ant-' over 'sk-') is the one reported.
_TOKEN_PREFIXES: tuple[str, ...] = tuple(sorted((
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "sk-", "sk-ant-", "AKIA", "ASIA", "xoxb-", "xoxp-", "xoxa-", "xoxr-",
    "AIza", "hf_", "npm_", "whsec_", "rk_live_", "rk_test_",
    "sk_live_", "sk_test_",
), key=len, reverse=True))
# Compiled boundary-anchored matcher per prefix (lookbehind = not preceded by alnum).
_TOKEN_PREFIX_RES: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (p, re.compile(r"(?<![A-Za-z0-9])" + re.escape(p))) for p in _TOKEN_PREFIXES
)

# ===== PII / path / injection patterns =====
# Absolute POSIX path — boundary fix (#4): prefix is line-start OR any char that
# is NOT word/./-//, so a path after a quote/bracket (file="/etc/shadow") is caught.
_ABS_POSIX_PATH_RE = re.compile(r"(?:^|[^\w./\-])(/[A-Za-z0-9]+(?:/[\w.\-]+)+)")
_WIN_DRIVE_RE = re.compile(r"\b[A-Za-z]:\\")
_UNC_RE = re.compile(r"\\\\[A-Za-z0-9]")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----")
_URL_RE = re.compile(r"https?://\S+")
_LONG_BASE64_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")
# IPv4 — kept permissive (a dotted version like 1.2.3.4 is syntactically an IP;
# fail-closed = accept the rare false-positive). audit #19 LOW.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# IPv6 — tightened (#19): require >= 4 hextet groups OR a `::` compression, so a
# 3-group timestamp like 12:34:56 no longer false-matches.
_IPV6_RE = re.compile(
    r"(?:[0-9a-fA-F]{1,4}:){4,}[0-9a-fA-F]{0,4}"  # >=4 colon groups
    r"|(?:[0-9a-fA-F]{1,4}:){1,}:[0-9a-fA-F]{0,4}"  # contains :: compression
    r"|::[0-9a-fA-F]{1,4}"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(?:Authorization|Cookie|Set-Cookie|X-API-Key|Bearer|Token|"
    r"sessionid|session_id|csrf|xsrf)\s*[:=]\s*\S+"
)
_LABELED_PHONE_RE = re.compile(
    r"(?i)\b(?:phone|tel|mobile|whatsapp)\s*[:=]\s*[+\d][\d\s\-()]{6,}"
)
_HTML_COMMENT_RE = re.compile(r"<!--|-->")
# Lone surrogate code points U+D800–DFFF — not UTF-8 encodable (compiled C-level
# scan; see the surrogate reject in scan_leaks).
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
# Unicode line/paragraph separators that str.splitlines() honours but are >= 0x20
# (so the <32 control-char check misses them): NEL U+0085, LS U+2028, PS U+2029.
_UNICODE_LINESEP = ("\x85", "\u2028", "\u2029")


def _check_tokens(field: str, value: str, violations: list[str]) -> None:
    """Token detection: _secret_patterns (primary, fail-closed) + prefix belt."""
    try:
        from _secret_patterns import secret_counts
    except ImportError:
        # fail-CLOSED (#18): a missing dependency must NOT silently disable the
        # guard. Append an internal violation so the record is rejected.
        violations.append(f"{field}: internal — _secret_patterns unavailable; failing closed")
    else:
        counts = secret_counts(value)
        if counts:
            violations.append(
                f"{field}: secret pattern detected ({', '.join(sorted(counts))}); raw value suppressed"
            )
    # word-boundary belt — catches a real token embedded mid-string (#15) while
    # NOT firing on a prefix buried inside a plain word (e.g. 'sk-' in 'task-runner').
    for prefix, prefix_re in _TOKEN_PREFIX_RES:
        if prefix_re.search(value):
            violations.append(f"{field}: contains token-shape prefix {prefix!r}")
            break  # one is enough; never echo full value


# RC-01 (#236): per-field leak-scan input cap. scan_leaks / scan_identifier run
# the O(N^2)-prone _EMAIL_RE (\b[A-Za-z0-9._%+-]+@…) plus 12 other `.search`
# patterns and the 24-prefix belt over each value. _EMAIL_RE is empirically
# quadratic on a long local-part-class run with no '@' (~1.6s at 40 KiB, ~25s at
# 160 KiB), and a guard's MAX_*_LEN check appends a violation but does NOT skip
# the scan_leaks call, so an oversized field still reaches the bank. Every field
# here is schema-capped tiny (<= 800 chars), so 8 KiB is a 10x backstop that
# bounds _EMAIL_RE worst-case to well under 0.1s.
#
# DELIBERATELY tighter than _secret_patterns.SECRET_SCAN_MAX_CHARS (256 KiB):
# that cap governs secret_counts over whole documents (PR bodies <= 64 KiB)
# scanned by the fast prefix-anchored token bank, which is ~linear and never
# runs _EMAIL_RE — so it can afford a far larger ceiling.
_FIELD_SCAN_MAX_CHARS = 8_192  # 8 KiB


def scan_leaks(
    field: str, value: str, violations: list[str], *, multiline: bool = False
) -> None:
    """Full leak scan for a prose / host string value.

    Appends a violation per detected leak class. NEVER echoes `value`.
    `multiline=False` rejects newline/CR + control chars (inline guard);
    `multiline=True` allows \\n \\r \\t but still rejects other control chars
    and markdown heading lines (#8 injection parity).
    """
    if not isinstance(value, str):  # defensive; callers usually type-check first
        violations.append(f"{field}: expected str for leak scan, got {type(value).__name__}")
        return

    if len(value) > _FIELD_SCAN_MAX_CHARS:
        # RC-01 (#236) fail-CLOSED: do not run the regex bank (notably the
        # quadratic _EMAIL_RE) over an oversized value, and never certify it clean.
        violations.append(
            f"{field}: value too large to leak-scan safely "
            f"({len(value)} chars > {_FIELD_SCAN_MAX_CHARS}); failing closed"
        )
        return

    _check_tokens(field, value, violations)

    if _ABS_POSIX_PATH_RE.search(value):
        violations.append(f"{field}: contains absolute POSIX path (use project-relative)")
    if _WIN_DRIVE_RE.search(value):
        violations.append(f"{field}: contains Windows drive path")
    if _UNC_RE.search(value):
        violations.append(f"{field}: contains UNC path")
    if _EMAIL_RE.search(value):
        violations.append(f"{field}: contains email address (PII)")
    if _JWT_RE.search(value):
        violations.append(f"{field}: contains JWT-shape token")
    if _PEM_RE.search(value):
        violations.append(f"{field}: contains PEM private key marker")
    if _URL_RE.search(value):
        violations.append(f"{field}: contains URL")
    if _LONG_BASE64_RE.search(value):
        violations.append(f"{field}: contains long base64 sequence (possible token)")
    if _IPV4_RE.search(value):
        violations.append(f"{field}: contains IPv4 address")
    if _IPV6_RE.search(value):
        violations.append(f"{field}: contains IPv6-shape address")
    if _AUTH_HEADER_RE.search(value):
        violations.append(f"{field}: contains auth/cookie/session header key-value")
    if _LABELED_PHONE_RE.search(value):
        violations.append(f"{field}: contains labeled phone/tel/mobile field (PII)")
    if _HTML_COMMENT_RE.search(value):
        violations.append(f"{field}: contains HTML comment delimiter (<!-- or -->)")

    # control chars
    if multiline:
        for ch in value:
            if ord(ch) < 32 and ch not in ("\t", "\n", "\r"):
                violations.append(f"{field}: contains disallowed control character")
                break
        # markdown heading injection parity (#8): reject any line starting with '#'
        for line in value.splitlines():
            if line.lstrip().startswith("#"):
                violations.append(
                    f"{field}: contains markdown heading line (starts with '#'); injection guard"
                )
                break
    else:
        # single-line invariant: reject ALL line breaks. NEL/LS/PS are line breaks
        # that str.splitlines() / markdown renderers honour but are >= 0x20, so the
        # <32 control check below misses them — fold them in here (L3 RC-02/RB-03/
        # RB-04: inline single-line guard bypass). C-level `in` per separator.
        if any(sep in value for sep in ("\n", "\r") + _UNICODE_LINESEP):
            violations.append(f"{field}: must be single-line (no newline/CR/NEL/LS/PS; markdown injection guard)")
        # \n/\r are already reported by the single-line check above; exclude them
        # here (as the multiline branch does) so a newline is not double-reported
        # (audit a91d790c gemini f2).
        if any(ord(c) < 32 and c not in ("\t", "\n", "\r") for c in value):
            violations.append(f"{field}: contains control characters")

    # Lone surrogates U+D800-DFFF — universal (BOTH modes): a JSON "\\udXXX" escape
    # yields a Python str that passes every regex AND the <32 checks, but the first
    # downstream UTF-8 emit (json.dump(ensure_ascii=False).encode()) raises
    # UnicodeEncodeError. Reject so is_safe=True can never certify un-encodable data
    # (L3 RB-01/RB-02, same class as the PR-D skill-schema surrogate fix).
    #
    # NEL/LS/PS are deliberately NOT rejected in multiline mode: there they are
    # legitimate line breaks like \\n, and markdown-heading injection via ANY
    # separator is already caught by the heading scan above (str.splitlines()
    # honours NEL/LS/PS), so a separate multiline reject adds no security over the
    # \\n allowance and only over-rejects valid prose (audit 65c96e08 gemini f1).
    if _SURROGATE_RE.search(value):
        violations.append(f"{field}: contains lone surrogate code point (not UTF-8 encodable)")


def scan_identifier(field: str, value: str, violations: list[str]) -> None:
    """Leak scan for an IDENTIFIER / user-controlled KEY (slug / name / step-id).

    Identifiers already pass a shape regex that constrains the charset, so the
    main residual risk is a token-shape value that happens to fit the shape
    (#3: 'sk-...'/'npm_...' all-lowercase fits SLUG_RE). Run the token + control
    check; skip prose-only patterns (email/url/path) that the shape regex already
    excludes by charset.

    DELIBERATE non-check (round-3 audit ebd87bef #f5): the long-base64 heuristic
    (_LONG_BASE64_RE, applied in scan_leaks) is NOT applied here. Identifiers such
    as a 40+ char session_id, UUID, or content hash are legitimately long opaque
    strings; flagging them would be a high false-positive rate. Identifier token
    detection relies on prefix patterns (_secret_patterns + belt), which cover the
    realistic leak shapes (prefixed provider tokens).
    """
    if not isinstance(value, str):
        violations.append(f"{field}: expected str identifier, got {type(value).__name__}")
        return
    if len(value) > _FIELD_SCAN_MAX_CHARS:
        # RC-01 (#236) fail-CLOSED: bound the quadratic _EMAIL_RE / belt cost on a
        # pathological oversized identifier, and never certify it clean.
        violations.append(
            f"{field}: identifier too large to leak-scan safely "
            f"({len(value)} chars > {_FIELD_SCAN_MAX_CHARS}); failing closed"
        )
        return
    _check_tokens(field, value, violations)
    if any(ord(c) < 32 for c in value):
        violations.append(f"{field}: contains control character")


def check_host_value(field: str, value: str, violations: list[str]) -> None:
    """Hostname-only validation (#1): reject userinfo (@), port (:), scheme,
    path, query, whitespace — then run full leak scan on the host string.
    """
    if not isinstance(value, str):
        violations.append(f"{field}: host must be str, got {type(value).__name__}")
        return
    bad_chars = ("://", "/", "?", "#", " ", "@", ":")
    for bad in bad_chars:
        if bad in value:
            violations.append(f"{field}: host must be hostname only, contains {bad!r}")
            break
    scan_leaks(field, value, violations, multiline=False)


def self_test() -> int:
    """Offline self-test — every leak class + the audit 4f0c48c0 regressions."""
    def leaks(v, **kw):
        out: list[str] = []
        scan_leaks("f", v, out, **kw)
        return out

    # token (secret_counts + belt)
    assert leaks("gh" + "p_" + "a" * 36), "github token must be caught"
    assert leaks("prefix-" + "sk-ant-" + "api03-" + "a" * 60), "mid-string token (#15)"
    # word-boundary belt: a prefix buried inside a plain word must NOT fire
    # (gemini #1 FP — 'sk-' inside 'task-runner' / 'disk-usage'). The boundary
    # anchor restores parity with the word-boundary-aware primary detector.
    assert not leaks("this task-runner uses disk-usage tools"), "sk- inside word must NOT fire"
    assert not leaks("uses disk-usage stats; no real secrets"), "disk-usage must NOT fire"
    # but a real token after a boundary char (quote / hyphen / space) still fires
    assert leaks('key="sk-' + "ant-api03-" + "a" * 60 + '"'), "token after quote still caught"
    # path boundary fix (#4)
    assert leaks('file="/etc/shadow" x'), "abs path after quote (#4)"
    assert not leaks("relative/path/x"), "relative path must NOT trigger abs-path"
    # PII
    assert leaks("contact a@b.com"), "email"
    assert leaks("see https://x.io/y"), "url"
    # IPv6 tightening (#19): timestamp must NOT match, real IPv6 must
    assert not any("IPv6" in v for v in leaks("event at 12:34:56 today")), "timestamp not IPv6 (#19)"
    assert any("IPv6" in v for v in leaks("addr fe80::1 here")), "real IPv6 caught"
    assert any("IPv6" in v for v in leaks("2001:db8:0:1:1:1:1:1")), "full IPv6 caught"
    # markdown heading parity (#8) — only in multiline
    assert any("heading" in v for v in leaks("ok\n# Fake Section", multiline=True)), "md heading (#8)"
    assert not leaks("normal multiline\nsecond line ok", multiline=True), "benign multiline ok"
    # inline rejects newline
    assert any("single-line" in v for v in leaks("a\nb", multiline=False)), "inline newline"
    # NEVER echo value
    secret = "gh" + "p_" + "S3CR3T" + "a" * 30
    for v in leaks(secret):
        assert "S3CR3T" not in v, f"value leaked in violation: {v}"

    # identifier
    idv: list[str] = []
    scan_identifier("slug", "npm_" + "a" * 34, idv)
    assert idv, "token-shape identifier must be caught (#3)"
    idv2: list[str] = []
    scan_identifier("slug", "json.parse-error", idv2)
    assert not idv2, f"benign slug must pass: {idv2}"

    # host
    hv: list[str] = []
    check_host_value("host", "admin:tok@github.com", hv)
    assert hv, "host userinfo must be caught (#1)"
    hv2: list[str] = []
    check_host_value("host", "github.com:443", hv2)
    assert hv2, "host port must be caught (#1)"
    hv3: list[str] = []
    check_host_value("host", "github.com", hv3)
    assert not hv3, f"clean hostname must pass: {hv3}"

    # L3 RB-01/RB-02: lone surrogate (U+D800-DFFF) must be rejected in BOTH modes
    # (passes every regex + the <32 check, but crashes the first UTF-8 emit).
    assert any("surrogate" in v for v in leaks("intent \ud800 x")), "lone surrogate inline"
    assert any("surrogate" in v for v in leaks("ok\nbody \udfff", multiline=True)), "lone surrogate multiline"
    # L3 RC-02/RB-03/RB-04: NEL/LS/PS are line breaks. INLINE (single-line) mode
    # rejects them; MULTILINE mode allows them as line breaks (like \n) — heading
    # injection via any separator is still caught by the heading scan
    # (str.splitlines() honours NEL/LS/PS). (audit 65c96e08 gemini f1)
    for sep in ("\x85", "\u2028", "\u2029"):
        assert any("single-line" in v for v in leaks("a" + sep + "b")), f"inline sep U+{ord(sep):04X} rejected"
        assert any("heading" in v for v in leaks("a" + sep + "# fake", multiline=True)), f"ml heading via U+{ord(sep):04X} caught"
        assert not leaks("a" + sep + "normal line", multiline=True), f"ml non-heading sep U+{ord(sep):04X} allowed"
    # benign real-newline multiline still passes (no over-rejection regression)
    assert not leaks("line one\nline two ok", multiline=True), "benign newline multiline must pass"

    print("OK: _redaction_common self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
