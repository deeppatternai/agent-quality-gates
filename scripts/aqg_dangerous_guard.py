#!/usr/bin/env python3
"""AQG Dangerous Command Guard — pattern-matching rule engine (Q3 #9 ship).

Per framework v2.1 §3 + §7 #9: AQG owns (1) the policy and match rules,
(2) this rule engine. Actual interception = PreToolUse-aware client owns.

Boundary:
- This script is a deterministic pattern matcher invoked by an example hook.
- It does NOT auto-monitor tool calls.
- It does NOT hold runtime enforcement authority.
- Inspired by mattpocock/git-guardrails-claude-code (2026-05-04 ecosystem scan).

Input (stdin, JSON; matches Claude Code PreToolUse hook format):
    {
      "tool_name": "Bash",
      "tool_input": {
        "command": "rm -rf /tmp/foo",
        "description": "..."
      }
    }

Output (stdout, JSON):
    {
      "policy_version": "v0",
      "tool_name": "Bash",
      "decision": "allow|warn|block",
      "matches": [{"rule_id", "category", "severity", "description", "evidence"}]
    }

Exit codes:
  0  allow / minor or no match
  1  warn  (major severity)
  2  block (critical severity)
  70 usage error (bad input format)

Override default rules via env var: AQG_DANGEROUS_GUARD_RULES=/path/to/rules.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


POLICY_VERSION = "v0.2"  # bumped after audit 4f0c48c0 redaction-bypass fixes

EXIT_ALLOW = 0
EXIT_WARN = 1
EXIT_BLOCK = 2
EXIT_USAGE = 70

# Severity ordering (higher = more severe)
_SEVERITY_RANK = {"minor": 0, "major": 1, "critical": 2}
_VALID_SEVERITIES = frozenset(_SEVERITY_RANK.keys())

# audit gpt #6 + gemini #2 fix: command-segment boundary (don't cross |, ;, &, newline)
_SEG = r"[^|;&\n]"

# audit gpt #4 + gemini #1 fix: rm with recursive+force variants
# Covers: -rf, -fr, -Rf, -fR, -rF, -fR, -r -f, -f -r, --recursive --force, etc.
_RM_FORCE = (
    r"(?:"
    # round-5 b276170c gpt f1: a single flag cluster containing BOTH r and f,
    # allowing other letters (e.g. -rfv, -vfr). Order-agnostic via two branches.
    r"-[a-zA-Z]*[rR][a-zA-Z]*[fF][a-zA-Z]*"     # -rf, -rfv, -Rf, -vrf... (r before f)
    r"|-[a-zA-Z]*[fF][a-zA-Z]*[rR][a-zA-Z]*"    # -fr, -frv... (f before r)
    r"|-[rR]\s+-[fF]|-[fF]\s+-[rR]"              # -r -f, -f -r, -R -f, -f -R
    r"|--recursive\s+--force|--force\s+--recursive"  # long form
    r"|--recursive\s+-[fF]|-[fF]\s+--recursive"   # mixed
    r"|--force\s+-[rR]|-[rR]\s+--force"           # mixed
    r")"
)

# audit 4f0c48c0 #11: end-of-options form `rm -rf -- /` / `rm -rf -- /home`
# Optional `-- ` separator between the force flags and the (dangerous) target.
# POSIX `--` marks end of options so `rm -rf -- /` is equivalent to `rm -rf /`.
_END_OPTS = r"(?:(?:--?[\w-]+|--)\s+)*"

# audit 4f0c48c0 #13: Bash fields (besides `command`) whose string values are
# scanned by secret-leak rules so a secret stashed in e.g. `description`
# cannot bypass detection. Kept explicit (not "all fields") to avoid scanning
# the command twice or pulling in non-string structures.
_BASH_SECRET_SCAN_FIELDS = ("description",)

# #242 GD-03: segment char EXCLUDING the single-quote. SL-1/SL-2 (echo/printf secret
# EXPANSION) use `{_NO_SQUOTE_SEG}*\$` so the match prefix cannot cross a `'…'` span:
# `echo "x: $TOKEN"` (double-quote → shell expands → leak) matches, while
# `echo '$TOKEN'` (single-quote → shell does NOT expand → literal) does not. This is a
# pure-regex alternative to a quote-state scanner — crucially it KEEPS detection of an
# expanded secret inside a nested-shell payload (`bash -c 'echo $TOKEN'`, `eval '…'`),
# which a scanner that drops single-quoted spans would silently miss (audit f6cebf0d).
_NO_SQUOTE_SEG = r"[^'|;&\n]"


def _flatten_to_text(value: object) -> str:
    """Flatten any tool-input value to scannable text — fail-safe, never raises.

    A list/dict is recursively flattened to space-joined scalar tokens so command
    rules still see argv-like text. round-5 b276170c: a non-str `command` field
    ({"command": ["rm","-rf","/"]}) crashed regex.search (fail-open), and a nested
    list produced repr punctuation that dodged the rm rules — recursive flatten
    fixes both.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_flatten_to_text(x) for x in value)
    if isinstance(value, dict):
        return " ".join(_flatten_to_text(v) for v in value.values())
    if value is None:
        return ""
    return str(value)


def _extra_bash_string_fields(tool_input: dict) -> list[str]:
    """Return scannable text of secret-scanned Bash fields other than `command`.

    round-5 b276170c gemini f2: a non-string value is FLATTENED (not skipped) — a
    secret stashed in a list/dict `description` (e.g. {"description": ["ghp_..."]})
    must still be scanned, else it leaks (host frameworks log the raw tool call).
    """
    out: list[str] = []
    for field in _BASH_SECRET_SCAN_FIELDS:
        value = tool_input.get(field)
        if value is None:
            continue
        text = _flatten_to_text(value)
        if text:
            out.append(text)
    return out


def _normalize_command(command: str) -> Optional[str]:
    """Return a shell-tokenised, quote/escape-resolved form of `command`, or None.

    #242 (RM-escaping): rm-rf-risk rules also run on this normalised shlex view so
    shell-equivalent root spellings the raw-string regex misses — `rm -rf \\/`,
    `rm -rf ''/`, `rm -rf "/"` — are caught. ANSI-C quoting (`$'/'`, `$'\\x2f'`) is a
    documented residual: shlex (POSIX) does not decode it and a 1-char regex cannot
    track quote state (audit d38064a9), so it is left rather than shipping a buggy
    lookaround hack. On an unclosed quote shlex raises ValueError; rather than
    fail-OPEN to raw (which misses `\\/`), retry with quote chars stripped so an
    appended unbalanced quote cannot bypass the normalise (audit d38064a9 gemini f2).
    None only if both attempts fail.
    """
    try:
        return " ".join(shlex.split(command))
    except ValueError:
        try:
            return " ".join(shlex.split(command.replace("'", " ").replace('"', " ")))
        except ValueError:
            return None


def _scan_texts_for_rule(
    rule: "Rule",
    command: str,
    normalized: Optional[str],
    secret_scan_text: str,
) -> tuple[str, ...]:
    """Pick the text view(s) a rule matches against (#242).

    - secret-leak → raw command + extra fields. SL-1/SL-2 distinguish single- vs
      double-quoted `$VAR` via the `{_NO_SQUOTE_SEG}` prefix in their OWN regex (a
      pure-regex approach, not a separate view — see audit f6cebf0d).
    - rm-rf-risk → raw AND shlex-normalised (catch shell-equivalent root spellings);
      the normalised view is added only when it differs and shlex succeeded.
    - everything else → raw command.
    """
    if rule.category == "secret-leak":
        return (secret_scan_text,)
    if rule.category == "rm-rf-risk" and normalized is not None and normalized != command:
        return (command, normalized)
    return (command,)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    severity: str  # minor | major | critical
    description: str
    command_regex: re.Pattern
    tool_name: str  # Bash / Edit / Write / etc. or "*" for any

    def matches(self, tool_name: str, command: str) -> Optional[str]:
        """Return matched evidence string if rule fires, else None."""
        if self.tool_name != "*" and self.tool_name != tool_name:
            return None
        m = self.command_regex.search(command)
        if m is None:
            return None
        return m.group(0)


# ===== Default rules (per docs/policies/dangerous-command-guard.md) =====


def _default_rules() -> list[Rule]:
    """Default rule set v0.1 (post audit 3a4fe0eb fixes).

    Per-rule notes reference audit findings (gpt-5.5 # / gemini #).
    Segment-bound `_SEG*` prevents .* cross-statement false positives.
    Lookaheads make multi-flag rules order-agnostic.
    """
    return [
        # Category 1: Branch protection bypass (CRITICAL block)
        # audit gpt #6 + gemini #2: replace .* with segment-bound lookahead
        Rule("BPB-1", "branch-protection-bypass", "critical",
             "gh pr merge --admin bypasses required reviews + checks",
             re.compile(rf"\b(?i:gh)\s+pr\s+merge\b(?={_SEG}*\B--admin\b)"), "Bash"),
        # audit gpt #5: include short -f + refspec form (force-update via +HEAD:main)
        # NB: \B before hyphen flags because space→hyphen is NOT a word boundary
        Rule("BPB-3", "branch-protection-bypass", "critical",
             "git push -f / --force / --force-with-lease to protected branch (any flag order)",
             re.compile(
                 rf"\b(?i:git)\s+push\b(?={_SEG}*\B(?:-f|--force|--force-with-lease)\b)"
                 rf"(?={_SEG}*\b(?:main|master|release/[\w./-]+)\b)"
             ), "Bash"),
        # #242 BPB-3b: cover the `refs/heads/` refspec form + colon-less single-refspec
        # (`+refs/heads/main`), and stay TARGET-aware (audit f6cebf0d f2). Match =
        # optional src `<ref>:` (dotted names allowed) + optional `refs/heads/` + a
        # protected branch that is the refspec TARGET — the trailing `(?![\w./:~^-])`
        # rejects a protected name on the SOURCE side, incl. rev-expr sources like
        # `+main~1:feature` (audit d38064a9 f3). A rev-expr SOURCE pushed to a protected
        # target (`+HEAD~1:main`) is a documented residual (needs full refspec parsing).
        Rule("BPB-3b", "branch-protection-bypass", "critical",
             "git push +refspec force-update (incl. refs/heads/ form) to protected branch",
             re.compile(
                 rf"\b(?i:git)\s+push\b{_SEG}*\+(?:[\w./-]+:)?(?:refs/heads/)?"
                 r"(?:main|master|release/[\w./-]+)(?![\w./:~^-])"
             ), "Bash"),
        # audit 4f0c48c0 #12: -X accepts `-X DELETE`, `-XDELETE`, `-X=DELETE`
        # ([\s=]* — curl/gh allow the value glued or `=`-joined to -X)
        Rule("BPB-5", "branch-protection-bypass", "critical",
             "Disable branch protection via gh api (-X DELETE on /branches/<main|master>/protection)",
             re.compile(
                 rf"\b(?i:gh)\s+api\b(?={_SEG}*-X[\s=]*DELETE\b)(?={_SEG}*/branches/(?:main|master)/protection\b)"
             ), "Bash"),
        # audit 4f0c48c0 #12: --method accepts both `--method DELETE` and `--method=DELETE`
        Rule("BPB-6", "branch-protection-bypass", "critical",
             "Modify branch protection via gh api --method PUT/PATCH/DELETE",
             re.compile(
                 rf"\b(?i:gh)\s+api\b(?={_SEG}*\B--method[\s=]+(?:PUT|PATCH|DELETE)\b)"
                 rf"(?={_SEG}*/branches/[^/]+/protection\b)"
             ), "Bash"),

        # Category 2: rm -rf risk — audit gpt #4 + gemini #1
        # _RM_FORCE covers -rf / -fr / -Rf / --recursive --force / mixed
        Rule("RM-1", "rm-rf-risk", "critical",
             "rm -rf / (or root glob /*) — delete root",
             re.compile(rf"\b(?i:rm)\s+{_RM_FORCE}\s+{_END_OPTS}[\"']?/(?:\*|/|\.|\?|\s|[\"']|$)"), "Bash"),
        Rule("RM-2", "rm-rf-risk", "critical",
             "rm -rf /<top-level-dir> (delete system dir)",
             re.compile(
                 rf"\b(?i:rm)\s+{_RM_FORCE}\s+{_END_OPTS}[\"']?/(?:bin|etc|usr|var|opt|lib|sbin|home|root|sys|proc|dev)\b"
             ), "Bash"),
        Rule("RM-3", "rm-rf-risk", "critical",
             "rm -rf ~ (delete home)",
             re.compile(rf"\b(?i:rm)\s+{_RM_FORCE}\s+{_END_OPTS}[\"']?~(?:\s|/|[\"']|$)"), "Bash"),
        # audit gemini #6: support quoted "$VAR" / '$VAR'
        Rule("RM-4", "rm-rf-risk", "major",
             "rm -rf $VAR (or quoted) — risky if VAR empty/unset",
             re.compile(
                 rf"\b(?i:rm)\s+{_RM_FORCE}\s+{_END_OPTS}[\"']?\$\{{?[A-Z_][A-Z0-9_]*\}}?[\"']?(?:/|\s|$)"
             ), "Bash"),
        Rule("RM-5", "rm-rf-risk", "major",
             "rm -rf with parent traversal",
             re.compile(rf"\b(?i:rm)\s+{_RM_FORCE}\s+{_END_OPTS}\.\./"), "Bash"),

        # Category 3: Secret leak (CRITICAL).
        # tool_name="*" for SL-5/6/7 (literal patterns in any tool input).
        # audit 4f0c48c0 #9: allow env-name prefix/suffix so $GITHUB_TOKEN,
        # $OPENAI_API_KEY, $AWS_SECRET_ACCESS_KEY are caught (previously only
        # an exact leading TOKEN/SECRET/... matched). The `[A-Z0-9_]*` on both
        # sides stays within the uppercase env-name charset so benign vars like
        # $HOME / $PATH / $USER do not match.
        # #242 GD-03: anchor relaxed from `\s+\$` to `{_NO_SQUOTE_SEG}*\$` so a
        # non-first-arg secret (`echo "x: $TOKEN"`) is caught, while the single-quote-
        # excluding prefix keeps `echo '$TOKEN'` (literal, not expanded) from matching.
        # Pure regex (no separate view): also catches `bash -c 'echo $TOKEN'` where the
        # inner shell DOES expand it — a span-dropping scanner would miss that.
        Rule("SL-1", "secret-leak", "critical",
             "echo $*TOKEN/API_KEY/SECRET/PASSWORD* prints secret to stdout/log",
             re.compile(
                 rf"\b(?i:echo|print)\b{_NO_SQUOTE_SEG}*\$\{{?[A-Z0-9_]*"
                 r"(?:TOKEN|API_KEY|SECRET|PASSWORD|PWD_SECRET|CREDENTIAL)[A-Z0-9_]*\b"
             ), "Bash"),
        # audit gpt #6: segment-bound printf; audit 4f0c48c0 #9: env-name prefix/suffix.
        # #242: SL-2 KEEPS the broad `{_SEG}*` prefix (NOT SL-1's single-quote-excluding
        # one). Narrowing it would regress `printf '%s\n' "$SECRET"` (single-quoted
        # format arg, then a double-quoted secret) from block to allow (audit d38064a9).
        # Trade-off: `printf '$SECRET'` (literal) stays a benign FP matching pre-fix
        # behaviour — for printf, missing a real leak (FN) is worse than that FP.
        Rule("SL-2", "secret-leak", "critical",
             "printf with secret env (any *TOKEN/API_KEY/SECRET/PASSWORD* var)",
             re.compile(
                 rf"\b(?i:printf)\b{_SEG}*\$\{{?[A-Z0-9_]*"
                 r"(?:TOKEN|API_KEY|SECRET|PASSWORD|PWD_SECRET|CREDENTIAL)[A-Z0-9_]*\b"
             ), "Bash"),
        Rule("SL-3", "secret-leak", "critical",
             "cat .env or .env.<suffix> (print env file contents)",
             re.compile(r"\b(?i:cat)\s+\.env(?:\.\w+)?(?:\s|$)"), "Bash"),
        Rule("SL-4", "secret-leak", "critical",
             "cat credentials file",
             re.compile(rf"\b(?i:cat)\b{_SEG}*\bcredentials?\b"), "Bash"),
        Rule("SL-5", "secret-leak", "critical",
             "AWS access key literal anywhere in tool input",
             # PR-L3-1b (sister of RC-03): + ASIA/ABIA/ACCA credential prefixes.
             # Lookbehind/lookahead, not \b: \b treats '_' as a word char and would
             # miss '_AKIA…' / 'AKIA…_' (audit 9b6283dd f1).
             re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}(?![A-Za-z0-9])"), "*"),
        # audit 4f0c48c0 #10: align with _secret_patterns.github_token —
        # cover ghu_ (user-to-server) / ghr_ (refresh) / ghs_ (server) /
        # gho_ (oauth) and fine-grained github_pat_ tokens (previously only
        # ghp_/gho_/ghs_ at length >=36 matched, missing ghu_/ghr_/pat).
        Rule("SL-6", "secret-leak", "critical",
             "GitHub token literal anywhere in tool input",
             re.compile(
                 r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
             ), "*"),
        # audit 4f0c48c0 #10: align with _secret_patterns stripe_secret_key +
        # stripe_webhook_secret — cover restricted rk_ keys and whsec_ webhook
        # signing secrets (previously only sk_live_/sk_test_ matched).
        Rule("SL-7", "secret-leak", "critical",
             "Stripe key literal anywhere in tool input (sk/rk live/test, whsec)",
             re.compile(
                 r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b|\bwhsec_[A-Za-z0-9]{20,}\b"
             ), "*"),
        # audit gpt #3 + gemini #7 fix: SL-8 added (was documented but missing)
        Rule("SL-8", "secret-leak", "critical",
             "Grepping secrets from git history",
             re.compile(
                 rf"\b(?i:git)\s+log\b{_SEG}*\B--all\b{_SEG}*\|\s*(?:grep|rg)\b{_SEG}*(?:token|secret|key)"
             ), "Bash"),

        # Category 4: Production write (MAJOR warn) — audit gpt #3 + gemini #3 + #5
        # PW-1: include aws/gcloud (gpt #3 doc-impl drift); kubectl --namespace long form covered in PW-2
        Rule("PW-1", "production-write", "major",
             "deploy command — likely production target",
             re.compile(
                 r"\b(?i:fly|vercel|netlify|heroku|railway|aws|gcloud)\s+deploy\b"
             ), "Bash"),
        # audit gemini #3: support both -n and --namespace (long form)
        Rule("PW-2", "production-write", "major",
             "kubectl mutation against prod namespace (-n or --namespace)",
             re.compile(
                 rf"\b(?i:kubectl)\s+(?:apply|delete|rollout|patch|replace)\b{_SEG}*"
                 r"(?:-n\s+|--namespace[\s=])(?:prod|production)\b"
             ), "Bash"),
        Rule("PW-3", "production-write", "major",
             "AWS ECS/EKS service/cluster update",
             re.compile(r"\b(?i:aws)\s+(?:ecs|eks)\s+(?:update-service|update-cluster)\b"), "Bash"),
        # audit gpt #7 + gemini #5: warn unless --prerelease has truthy value (true|1|yes)
        # Pragmatic v0.1: warn always; user opts out via --prerelease=true (truthy)
        Rule("PW-4", "production-write", "major",
             "gh release create — production unless --prerelease=true|1|yes",
             re.compile(
                 r"\b(?i:gh)\s+release\s+create\b"
                 rf"(?!{_SEG}*\B--prerelease[\s=]+(?:true|1|yes)\b)"
                 rf"(?!{_SEG}*\B--prerelease\b\s*$)"
             ), "Bash"),
        Rule("PW-5", "production-write", "major",
             "terraform apply",
             re.compile(r"\b(?i:terraform)\s+apply\b"), "Bash"),
        Rule("PW-6", "production-write", "major",
             "pulumi up (deploy)",
             re.compile(r"\b(?i:pulumi)\s+up\b"), "Bash"),

        # Category 5: Verification bypass (MAJOR; CRITICAL on protected branch)
        Rule("VB-1", "verification-bypass", "major",
             "git commit --no-verify (skip pre-commit hooks)",
             re.compile(rf"\b(?i:git)\s+commit\b(?={_SEG}*\B--no-verify\b)"), "Bash"),
        Rule("VB-2", "verification-bypass", "major",
             "git push --no-verify (skip pre-push hooks)",
             re.compile(rf"\b(?i:git)\s+push\b(?={_SEG}*\B--no-verify\b)"), "Bash"),
        Rule("VB-3", "verification-bypass", "major",
             "git commit --no-gpg-sign (skip signing)",
             re.compile(rf"\b(?i:git)\s+commit\b(?={_SEG}*\B--no-gpg-sign\b)"), "Bash"),
        # audit gemini #4: order-agnostic (--no-verify before OR after main)
        Rule("VB-4", "verification-bypass", "critical",
             "--no-verify push to protected branch (BPB upgrade; any flag order)",
             re.compile(
                 rf"\b(?i:git)\s+push\b(?={_SEG}*\B--no-verify\b)"
                 rf"(?={_SEG}*\b(?:main|master|release/[\w./-]+)\b)"
             ), "Bash"),
    ]


def load_rules() -> list[Rule]:
    """Load default rules; overlay with AQG_DANGEROUS_GUARD_RULES if set.

    Custom rules JSON shape (list of dicts):
        [{"rule_id", "category", "severity", "description", "command_regex", "tool_name"}]
    """
    rules = _default_rules()
    custom_path = os.environ.get("AQG_DANGEROUS_GUARD_RULES")
    if not custom_path:
        return rules
    try:
        custom = json.loads(Path(custom_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(
            f"WARN: failed to load AQG_DANGEROUS_GUARD_RULES={custom_path}: {exc}",
            file=sys.stderr,
        )
        return rules
    if not isinstance(custom, list):
        print(
            f"WARN: AQG_DANGEROUS_GUARD_RULES must be a JSON list; got {type(custom).__name__}",
            file=sys.stderr,
        )
        return rules
    for entry in custom:
        # audit gpt #8 fix: validate severity to prevent KeyError crash in evaluate
        if not isinstance(entry, dict):
            print("WARN: skipping non-dict custom rule entry (content suppressed)", file=sys.stderr)
            continue
        sev = entry.get("severity")
        # round-4 4a89cc29: isinstance guard — a list/dict severity in a custom
        # rules JSON would raise TypeError on `not in frozenset` (unhashable).
        if not isinstance(sev, str) or sev not in _VALID_SEVERITIES:
            print(
                # round-5 b276170c gpt f3: never echo untrusted custom-rule values
                # (rule_id / severity) — they could carry a token. Report type only.
                f"WARN: skipping custom rule with invalid severity "
                f"(type {type(sev).__name__}; must be one of {sorted(_VALID_SEVERITIES)})",
                file=sys.stderr,
            )
            continue
        try:
            rules.append(
                Rule(
                    rule_id=entry["rule_id"],
                    category=entry["category"],
                    severity=entry["severity"],
                    description=entry["description"],
                    command_regex=re.compile(entry["command_regex"]),
                    tool_name=entry.get("tool_name", "*"),
                )
            )
        except (KeyError, re.error) as exc:
            print(
                # round-5 b276170c gpt f3: suppress raw rule_id + exception text
                # (a bad command_regex exc echoes the regex; rule_id may carry a token).
                f"WARN: skipping malformed custom rule ({type(exc).__name__}; content suppressed)",
                file=sys.stderr,
            )
    return rules


def evaluate(
    tool_name: str, tool_input: dict, rules: Optional[list[Rule]] = None
) -> dict:
    """Evaluate one tool call against rule set; return decision dict."""
    if rules is None:
        rules = load_rules()
    # round-3/4/5: evaluate() must never crash or fail-open on a malformed
    # tool_input. A non-dict (str / list / scalar) is flattened into a command
    # string so its content is still scanned (fail-SAFE, never dropped to {}).
    if not isinstance(tool_input, dict):
        tool_input = {"command": _flatten_to_text(tool_input)}
    # Extract command string. Different tool_name -> different field shape;
    # default to "command" key for Bash. Other tools may use "file_path", etc.
    command = ""
    if tool_name == "Bash":
        # round-5 b276170c gemini f1: flatten a non-str command field (list/dict)
        # instead of passing it raw to regex.search (which would TypeError → fail-open).
        command = _flatten_to_text(tool_input.get("command", ""))
    else:
        # Generic: serialize tool_input to JSON so rules can match against any field.
        try:
            command = json.dumps(tool_input, ensure_ascii=False)
        except (TypeError, ValueError):
            command = str(tool_input)

    # audit 4f0c48c0 #13: secret-leak rules must also scan non-command Bash
    # fields (e.g. `description`) — a secret pasted into description bypassed
    # detection. We DO NOT widen the scan text for non-secret rules: command
    # semantics stay intact (a dangerous-looking string in `description` must
    # not, e.g., block a benign command). Only secret-leak rules see the wider
    # text, built by appending the other string fields after the command.
    secret_scan_text = command
    if tool_name == "Bash":
        extras = _extra_bash_string_fields(tool_input)
        if extras:
            # newline-join so a value cannot bridge across fields into a
            # spurious match (newline is a segment boundary for `_SEG`-bound rules).
            secret_scan_text = "\n".join([command, *extras])

    # #242: shlex-normalised view (None on shlex failure → fail-safe to raw) lets
    # rm-rf-risk rules catch shell-equivalent root spellings (`\/`, `''/`, `$'/'`).
    normalized_command = _normalize_command(command)

    matches = []
    for rule in rules:
        scan_texts = _scan_texts_for_rule(
            rule, command, normalized_command, secret_scan_text
        )
        evidence = None
        for scan_text in scan_texts:
            evidence = rule.matches(tool_name, scan_text)
            if evidence is not None:
                break
        if evidence is not None:
            # audit gpt #1 CRITICAL fix: redact evidence for secret-leak rules
            # so the guard does NOT echo the matched secret it was supposed
            # to prevent leaking. Hash prefix preserves correlation without leak.
            if rule.category == "secret-leak":
                import hashlib
                ev_hash = hashlib.sha256(evidence.encode("utf-8")).hexdigest()[:8]
                stored_evidence = f"[REDACTED:{rule.rule_id} sha256:{ev_hash}]"
            else:
                stored_evidence = evidence
            matches.append(
                {
                    "rule_id": rule.rule_id,
                    "category": rule.category,
                    "severity": rule.severity,
                    "description": rule.description,
                    "evidence": stored_evidence,
                }
            )

    if not matches:
        decision = "allow"
    else:
        max_sev = max(_SEVERITY_RANK[m["severity"]] for m in matches)
        if max_sev == _SEVERITY_RANK["critical"]:
            decision = "block"
        elif max_sev == _SEVERITY_RANK["major"]:
            decision = "warn"
        else:
            decision = "allow"

    return {
        "policy_version": POLICY_VERSION,
        "tool_name": tool_name,
        "decision": decision,
        "matches": matches,
    }


def decision_to_exit(decision: str) -> int:
    return {
        "allow": EXIT_ALLOW,
        "warn": EXIT_WARN,
        "block": EXIT_BLOCK,
    }.get(decision, EXIT_ALLOW)


def render_human(result: dict) -> str:
    """Human-readable reasoning to stderr."""
    decision = result["decision"]
    if decision == "allow" and not result["matches"]:
        return ""  # silent on no-match allow
    lines = [f"AQG dangerous-guard: decision={decision}"]
    for m in result["matches"]:
        lines.append(
            f"  [{m['severity'].upper()}] {m['rule_id']} ({m['category']}): {m['description']}"
        )
        lines.append(f"    matched: {m['evidence']}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aqg_dangerous_guard",
        description="AQG dangerous command guard — pattern-matching rule engine for PreToolUse hooks",
    )
    parser.add_argument(
        "--input",
        help="Path to input JSON file (default: stdin)",
    )
    parser.add_argument(
        "--list-rules",
        action="store_true",
        help="Print loaded rules + exit 0",
    )
    args = parser.parse_args(argv)

    if args.list_rules:
        for r in load_rules():
            print(f"{r.rule_id} [{r.severity}] {r.category}: {r.description}")
        return EXIT_ALLOW

    # Read tool call JSON
    if args.input:
        try:
            payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot read --input {args.input}: {exc}", file=sys.stderr)
            return EXIT_USAGE
    else:
        try:
            payload = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            print(f"ERROR: stdin is not valid JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE

    if not isinstance(payload, dict):
        # GD-04: a non-dict JSON payload ([]/"s"/123/null) would AttributeError on
        # .get(); honor the documented exit-70 bad-input contract instead.
        print(
            f"ERROR: payload must be a JSON object; got {type(payload).__name__}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        print(f"ERROR: tool_input must be dict; got {type(tool_input).__name__}", file=sys.stderr)
        return EXIT_USAGE

    result = evaluate(tool_name, tool_input)

    # stdout: structured JSON for machine parsing
    print(json.dumps(result, ensure_ascii=False))
    # stderr: human reasoning (visible to operator)
    human = render_human(result)
    if human:
        print(human, end="", file=sys.stderr)

    return decision_to_exit(result["decision"])


if __name__ == "__main__":
    raise SystemExit(main())
