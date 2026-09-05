"""L1 redaction-engine bypass regression suite (audits 4f0c48c0 / dfd025c8 / ebd87bef).

Executable, durable lock for the headline false-negative bypasses + NEVER-echo
violations found across the three pre-release audit rounds of the redaction
engine. This is the CANONICAL cross-module lock — it is NOT exhaustive: each
guard module additionally ships its own tests/test_*_redaction.py with the full
per-module case matrix.

Each test cites the audit + finding id it locks down.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _bugfix_redaction as bug  # noqa: E402
import _incident_redaction as inc  # noqa: E402
import _metrics_redaction as met  # noqa: E402
import _orchestration_redaction as orch  # noqa: E402
import _redaction_common as rc  # noqa: E402
import _simulation_redaction as sim  # noqa: E402
import _surface_redaction as surf  # noqa: E402
import _wip_redaction as wip  # noqa: E402
from aqg_dangerous_guard import evaluate  # noqa: E402

MARK = "ZZSECRETMARKERZZ"  # canary string that must never appear in a violation


# ===== valid fixtures =====

def _bugfix() -> dict:
    return {"schema_version": 1, "slug": "ok-fix", "date": "2026-05-03", "actor": "claude",
            "title": "t", "affected_area": "x", "severity": "low", "backward_compatible": "yes",
            "symptom": "s", "root_cause": "r", "fix": "f", "verification": "v",
            "regression_coverage": "rc", "boundaries": "b"}


def _incident() -> dict:
    return {"schema_version": 1, "slug": "ok-inc", "date": "2026-05-03", "actor": "claude",
            "title": "t", "severity": "P1", "detection_source": "monitoring",
            "impact_scope": "internal", "summary": "s", "root_cause": "r", "resolution": "res",
            "followups": ["a"], "boundaries": "b"}


def _orch() -> dict:
    return {"schema_version": 1, "name": "x", "description": "d",
            "steps": [{"id": "aa", "actor": "claude", "intent": "i", "inputs": [],
                       "timeout_seconds": 1, "retry_max": 0, "on_fail": "abort"}],
            "boundaries": "b"}


def _sim() -> dict:
    return {"schema_version": 1, "name": "x", "description": "d", "kind": "api",
            "duration_seconds": 1, "boundaries": "b"}


def _wip() -> dict:
    return {"schema_version": 1, "session_id": "abc123", "saved_at_iso": "2026-05-03T12:34:56+00:00",
            "trigger": "manual", "cwd_sha256_first8": "deadbeef"}


# ===== Deep audit 4f0c48c0 — original bypasses must stay BLOCKED =====

class TestThreeAuditBypasses:
    def test_surface_host_userinfo(self):  # #1
        r = surf.check_fingerprint({"a": {"host": "admin:tok@github.com"}})
        assert not r.is_safe

    def test_surface_host_port(self):  # #1
        assert not surf.check_fingerprint({"a": {"host": "github.com:443"}}).is_safe

    def test_surface_host_clean_still_ok(self):  # #1 regression: no over-block
        assert surf.check_fingerprint({"a": {"host": "github.com"}}).is_safe

    def test_surface_category_token(self):  # #2
        assert not surf.check_fingerprint({"npm_" + "a" * 34: {"exists": True}}).is_safe

    def test_bugfix_slug_token(self):  # #3
        b = _bugfix(); b["slug"] = "npm_" + "a" * 34
        assert not bug.check_bugfix_record(b).is_safe

    def test_bugfix_abspath_after_quote(self):  # #4
        b = _bugfix(); b["fix"] = 'edited file="/etc/shadow" x'
        assert not bug.check_bugfix_record(b).is_safe

    def test_bugfix_relpath_not_blocked(self):  # #4 regression: no over-block
        b = _bugfix(); b["fix"] = "edited scripts/foo.py and tests/bar.py"
        assert bug.check_bugfix_record(b).is_safe

    def test_bugfix_enum_list_no_crash(self):  # #7
        b = _bugfix(); b["severity"] = ["low"]
        r = bug.check_bugfix_record(b)  # must return Result, not raise
        assert not r.is_safe

    def test_orch_desc_abspath(self):  # #5
        o = _orch(); o["description"] = "deployed to /Users/example/.ssh/id_rsa here"
        assert not orch.check_orchestration_manifest(o).is_safe

    def test_orch_desc_email(self):  # #5
        o = _orch(); o["description"] = "ping admin@example.com"
        assert not orch.check_orchestration_manifest(o).is_safe

    def test_sim_param_key_sensitive_substring(self):  # #14
        s = _sim(); s["parameters"] = {"db_password": "x"}
        assert not sim.check_simulation_manifest(s).is_safe

    def test_wip_session_mid_token(self):  # #15
        w = _wip(); w["session_id"] = "sess-ghp_" + "a" * 16
        assert not wip.check_wip_snapshot(w).is_safe

    def test_incident_markdown_heading(self):  # #8
        i = _incident(); i["root_cause"] = "ok\n# Fake Section"
        assert not inc.check_incident_record(i).is_safe

    @pytest.mark.parametrize("cmd", [
        "echo $GITHUB_TOKEN", "echo $OPENAI_API_KEY", "printf $AWS_SECRET_ACCESS_KEY",  # #9
        "rm -rf -- /", "rm -rf -- /home",  # #11
        "gh api --method=DELETE /repos/o/r/branches/main/protection",  # #12
    ])
    def test_guard_blocks(self, cmd):
        assert evaluate("Bash", {"command": cmd})["decision"] == "block"

    @pytest.mark.parametrize("cmd", ["echo $TOKEN", "rm -rf /", "rm -rf /etc"])
    def test_guard_control_still_blocks(self, cmd):  # controls: precise edges, not over-broad
        assert evaluate("Bash", {"command": cmd})["decision"] == "block"

    def test_guard_benign_not_blocked(self):  # regression: no over-block
        assert evaluate("Bash", {"command": "rm -rf ./build"})["decision"] != "block"

    def test_guard_ghu_github_pat(self):  # #10
        assert evaluate("Bash", {"command": "echo ghu_" + "a" * 40})["decision"] == "block"
        assert evaluate("Bash", {"command": "echo github_pat_" + "a" * 40})["decision"] == "block"

    def test_guard_secret_in_description(self):  # #13
        assert evaluate("Bash", {"command": "ls", "description": "key AKIA" + "A" * 16})["decision"] == "block"


# ===== Deep audit dfd025c8 — fix-verification residuals must stay FIXED =====

class TestFixResiduals:
    def test_incident_slug_token(self):  # dfd f2 / N2
        i = _incident(); i["slug"] = "npm_" + "a" * 34
        assert not inc.check_incident_record(i).is_safe

    def test_orch_step_id_token(self):  # dfd f2 / N3
        o = _orch(); o["steps"][0]["id"] = "npm_" + "a" * 34
        assert not orch.check_orchestration_manifest(o).is_safe

    def test_sim_param_key_token(self):  # dfd f2 / N4
        s = _sim(); s["parameters"] = {"npm_" + "a" * 30: "x"}
        assert not sim.check_simulation_manifest(s).is_safe


class TestNeverEchoInvariant:
    """No violation message may echo a user-controlled value (dfd f1 / N1, N5)."""

    def test_incident_enum_no_echo(self):
        for fld in ("severity", "detection_source", "impact_scope", "marker"):
            i = _incident(); i[fld] = MARK
            r = inc.check_incident_record(i)
            assert not r.is_safe
            assert all(MARK not in v for v in r.violations), f"{fld} echoed: {r.violations}"

    def test_wip_enum_no_echo(self):
        w = _wip(); w["trigger"] = MARK
        r = wip.check_wip_snapshot(w)
        assert all(MARK not in v for v in r.violations), r.violations
        w = _wip(); w["cwd_status"] = {"git_branch_status": MARK}
        r = wip.check_wip_snapshot(w)
        assert all(MARK not in v for v in r.violations), r.violations

    def test_surface_category_key_no_echo(self):
        cat = "npm_" + "a" * 34
        r = surf.check_fingerprint({cat: {"exists": True}})
        assert not r.is_safe
        assert all(cat not in v for v in r.violations), r.violations

    def test_redaction_common_never_echoes_secret(self):
        secret = "gh" + "p_" + "S3CR3TBODY" + "a" * 30
        out: list[str] = []
        rc.scan_leaks("f", secret, out)
        assert out and all("S3CR3TBODY" not in v for v in out)


# ===== Fallback audit ebd87bef — completeness residuals (round-3 fixes) =====

class TestRound3Residuals:
    def test_bugfix_actor_token(self):  # ebd f1/f2 — actor identifier scan
        b = _bugfix(); b["actor"] = "npm_" + "a" * 34
        assert not bug.check_bugfix_record(b).is_safe

    def test_incident_actor_token(self):  # ebd f1/f2
        i = _incident(); i["actor"] = "npm_" + "a" * 34
        assert not inc.check_incident_record(i).is_safe

    def test_schema_version_list_no_crash(self):  # ebd f3 — type guard, no TypeError
        b = _bugfix(); b["schema_version"] = [1]
        assert not bug.check_bugfix_record(b).is_safe  # returns Result, doesn't raise
        w = _wip(); w["schema_version"] = [1]
        assert not wip.check_wip_snapshot(w).is_safe

    def test_guard_str_tool_input_no_crash(self):  # ebd gemini f1 — AttributeError guard
        r = evaluate("Bash", "rm -rf /")  # str tool_input must not raise
        assert r["decision"] == "block"

    def test_sim_sensitive_key_no_echo(self):  # ebd f3/f4 — never echo param key
        s = _sim(); s["parameters"] = {"api_key_extra": "x"}
        r = sim.check_simulation_manifest(s)
        assert not r.is_safe
        assert all("api_key_extra" not in v for v in r.violations), r.violations


# ===== Round 4 4a89cc29 — TypeError-crash class exhaustion + guard fail-open =====

class TestRound4Residuals:
    def test_guard_custom_rule_bad_severity_no_crash(self):  # 4a89cc29 f1/f3
        import json
        import os
        import tempfile
        from aqg_dangerous_guard import load_rules
        rules = json.dumps([{"rule_id": "X", "category": "c", "severity": [],
                             "description": "d", "command_regex": "x", "tool_name": "Bash"}])
        fd, p = tempfile.mkstemp(suffix=".json"); os.write(fd, rules.encode()); os.close(fd)
        os.environ["AQG_DANGEROUS_GUARD_RULES"] = p
        try:
            load_rules()  # must not raise TypeError on unhashable severity
        finally:
            os.environ.pop("AQG_DANGEROUS_GUARD_RULES", None); os.unlink(p)

    def test_guard_list_tool_input_blocks(self):  # 4a89cc29 gemini f1 — no fail-open
        r = evaluate("Bash", ["r" + "m", "-" + "rf", "/"])
        assert r["decision"] == "block"  # list args joined + scanned, not dropped to {}

    def test_bugfix_marker_list_no_crash(self):  # 4a89cc29 — enum type-guard exhaustion
        b = _bugfix(); b["marker"] = ["x"]
        assert not bug.check_bugfix_record(b).is_safe  # Result, not TypeError

    def test_metrics_enum_list_no_crash(self):  # 4a89cc29 — metrics enum exhaustion
        m = {"schema_version": 1, "ts": "2026-05-03T12:34:56+00:00", "tool": "audit", "result": "pass"}
        for fld in ("tool", "result", "actor", "marker"):
            bad = dict(m); bad[fld] = ["x"]
            assert not met.check_metrics_record(bad).is_safe  # Result, not TypeError


# ===== Round 5 b276170c — dangerous_guard input robustness + rm coverage =====

class TestRound5Residuals:
    def test_guard_command_field_list_blocks(self):  # b276170c gemini f1 — no crash/fail-open
        assert evaluate("Bash", {"command": ["r" + "m", "-r" + "f", "/"]})["decision"] == "block"

    def test_guard_command_nested_list_blocks(self):  # b276170c gpt f2 — recursive flatten
        assert evaluate("Bash", {"command": ["r" + "m", ["-r" + "f"], ["/"]]})["decision"] == "block"

    def test_guard_description_list_secret_blocks(self):  # b276170c gemini f2 — non-str field scanned
        sec = "gh" + "p_" + "a" * 36
        assert evaluate("Bash", {"command": "echo done", "description": [sec]})["decision"] == "block"

    def test_guard_rm_extra_flags_block(self):  # b276170c gpt f1 — -rfv / --no-preserve-root
        assert evaluate("Bash", {"command": "r" + "m -rfv /home"})["decision"] == "block"
        assert evaluate("Bash", {"command": "r" + "m -r" + "f --no-preserve-root /"})["decision"] == "block"

    def test_guard_rm_benign_not_overblocked(self):  # FP guard — extended rm regex must not over-block
        assert evaluate("Bash", {"command": "r" + "m -r" + "f ./build"})["decision"] != "block"

    def test_guard_loadrules_warning_no_echo(self):  # b276170c gpt f3 — never echo custom-rule token
        import contextlib
        import io
        import json
        import os
        import tempfile
        from aqg_dangerous_guard import load_rules
        tok = "gh" + "p_" + "c" * 36
        rules = json.dumps([{"rule_id": tok, "category": "c", "severity": "bad",
                             "description": "d", "command_regex": "x", "tool_name": "Bash"}])
        fd, p = tempfile.mkstemp(suffix=".json"); os.write(fd, rules.encode()); os.close(fd)
        os.environ["AQG_DANGEROUS_GUARD_RULES"] = p
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                load_rules()
        finally:
            os.environ.pop("AQG_DANGEROUS_GUARD_RULES", None); os.unlink(p)
        assert tok not in err.getvalue()
