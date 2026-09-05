"""L3 RA-01/RA-02 regression suite for _metrics_redaction convergence.

Before: _metrics_redaction had its own stale 13-prefix `_TOKEN_PREFIXES` list and a
local `_check_string_safe` — it never called the shared redaction codepath, so real
provider tokens whose prefix was not in that list (npm_/hf_/whsec_/sk_live_/stripe/…)
passed `check_metrics_record` with is_safe=True and reached the local metrics ledger /
`cmd_list` stdout (RA-01). The local check also lacked the 18-pattern secret detector
the other guards get via `_redaction_common` (RA-02).

After: the two free-form string fields (tool_version, event_id) delegate to the shared
`_redaction_common.scan_identifier` (secret_counts 18-pattern + word-boundary belt +
control-char) — same codepath the orchestration/simulation guards use for slug/id fields.

Each token-repro FAILS against pre-fix source (stash-proven):
`git stash push -- scripts/_metrics_redaction.py` -> RED.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _metrics_redaction as mr  # noqa: E402


def _valid() -> dict:
    return {
        "schema_version": 1,
        "ts": "2026-05-03T12:34:56+00:00",
        "tool": "audit",
        "result": "pass",
        "tool_version": "0.2.6",
        "actor": "claude",
        "marker": "auto-recorded-by-aqg-metrics",
    }


def test_valid_metrics_record_passes() -> None:
    assert mr.check_metrics_record(_valid()).is_safe


# RA-01: tokens the OLD local 13-prefix list missed, fitting tool_version (<=32 chars)
@pytest.mark.parametrize("tok", ["whsec_" + "a" * 24, "sk_live_" + "a" * 20])
def test_tool_version_missed_token_now_caught(tok: str) -> None:
    rec = _valid()
    rec["tool_version"] = tok
    assert not mr.check_metrics_record(rec).is_safe


# RA-01: tokens fitting event_id (<=128 chars, lowercase charset)
@pytest.mark.parametrize("tok", ["npm_" + "a" * 34, "hf_" + "a" * 33])
def test_event_id_missed_token_now_caught(tok: str) -> None:
    rec = _valid()
    rec["event_id"] = tok
    assert not mr.check_metrics_record(rec).is_safe


def test_existing_ghp_midstring_still_caught() -> None:
    # regression: the prior gemini-#3 substring case must still be rejected.
    rec = _valid()
    rec["tool_version"] = "0.2.6-ghp_xxx"
    assert not mr.check_metrics_record(rec).is_safe


# Audit 1bb805a7 f1 adjudication: the shared scan_identifier uses a word-boundary belt
# (deliberate anti-FP design — avoids 'sk-' inside 'task-runner'). The realistic embedded
# leak — a token after a '-', '_' or '.' delimiter, which is how identifier values are
# structured — IS caught; only a token glued directly to a letter/digit with NO delimiter
# is missed (contrived for these AQG-set local-only fields). These pin BOTH: (a) the
# realistic delimited-embedded token is caught (closes the "prove mid-string" concern for
# the broader token set), and (b) a legitimate 'sk-'-containing identifier is NOT
# over-rejected (the reason raw-substring was dropped).
@pytest.mark.parametrize("val", ["run-whsec_" + "a" * 24, "build.sk_live_" + "a" * 20, "x_npm_" + "a" * 30])
def test_delimited_embedded_token_caught(val: str) -> None:
    rec = _valid()
    rec["event_id"] = val
    assert not mr.check_metrics_record(rec).is_safe


def test_legit_sk_substring_identifier_not_false_positive() -> None:
    # 'task-runner-build-7' contains 'sk-' (ta>sk-<runner) but is a legitimate value;
    # the word-boundary belt correctly does NOT flag it (raw-substring would FP).
    rec = _valid()
    rec["event_id"] = "task-runner-build-7"
    assert mr.check_metrics_record(rec).is_safe


def test_violation_never_echoes_token_body() -> None:
    # convergence keeps the no-echo invariant: the random token body must not appear
    # in any violation message (only field name + pattern category / prefix).
    body = "ZZSECRETBODYZZ"
    rec = _valid()
    rec["tool_version"] = ("sk_live_" + body + "a" * 6)[:32]
    result = mr.check_metrics_record(rec)
    assert not result.is_safe
    for v in result.violations:
        assert body not in v, f"raw token body leaked in violation: {v}"
