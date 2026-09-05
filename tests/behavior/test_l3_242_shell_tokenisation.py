r"""L3-3 #242 — shell-tokenisation closes 3 deferred dangerous_guard findings.

`aqg_dangerous_guard.py` is a pattern matcher invoked by an opt-in EXAMPLE
PreToolUse hook — no runtime enforcement authority, so these are defense-in-depth
(MED), not CRIT bypasses.

Closes the 3 findings the Deep audit `f9a48407` deferred because regex-without-tokenisation
cannot cover shell-equivalent spellings:
  - RM-escaping: `rm -rf` reaching root via `\/`, `''/`, `$'/'` — shlex normalise
    (+ ANSI-C `$'...'` prefix); the `\s+.../` anchor missed them.
  - GD-03: `echo "x: $TOKEN"` (double-quote → shell expands → leak) vs
    `echo '$TOKEN'` (single-quote literal → shell does NOT expand → safe) —
    quote-aware "expandable" view strips single-quoted spans before SL-1/SL-2.
  - BPB-3b: `refs/heads/` refspec form (`+refs/heads/main`, `+HEAD:refs/heads/main`).

Empirical basis (/tmp/shlex_probe.py): shlex.split normalises `\/`/`''/`/`"/"` to `/`
but (a) does NOT decode ANSI-C `$'/'` (→ `$/`), (b) LOSES the single-vs-double quote
distinction (`'$T'` and `"$T"` split identically), (c) raises ValueError on an unclosed
quote. Hence: shlex normalise for RM, a self-written quote-state scanner for GD-03,
plain regex for BPB-3b, and a raw fallback when shlex fails.

RED-proven: `git stash push -- scripts/aqg_dangerous_guard.py` → the closing cases
FAIL → pop → GREEN. Baseline-PASS rows are FP guards / regression locks / the
documented ANSI-C-encoded residual.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import aqg_dangerous_guard as g  # noqa: E402

S = "/"  # concatenated so dangerous literals never appear verbatim in source
BS = chr(92)  # backslash
SQ = chr(39)  # single quote


def _dec(cmd: str) -> str:
    return g.evaluate("Bash", {"command": cmd})["decision"]


# --------------------------------------------------------------------------- #
# Finding 1 — RM reaching / via shell-equivalent spellings (shlex normalise)
# --------------------------------------------------------------------------- #
class TestRmShellEquivalentRoot:
    def test_backslash_escaped_root_blocked(self) -> None:
        # rm -rf \/  — bash: `\/` == `/`; the `\s+.../` anchor missed it
        assert _dec("rm -rf " + BS + S) == "block"

    def test_empty_single_quote_root_blocked(self) -> None:
        # rm -rf ''/  — bash: `''/` == `/`
        assert _dec("rm -rf " + SQ + SQ + S) == "block"

    def test_empty_quote_system_dir_blocked(self) -> None:
        # rm -rf ''/etc → RM-2 after normalise
        assert _dec("rm -rf " + SQ + SQ + S + "etc") == "block"

    def test_normalise_keeps_benign_specific_path_allowed(self) -> None:
        assert _dec("rm -rf " + S + "tmp" + S + "foo") == "allow"


# --------------------------------------------------------------------------- #
# Finding 2 (GD-03) — quote-aware secret expansion
# --------------------------------------------------------------------------- #
class TestEchoSecretQuoteAware:
    def test_double_quoted_secret_prefix_blocked(self) -> None:
        # $TOKEN expands inside double quotes → leak (was a documented FN)
        assert _dec('echo "x: ' + "$TOKEN" + '"') == "block"

    def test_double_quoted_api_key_blocked(self) -> None:
        assert _dec('echo "key=' + "$API_KEY" + '"') == "block"

    def test_printf_double_quoted_secret_still_blocked(self) -> None:
        assert _dec('printf "v=%s" "' + "$AWS_SECRET_ACCESS_KEY" + '"') == "block"

    def test_bare_secret_still_blocked(self) -> None:
        assert _dec("echo " + "$TOKEN") == "block"

    def test_single_quoted_secret_var_allowed(self) -> None:
        # shell does NOT expand inside single quotes → not a leak (no FP)
        assert _dec("echo '" + "$TOKEN" + "'") == "allow"

    def test_single_quoted_prefix_literal_allowed(self) -> None:
        assert _dec("echo 'x: " + "$TOKEN" + "'") == "allow"


# --------------------------------------------------------------------------- #
# FP guards for the quote-aware view
# --------------------------------------------------------------------------- #
class TestQuoteAwareNoFalsePositives:
    def test_double_quoted_non_secret_var_allowed(self) -> None:
        assert _dec('echo "' + "$HOME" + '"') == "allow"
        assert _dec('echo "' + "$PATH" + '/bin"') == "allow"

    def test_positional_param_not_secret(self) -> None:
        assert _dec('echo "cost is ' + "$5" + '"') == "allow"

    def test_literal_secret_in_single_quotes_STILL_blocked(self) -> None:
        # SL-5/6/7 are LITERAL-token rules: `echo 'AKIA...'` still prints the key
        # to stdout (single quotes don't stop echo emitting the literal). These must
        # NOT move to the expandable view, else single-quoted literals would leak.
        assert _dec("echo '" + "AKIA" + "A" * 16 + "'") == "block"


# --------------------------------------------------------------------------- #
# Finding 3 (BPB-3b) — refs/heads/ refspec form
# --------------------------------------------------------------------------- #
class TestBpb3bRefsHeads:
    def test_refspec_refs_heads_main_blocked(self) -> None:
        assert _dec("git push origin +refs/heads/main") == "block"

    def test_refspec_head_colon_refs_heads_main_blocked(self) -> None:
        assert _dec("git push origin +HEAD:refs/heads/main") == "block"

    def test_existing_plain_refspec_still_blocked(self) -> None:
        # regression lock: the original `+HEAD:main` form must keep blocking
        assert _dec("git push origin +HEAD:main") == "block"

    def test_refspec_refs_heads_feature_allowed(self) -> None:
        # target is a non-protected branch → must NOT over-block
        assert _dec("git push origin +refs/heads/feature") == "allow"


# --------------------------------------------------------------------------- #
# Fail-safe — an unclosed quote must not crash or fail-open
# --------------------------------------------------------------------------- #
class TestUnbalancedQuoteFailSafe:
    def test_unbalanced_double_quote_secret_still_detected(self) -> None:
        # unclosed double quote → shlex raises; the quote-state scanner still keeps
        # the double-quoted body so the leak is caught
        assert _dec('echo "leak ' + "$TOKEN") == "block"

    def test_unbalanced_single_quote_returns_legal_decision(self) -> None:
        # must not crash / fail-open into garbage; raw fallback keeps a legal decision
        d = _dec("rm -rf '" + S)  # unclosed single quote
        assert d in {"allow", "warn", "block"}


# --------------------------------------------------------------------------- #
# Documented residual — ANSI-C *encoded* forms stay out of scope (MED ceiling)
# --------------------------------------------------------------------------- #
class TestAnsiCQuotingResidual:
    r"""ANSI-C quoting `$'...'` (ALL forms) is a documented residual: shlex (POSIX)
    does not decode it and a 1-char regex lookaround cannot track quote state (audit
    d38064a9), so it is left rather than shipping a buggy hack. Asserting current
    (allow) behaviour so a future state-machine fix that closes it trips these as a
    reminder — NOT a safety claim. `\/` and `''/` (the other RM-escaping forms) are
    NOT residual — shlex handles them (see TestRmShellEquivalentRoot)."""

    def test_basic_ansi_c_root_is_residual(self) -> None:
        # rm -rf $'/' — bash decodes to `/`; shlex POSIX leaves `$/`
        assert _dec("rm -rf $" + SQ + S + SQ) == "allow"

    def test_hex_encoded_root_is_residual(self) -> None:
        # rm -rf $'\x2f' == `/` in bash; hex/octal decode out of scope
        assert _dec("rm -rf $" + SQ + BS + "x2f" + SQ) == "allow"

    def test_empty_quote_concat_ansi_c_is_residual(self) -> None:
        # rm -rf ''$'/' — empty single-quote concatenated with ANSI-C $'/' == `/`
        # (audit d38064a9); a 1-char lookbehind couldn't tell this from a literal $/
        assert _dec("rm -rf " + SQ + SQ + "$" + SQ + S + SQ) == "allow"


# --------------------------------------------------------------------------- #
# Deep audit f6cebf0d regression locks — findings that reshaped the design
# (a hand-written quote-state scanner → a single-quote-excluding regex)
# --------------------------------------------------------------------------- #
class TestAuditF6cebf0dRegression:
    def test_nested_bash_c_secret_blocked(self) -> None:
        # gpt-5.5 f1: the inner shell of `bash -c '...'` DOES expand $TOKEN → leak.
        # A scanner dropping single-quoted spans would miss this; the regex keeps it.
        assert _dec("bash -c 'echo " + "$TOKEN" + "'") == "block"

    def test_nested_eval_secret_blocked(self) -> None:
        assert _dec("eval 'echo " + "$API_KEY" + "'") == "block"

    def test_apostrophe_in_comment_does_not_fail_open(self) -> None:
        # gemini f1 (was BLOCKING): an apostrophe in a comment must not silently
        # terminate the scan and blind SL-1 to a later $SECRET.
        assert _dec("echo " + "$TOKEN" + " # don't leak this") == "block"

    def test_bpb_dotted_source_refspec_blocked(self) -> None:
        # gpt f2 + gemini f2 (convergent): a dotted source branch must not break the match.
        assert _dec("git push origin +feature.1:main") == "block"
        assert _dec("git push origin +feature.v1:refs/heads/main") == "block"

    def test_bpb_protected_source_nonprotected_target_allowed(self) -> None:
        # gpt f2: only the refspec TARGET matters — a protected name on the SOURCE
        # side pushing to a non-protected target must NOT over-block.
        assert _dec("git push origin +refs/heads/main:refs/heads/feature") == "allow"
        assert _dec("git push origin +main:feature") == "allow"

    def test_ansi_c_literal_dollar_path_not_blocked(self) -> None:
        # gpt f3: `rm -rf '$'/` is the literal path `$/`, not `/`. Round 2 removed the
        # ANSI-C strip entirely, so shlex yields `$/` and this no longer mis-fires.
        assert _dec("rm -rf '" + "$" + "'" + S) == "allow"


# --------------------------------------------------------------------------- #
# Deep audit d38064a9 (round-2 revision verification) regression locks
# --------------------------------------------------------------------------- #
class TestAuditD38064a9Regression:
    def test_sl2_singlequoted_format_then_doublequoted_secret_blocked(self) -> None:
        # gpt/gemini f1: SL-2 keeps {_SEG}* so a single-quoted format arg before a
        # double-quoted secret still blocks (narrowing it would have regressed this).
        assert _dec("printf '%s" + BS + "n' " + '"$SECRET"') == "block"

    def test_sl1_benign_singlequote_prefix_is_documented_residual(self) -> None:
        # gpt/gemini f1: SL-1's single-quote-excluding prefix cannot see past a benign
        # single-quoted arg to a later double-quoted secret. pre-fix `\s+\$` ALSO missed
        # this (NOT a regression); a full fix needs a quote-state tokenizer. Asserting
        # current (allow) so a future fix trips this reminder — NOT a safety claim.
        assert _dec("echo 'label:' " + '"$TOKEN"') == "allow"

    def test_unclosed_quote_does_not_fail_open_rm_escape(self) -> None:
        # gemini f2: an appended unbalanced quote must not fail-open the shlex normalise
        # back to raw (which misses the \/ spelling). The quote-stripped retry catches it.
        assert _dec("rm -rf " + BS + S + " " + SQ) == "block"

    def test_bpb_revexpr_source_nonprotected_target_allowed(self) -> None:
        # gpt f3: a rev-expr source (main~1) pushing to a NON-protected target must not
        # over-block.
        assert _dec("git push origin +main~1:feature") == "allow"

    def test_bpb_revexpr_source_protected_target_is_residual(self) -> None:
        # gpt f3: a rev-expr SOURCE (HEAD~1) pushed to a protected target is a residual
        # — the source char class can't consume `~`; needs full refspec parsing.
        # Asserting current (allow) as a reminder — NOT a safety claim.
        assert _dec("git push origin +HEAD~1:main") == "allow"
