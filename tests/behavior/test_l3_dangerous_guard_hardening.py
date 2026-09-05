r"""L3 GD-04/01/02 regression for aqg_dangerous_guard.py (opt-in example hook).

`aqg_dangerous_guard.py` is a pattern matcher invoked by an EXAMPLE PreToolUse hook —
it does NOT hold runtime enforcement authority, so bypasses here are defense-in-depth
(MED/LOW), not CRIT.

Fixes shipped:
- GD-04 (HIGH): `payload.get(...)` raised AttributeError on a non-dict JSON payload
  ([]/"s"/123/null) → uncaught traceback exit 1, violating the exit-70 (EXIT_USAGE)
  bad-input contract. Now guarded → EXIT_USAGE.
- GD-01 (MED): no rule case-folded the verb, so `RM -rf /` / `GIT push -f main` /
  `GH pr merge --admin` bypassed every rule (macOS's case-insensitive FS resolves the
  uppercase verb to the real binary). Fixed with scoped `(?i:verb)` — ONLY the
  executable verb is case-folded.
- GD-02 (MED): RM-1/2/3 anchored the target right after end-of-options; `rm -rf "/"` /
  `'/'` / `//` / `/.` / `/?` bypassed. Added an optional quote + `/`,`.`,`?` to the
  root forms.

Adjudication of audit f9a48407 (gpt-5.5 + gemini — both surfaced the over-broad
IGNORECASE false positive that the FIRST attempt introduced):
- The first attempt case-folded the WHOLE pattern, which false-positived `echo $token`
  and `git push -F main` (env-var names + flags are case-sensitive). Narrowed to
  scoped `(?i:verb)`.
- GD-06 (kubectl `-n Prod`) REJECTED: k8s namespaces are RFC1123 lowercase-only, so
  `-n Prod` never targets the prod namespace — PW-2 matching only lowercase is correct.
- GD-02 backslash / ANSI-C-quoting forms (`\/`, `$'/'`) and GD-03 (relaxing SL-1, which
  false-positived single-quoted literal `$TOKEN`) were DEFERRED — now CLOSED by #242
  (quote-aware tokenisation); see test_l3_242_shell_tokenisation.py.

Bypass repros FAIL against pre-fix source (stash-proven):
`git stash push -- scripts/aqg_dangerous_guard.py` -> RED.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import aqg_dangerous_guard as g  # noqa: E402

S = "/"  # concatenated so dangerous literals never appear verbatim in source
GUARD = str(REPO / "scripts" / "aqg_dangerous_guard.py")


def _dec(cmd: str) -> str:
    return g.evaluate("Bash", {"command": cmd})["decision"]


# --------------------------------------------------------------------------- #
# GD-04 — non-dict payload must be a clean usage error, not a traceback
# --------------------------------------------------------------------------- #
class TestGd04NonDictPayload:
    def _run(self, payload_text: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, GUARD], input=payload_text, text=True, capture_output=True
        )

    def test_non_dict_payloads_exit_usage_not_traceback(self) -> None:
        for p in ("[]", '"hi"', "123", "null"):
            r = self._run(p)
            assert r.returncode == g.EXIT_USAGE, (p, r.returncode, r.stderr)
            assert "Traceback" not in r.stderr, (p, r.stderr)

    def test_valid_dict_payload_still_works(self) -> None:
        r = self._run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
        assert r.returncode == g.EXIT_ALLOW, r.stderr


# --------------------------------------------------------------------------- #
# GD-01 — scoped (?i:verb): the executable verb is case-folded, nothing else
# --------------------------------------------------------------------------- #
class TestGd01ScopedVerbCaseFold:
    def test_uppercase_rm_root_blocked(self) -> None:
        assert _dec("RM -rf " + S) == "block"

    def test_uppercase_git_force_push_blocked(self) -> None:
        assert _dec("GIT push -f main") == "block"

    def test_mixed_case_gh_admin_merge_blocked(self) -> None:
        assert _dec("GH pr merge --admin 123") == "block"

    def test_flag_case_is_NOT_folded(self) -> None:
        # `-F` is not git's force flag; case-folding the verb must not case-fold flags,
        # else this benign command would be wrongly blocked (gpt-5.5 f2).
        assert _dec("git push -F main") == "allow"


# --------------------------------------------------------------------------- #
# GD-02 — quoted / double-slash / dot / glob root bypass
# --------------------------------------------------------------------------- #
class TestGd02RmRootBypass:
    def test_double_quoted_root_blocked(self) -> None:
        assert _dec('rm -rf "' + S + '"') == "block"

    def test_single_quoted_root_blocked(self) -> None:
        assert _dec("rm -rf '" + S + "'") == "block"

    def test_double_slash_root_blocked(self) -> None:
        assert _dec("rm -rf " + S + S) == "block"

    def test_slash_dot_root_blocked(self) -> None:
        # `rm -rf /.` recursively deletes root contents (gemini f2 / gpt f1)
        assert _dec("rm -rf " + S + ".") == "block"

    def test_slash_glob_root_blocked(self) -> None:
        assert _dec("rm -rf " + S + "?") == "block"


# --------------------------------------------------------------------------- #
# FP guard — the over-block the first (global IGNORECASE) attempt introduced
# --------------------------------------------------------------------------- #
class TestNoFalsePositives:
    def test_lowercase_env_var_names_not_blocked(self) -> None:
        # env-var names are case-sensitive; scoped verb folding must not fold them
        # (the global-IGNORECASE attempt wrongly blocked these — gemini f1).
        assert _dec("echo $token") == "allow"
        assert _dec("echo $my_secret_value") == "allow"
        assert _dec("printf $api_key") == "allow"

    def test_single_quoted_literal_dollar_not_blocked(self) -> None:
        # shell does not expand `$TOKEN` inside single quotes, so it is not a leak
        # (the GD-03 {_SEG}* relaxation would have wrongly blocked this — gpt f3).
        assert _dec("echo '$TOKEN'") == "allow"

    def test_lowercase_secret_literals_not_blocked(self) -> None:
        # SL-5/6/7 literal token values stay case-sensitive: a lowercase look-alike
        # is not a real token and must not block a benign command.
        assert _dec("echo akia" + "a" * 16) == "allow"
        assert _dec("echo GHP_" + "a" * 25) == "allow"

    def test_real_aws_key_format_still_blocked(self) -> None:
        assert _dec("echo AKIA" + "A" * 16) == "block"

    def test_specific_paths_and_benign_not_blocked(self) -> None:
        assert _dec("rm -rf " + S + "tmp" + S + "foo") == "allow"
        assert _dec('rm -rf "' + S + 'tmp"') == "allow"
        assert _dec("echo hello world") == "allow"
        assert _dec("git push origin feature") == "allow"


# --------------------------------------------------------------------------- #
# Documented limitations — adjudicated NOT to fix (would reject / need shell parse)
# --------------------------------------------------------------------------- #
class TestDocumentedLimitations:
    def test_gd06_capitalized_namespace_is_not_a_bug(self) -> None:
        # k8s namespaces are RFC1123 lowercase-only; `-n Prod` never targets prod,
        # so PW-2 matching only lowercase prod is correct (GD-06 rejected).
        assert _dec("kubectl delete -n Prod pod") == "allow"
        assert _dec("kubectl delete -n prod pod") == "warn"

    def test_gd03_echo_prefix_secret_now_fixed_by_242(self) -> None:
        # GD-03 was deferred here as a documented limitation; #242 added a quote-aware
        # expandable view distinguishing `echo "x: $TOKEN"` (double quote → expands →
        # leak → block) from `echo '$TOKEN'` (single quote → literal → allow, asserted
        # in TestNoFalsePositives). Full coverage: test_l3_242_shell_tokenisation.py.
        assert _dec('echo "x: $TOKEN"') == "block"
