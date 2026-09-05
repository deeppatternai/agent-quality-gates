"""#236 / RC-01 — input-length cap across the secret/redaction regex bank.

The bank (`_secret_patterns.secret_counts` ~18 patterns via `findall`; plus the
~13 `.search()` leak patterns + 24-prefix belt in `_redaction_common`) is
O(N^2)-ish on very large inputs — `re.search` start-position × greedy local-part
scan, not a classic catastrophic-backtrack ReDoS (the round-1 re-anchoring was a
no-op for this). RC-01 adds an input-length cap so worst-case cost is bounded
regardless of the individual regex shapes.

Design — fail-CLOSED, NOT truncate-and-scan:
- an over-cap input is NOT scanned (cost stays O(1)) and is reported as a leak;
- `secret_counts` returns ``{SCAN_CAP_EXCEEDED_KEY: 1}`` (truthy → every
  count-gating caller blocks), `scan_leaks`/`scan_identifier` append a
  fail-closed violation and early-return.
Truncate-and-scan would be fail-OPEN — a secret past the cap would be silently
certified clean, violating this engine's core invariant (`_redaction_common`
docstring: "fail-CLOSED: never silently skip").

Two distinct caps (the field path runs the quadratic `_EMAIL_RE`, the document
path does not):
- `_secret_patterns.SECRET_SCAN_MAX_CHARS` = 256 KiB — `secret_counts` over whole
  documents (PR bodies <= 64 KiB), scanned by the fast prefix-anchored token
  bank (~linear), so a generous ceiling is fine.
- `_redaction_common._FIELD_SCAN_MAX_CHARS` = 8 KiB — per-field `scan_leaks` /
  `scan_identifier`, which run the O(N^2) `_EMAIL_RE`; fields are schema-capped
  tiny (<= 800 chars), so 8 KiB is a 10x backstop that bounds the quadratic cost.
Both caps sit far above any legitimate input, so real scans never change.

These tests call `secret_counts`/`scan_leaks` directly on in-memory strings (no
temp file), so assertions cannot be polluted by pytest tmp_path test-name
injection. Secret-shaped samples are built from fragments so the repo's own
secret scan stays clean.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _secret_patterns as sp  # noqa: E402
import _redaction_common as rc  # noqa: E402


def test_secret_counts_caps_oversized_input_fail_closed(monkeypatch):
    """RED: an over-cap input returns the fail-closed sentinel; bank not run."""
    monkeypatch.setattr(sp, "SECRET_SCAN_MAX_CHARS", 64)
    out = sp.secret_counts("a" * 65)
    assert out == {sp.SCAN_CAP_EXCEEDED_KEY: 1}
    # NEVER echo the input: the only key is the constant sentinel.
    assert "a" * 65 not in "".join(out)


def test_secret_counts_oversized_is_truthy_for_gating(monkeypatch):
    """RED: the fail-closed result must be truthy so count-gating callers block."""
    monkeypatch.setattr(sp, "SECRET_SCAN_MAX_CHARS", 64)
    assert sp.secret_counts("x" * 100)  # `if leaked:` (run_quality_gates / post_pr_comment)
    assert sum(sp.secret_counts("x" * 100).values()) > 0  # ledger_hook_writer path


def test_secret_counts_under_cap_scans_normally(monkeypatch):
    """No-regression: under-cap input is scanned; a real secret is still detected."""
    monkeypatch.setattr(sp, "SECRET_SCAN_MAX_CHARS", 1024)
    token = "gh" + "p_" + "a" * 36
    out = sp.secret_counts(token, patterns=sp.BUILTIN_PATTERNS)
    assert out.get("github_token") == 1
    assert sp.SCAN_CAP_EXCEEDED_KEY not in out
    # benign under-cap input -> empty (no sentinel, no false positive)
    assert sp.secret_counts("just some normal words here", patterns=sp.BUILTIN_PATTERNS) == {}


def test_real_default_cap_rejects_oversized_input():
    """At the real default cap (no monkeypatch), a >256 KiB input is capped."""
    big = "a" * (sp.SECRET_SCAN_MAX_CHARS + 1)
    assert sp.secret_counts(big) == {sp.SCAN_CAP_EXCEEDED_KEY: 1}


def test_scan_leaks_caps_oversized_value_fail_closed(monkeypatch):
    """RED: scan_leaks early-returns a fail-closed violation; the regex bank is skipped."""
    monkeypatch.setattr(rc, "_FIELD_SCAN_MAX_CHARS", 64)
    violations: list[str] = []
    # value contains an email (normally reported) AND is over the field cap
    rc.scan_leaks("f", "a@b.com " + "x" * 100, violations)
    assert any("too large" in v and "failing closed" in v for v in violations)
    # early-return: the email is NOT separately reported (bank skipped)
    assert not any("email" in v for v in violations)


def test_scan_identifier_caps_oversized_value_fail_closed(monkeypatch):
    """RED: scan_identifier early-returns a fail-closed violation when over-cap."""
    monkeypatch.setattr(rc, "_FIELD_SCAN_MAX_CHARS", 64)
    violations: list[str] = []
    rc.scan_identifier("slug", "y" * 100, violations)
    assert any("too large" in v and "failing closed" in v for v in violations)


def test_scan_leaks_under_cap_still_detects(monkeypatch):
    """No-regression: an under-cap value still runs the full leak scan."""
    monkeypatch.setattr(rc, "_FIELD_SCAN_MAX_CHARS", 1024)
    violations: list[str] = []
    rc.scan_leaks("f", "contact a@b.com here", violations)
    assert any("email" in v for v in violations)
    assert not any("too large" in v for v in violations)


def test_real_field_cap_rejects_oversized_field():
    """At the real 8 KiB field cap (no monkeypatch), an oversized pathological
    `_EMAIL_RE` value is rejected fail-closed before the quadratic scan runs.
    """
    # 'a.' * N is the O(N^2) _EMAIL_RE shape; over the field cap -> not scanned.
    value = "a." * rc._FIELD_SCAN_MAX_CHARS  # 2x the cap, no '@'
    violations: list[str] = []
    rc.scan_leaks("f", value, violations)
    assert any("too large" in v and "failing closed" in v for v in violations)
    assert not any("email" in v for v in violations)


def test_field_cap_tighter_than_document_cap():
    """The per-field cap is deliberately tighter than the document (secret_counts)
    cap: the field path runs the quadratic _EMAIL_RE, the document path does not.
    """
    assert rc._FIELD_SCAN_MAX_CHARS < sp.SECRET_SCAN_MAX_CHARS
