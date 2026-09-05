"""Tests for scripts/aqg_dangerous_guard.py — Q3 #9 dangerous command guard.

Per docs/policies/dangerous-command-guard.md:
- Critical (severity=critical) → exit 2 (block)
- Major (severity=major) → exit 1 (warn)
- Allow (no match or minor) → exit 0
- Boundary: AQG owns the rule engine, NOT the active enforcer
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import aqg_dangerous_guard as dg  # noqa: E402


SCRIPT_PATH = REPO / "scripts" / "aqg_dangerous_guard.py"


def _run(payload: dict, env: dict | None = None) -> tuple[int, str, str]:
    """Run the script with given payload via stdin; return (rc, stdout, stderr)."""
    proc = subprocess.run(
        ["python3", str(SCRIPT_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _evaluate(command: str, tool_name: str = "Bash") -> dict:
    """In-process call to evaluate(); returns full result dict."""
    return dg.evaluate(tool_name, {"command": command})


# ===========================================
# Branch protection bypass — CRITICAL
# ===========================================


class TestBranchProtectionBypass:
    def test_gh_pr_merge_admin_blocked(self):
        result = _evaluate("gh pr merge 123 --admin")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "BPB-1" for m in result["matches"])

    def test_gh_pr_merge_admin_with_squash_blocked(self):
        result = _evaluate("gh pr merge 42 --squash --admin")
        assert result["decision"] == "block"

    def test_force_push_to_main_blocked(self):
        result = _evaluate("git push --force origin main")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "BPB-3" for m in result["matches"])

    def test_force_push_to_master_blocked(self):
        result = _evaluate("git push --force origin master")
        assert result["decision"] == "block"

    def test_force_with_lease_to_main_blocked(self):
        # PR-Q3#9 audit gpt #5: BPB-3 consolidated -f / --force / --force-with-lease
        result = _evaluate("git push --force-with-lease origin main")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "BPB-3" for m in result["matches"])

    # PR-Q3#9 audit gpt #5: short -f form
    def test_short_force_to_main_blocked(self):
        result = _evaluate("git push -f origin main")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "BPB-3" for m in result["matches"])

    # PR-Q3#9 audit gpt #5: +refspec form
    def test_plus_refspec_to_main_blocked(self):
        result = _evaluate("git push origin +HEAD:main")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "BPB-3b" for m in result["matches"])

    def test_force_push_to_feature_branch_allowed(self):
        result = _evaluate("git push --force origin feat/my-branch")
        assert result["decision"] == "allow"

    def test_normal_push_to_main_allowed(self):
        result = _evaluate("git push origin main")
        assert result["decision"] == "allow"

    def test_disable_branch_protection_blocked(self):
        result = _evaluate(
            'gh api -X DELETE /repos/owner/repo/branches/main/protection'
        )
        assert result["decision"] == "block"

    # audit 4f0c48c0 #12: --method=DELETE (glued via `=`) must be caught (BPB-6)
    def test_disable_branch_protection_method_equals_blocked(self):
        result = _evaluate(
            "gh api --method=DELETE /repos/o/r/branches/main/protection"
        )
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "BPB-6" for m in result["matches"])

    # audit 4f0c48c0 #12: -XDELETE (glued, no separator) and -X=DELETE (BPB-5)
    def test_disable_branch_protection_x_glued_blocked(self):
        for cmd in (
            "gh api -XDELETE /repos/o/r/branches/main/protection",
            "gh api -X=DELETE /repos/o/r/branches/master/protection",
        ):
            result = _evaluate(cmd)
            assert result["decision"] == "block", f"failed for {cmd!r}"
            assert any(m["rule_id"] == "BPB-5" for m in result["matches"])


# ===========================================
# rm -rf risk
# ===========================================


class TestRmRfRisk:
    def test_rm_rf_root_blocked(self):
        result = _evaluate("rm -rf /")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "RM-1" for m in result["matches"])

    def test_rm_rf_top_level_dir_blocked(self):
        for d in ["/etc", "/usr", "/var", "/home", "/bin"]:
            result = _evaluate(f"rm -rf {d}")
            assert result["decision"] == "block", f"failed for {d}"

    def test_rm_rf_home_blocked(self):
        result = _evaluate("rm -rf ~")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "RM-3" for m in result["matches"])

    def test_rm_rf_home_subdir_blocked(self):
        result = _evaluate("rm -rf ~/my-data")
        assert result["decision"] == "block"

    # audit 4f0c48c0 #11: end-of-options form `rm -rf -- /` (POSIX `--` marks
    # end of options, so this is equivalent to `rm -rf /`).
    def test_rm_rf_end_of_options_root_blocked(self):
        result = _evaluate("rm -rf -- /")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "RM-1" for m in result["matches"])

    # audit 4f0c48c0 #11: control — plain `rm -rf /` (no `--`) still blocked
    def test_rm_rf_root_no_dashdash_still_blocked(self):
        result = _evaluate("rm -rf /")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "RM-1" for m in result["matches"])

    # audit 4f0c48c0 #11: end-of-options against a top-level system dir
    def test_rm_rf_end_of_options_system_dir_blocked(self):
        result = _evaluate("rm -rf -- /home")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "RM-2" for m in result["matches"])

    def test_rm_rf_var_warned(self):
        result = _evaluate("rm -rf $WORK_DIR/cache")
        assert result["decision"] == "warn"
        assert any(m["rule_id"] == "RM-4" for m in result["matches"])

    def test_rm_rf_parent_traversal_warned(self):
        result = _evaluate("rm -rf ../foo")
        assert result["decision"] == "warn"

    def test_rm_rf_node_modules_allowed(self):
        result = _evaluate("rm -rf node_modules")
        assert result["decision"] == "allow"

    def test_rm_rf_relative_path_allowed(self):
        result = _evaluate("rm -rf build/output")
        assert result["decision"] == "allow"

    # audit 4f0c48c0 #11: regression guard — the end-of-options fix must not
    # introduce a false-positive on a benign relative dotted path.
    def test_rm_rf_dot_build_allowed(self):
        result = _evaluate("rm -rf ./build")
        assert result["decision"] == "allow"


# ===========================================
# Secret leak — CRITICAL
# ===========================================


class TestSecretLeak:
    def test_echo_token_blocked(self):
        result = _evaluate("echo $TOKEN")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "SL-1" for m in result["matches"])

    def test_echo_api_key_blocked(self):
        result = _evaluate("echo $API_KEY")
        assert result["decision"] == "block"

    def test_echo_braced_secret_blocked(self):
        result = _evaluate("echo ${SECRET}")
        assert result["decision"] == "block"

    def test_cat_dotenv_blocked(self):
        result = _evaluate("cat .env")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "SL-3" for m in result["matches"])

    def test_cat_dotenv_local_blocked(self):
        result = _evaluate("cat .env.local")
        assert result["decision"] == "block"

    def test_cat_credentials_blocked(self):
        result = _evaluate("cat ~/.aws/credentials")
        assert result["decision"] == "block"

    def test_aws_key_literal_blocked(self):
        # split via concat to avoid GitHub Push Protection
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        result = _evaluate(f"curl -H 'X-Auth: {secret}' http://example.com")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "SL-5" for m in result["matches"])

    def test_github_token_literal_blocked(self):
        secret = "gh" + "p_" + "A" * 36 + "1"
        result = _evaluate(f"curl -H 'Auth: Bearer {secret}'")
        assert result["decision"] == "block"

    # audit 4f0c48c0 #9: env-name PREFIX (e.g. $GITHUB_TOKEN) must be caught,
    # not only an exact leading TOKEN/SECRET/...
    def test_echo_prefixed_token_blocked(self):
        result = _evaluate("echo $GITHUB_TOKEN")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "SL-1" for m in result["matches"])

    # audit 4f0c48c0 #9: control — bare $TOKEN (no prefix) still blocked
    def test_echo_bare_token_still_blocked(self):
        result = _evaluate("echo $TOKEN")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "SL-1" for m in result["matches"])

    # audit 4f0c48c0 #9: env-name SUFFIX (e.g. $OPENAI_API_KEY, $AWS_SECRET_ACCESS_KEY)
    def test_echo_suffixed_secret_env_blocked(self):
        for cmd in ("echo $OPENAI_API_KEY", "printf $AWS_SECRET_ACCESS_KEY"):
            result = _evaluate(cmd)
            assert result["decision"] == "block", f"failed for {cmd!r}"

    # audit 4f0c48c0 #10: SL-6 must cover ghu_/ghr_/ghs_/gho_ and github_pat_
    # (previously only ghp_/gho_/ghs_ at length >=36 matched).
    def test_github_user_to_server_and_pat_blocked(self):
        for literal in ("gh" + "u_" + "A" * 36, "github" + "_pat_" + "B" * 36):
            result = _evaluate(f"curl -H 'Authorization: token {literal}'")
            assert result["decision"] == "block", f"failed for {literal[:12]!r}"
            assert any(m["rule_id"] == "SL-6" for m in result["matches"])

    # audit 4f0c48c0 #10: SL-7 must cover restricted rk_ keys + whsec_ webhook
    def test_stripe_restricted_and_webhook_blocked(self):
        for literal in ("rk" + "_live_" + "C" * 24, "whsec" + "_" + "D" * 24):
            result = _evaluate(f"export STRIPE={literal}")
            assert result["decision"] == "block", f"failed for {literal[:8]!r}"
            assert any(m["rule_id"] == "SL-7" for m in result["matches"])

    # audit 4f0c48c0 #13: a secret stashed in the Bash `description` field
    # (not the command) must still be detected by secret-leak rules.
    def test_secret_in_description_blocked(self):
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        result = dg.evaluate(
            "Bash", {"command": "ls", "description": f"pull data using {secret}"}
        )
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "SL-5" for m in result["matches"])

    def test_echo_normal_var_allowed(self):
        result = _evaluate("echo $HOME")
        assert result["decision"] == "allow"

    def test_cat_random_file_allowed(self):
        result = _evaluate("cat README.md")
        assert result["decision"] == "allow"


# ===========================================
# Production write — MAJOR
# ===========================================


class TestProductionWrite:
    def test_vercel_deploy_warned(self):
        result = _evaluate("vercel deploy --prod")
        assert result["decision"] == "warn"

    def test_fly_deploy_warned(self):
        result = _evaluate("fly deploy")
        assert result["decision"] == "warn"

    def test_kubectl_apply_prod_warned(self):
        result = _evaluate("kubectl apply -f deploy.yaml -n production")
        assert result["decision"] == "warn"
        assert any(m["rule_id"] == "PW-2" for m in result["matches"])

    def test_kubectl_apply_dev_allowed(self):
        result = _evaluate("kubectl apply -f deploy.yaml -n dev")
        assert result["decision"] == "allow"

    def test_terraform_apply_warned(self):
        result = _evaluate("terraform apply -auto-approve")
        assert result["decision"] == "warn"

    def test_pulumi_up_warned(self):
        result = _evaluate("pulumi up --yes")
        assert result["decision"] == "warn"

    def test_aws_ecs_update_service_warned(self):
        result = _evaluate("aws ecs update-service --cluster prod --service api")
        assert result["decision"] == "warn"

    def test_gh_release_create_default_warned(self):
        # No --prerelease flag = production release
        result = _evaluate("gh release create v1.0.0")
        assert result["decision"] == "warn"


# ===========================================
# Verification bypass — MAJOR (CRITICAL on protected branch push)
# ===========================================


class TestVerificationBypass:
    def test_commit_no_verify_warned(self):
        result = _evaluate('git commit --no-verify -m "skip"')
        assert result["decision"] == "warn"
        assert any(m["rule_id"] == "VB-1" for m in result["matches"])

    def test_push_no_verify_to_feature_warned(self):
        result = _evaluate("git push --no-verify origin feat/x")
        assert result["decision"] == "warn"

    def test_push_no_verify_to_main_blocked(self):
        # VB-4 upgrades to critical
        result = _evaluate("git push --no-verify origin main")
        assert result["decision"] == "block"
        assert any(m["rule_id"] == "VB-4" for m in result["matches"])

    def test_no_gpg_sign_warned(self):
        result = _evaluate('git commit --no-gpg-sign -m "test"')
        assert result["decision"] == "warn"

    def test_normal_commit_allowed(self):
        result = _evaluate('git commit -m "feat: add x"')
        assert result["decision"] == "allow"


# ===========================================
# Severity ordering + decision logic
# ===========================================


class TestSeverityOrdering:
    def test_multiple_matches_pick_highest(self):
        # `git push --force origin main` matches BPB-3 (critical); also might
        # match other things — should still resolve to "block"
        result = _evaluate("git push --force origin main --no-verify")
        assert result["decision"] == "block"
        # Should have multiple matches
        assert len(result["matches"]) >= 1

    def test_only_warns_no_critical_decision_warn(self):
        result = _evaluate("vercel deploy && terraform apply")
        # Multiple major matches but no critical → warn
        assert result["decision"] == "warn"

    def test_no_match_decision_allow(self):
        result = _evaluate("ls -la")
        assert result["decision"] == "allow"
        assert result["matches"] == []


# ===========================================
# Subprocess CLI integration
# ===========================================


class TestCLIIntegration:
    def test_stdin_input_critical_exit_2(self):
        rc, out, err = _run({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
        assert rc == 2
        result = json.loads(out)
        assert result["decision"] == "block"
        assert "RM-1" in err
        assert "CRITICAL" in err

    def test_stdin_input_major_exit_1(self):
        rc, out, err = _run({"tool_name": "Bash", "tool_input": {"command": "vercel deploy"}})
        assert rc == 1
        result = json.loads(out)
        assert result["decision"] == "warn"

    def test_stdin_input_allow_exit_0(self):
        rc, out, err = _run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert rc == 0
        result = json.loads(out)
        assert result["decision"] == "allow"
        assert err == ""  # silent on no-match

    def test_invalid_json_exits_70(self):
        proc = subprocess.run(
            ["python3", str(SCRIPT_PATH)],
            input="not json",
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 70

    def test_list_rules(self):
        proc = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--list-rules"],
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 0
        assert "BPB-1" in proc.stdout
        assert "RM-1" in proc.stdout
        assert "SL-1" in proc.stdout
        assert "PW-1" in proc.stdout
        assert "VB-1" in proc.stdout

    def test_non_bash_tool_serialized(self):
        # Edit tool with file_path containing AWS key triggers SL-5
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        rc, out, _ = _run(
            {"tool_name": "Edit", "tool_input": {"file_path": f"/tmp/{secret}.txt"}}
        )
        # Should detect via JSON-serialized fallback
        assert rc == 2

    def test_decision_to_exit_mapping(self):
        assert dg.decision_to_exit("allow") == 0
        assert dg.decision_to_exit("warn") == 1
        assert dg.decision_to_exit("block") == 2
        assert dg.decision_to_exit("unknown") == 0  # default safe


# ===========================================
# Output schema
# ===========================================


class TestOutputSchema:
    def test_decision_dict_has_required_fields(self):
        result = _evaluate("rm -rf /")
        assert "policy_version" in result
        assert "tool_name" in result
        assert "decision" in result
        assert "matches" in result
        assert isinstance(result["matches"], list)
        for m in result["matches"]:
            assert "rule_id" in m
            assert "category" in m
            assert "severity" in m
            assert "description" in m
            assert "evidence" in m

    def test_policy_version_constant(self):
        result = _evaluate("anything")
        assert result["policy_version"] == dg.POLICY_VERSION


# ===========================================
# Custom rules override
# ===========================================


class TestCustomRulesOverride:
    def test_custom_rule_loaded(self, tmp_path, monkeypatch):
        custom = tmp_path / "rules.json"
        custom.write_text(
            json.dumps(
                [
                    {
                        "rule_id": "CUST-1",
                        "category": "custom",
                        "severity": "critical",
                        "description": "Custom — block any 'forbidden_keyword'",
                        "command_regex": r"forbidden_keyword",
                        "tool_name": "Bash",
                    }
                ]
            )
        )
        monkeypatch.setenv("AQG_DANGEROUS_GUARD_RULES", str(custom))
        rules = dg.load_rules()
        assert any(r.rule_id == "CUST-1" for r in rules)

    def test_malformed_custom_rules_warned_not_crash(self, tmp_path, monkeypatch, capsys):
        custom = tmp_path / "rules.json"
        custom.write_text(json.dumps([{"rule_id": "BAD"}]))  # missing required fields
        monkeypatch.setenv("AQG_DANGEROUS_GUARD_RULES", str(custom))
        rules = dg.load_rules()
        captured = capsys.readouterr()
        assert "WARN" in captured.err
        # Default rules still present
        assert any(r.rule_id == "RM-1" for r in rules)

    def test_invalid_json_custom_rules_warned(self, tmp_path, monkeypatch, capsys):
        custom = tmp_path / "rules.json"
        custom.write_text("not json")
        monkeypatch.setenv("AQG_DANGEROUS_GUARD_RULES", str(custom))
        rules = dg.load_rules()
        captured = capsys.readouterr()
        assert "WARN" in captured.err
        assert any(r.rule_id == "RM-1" for r in rules)
