# AQG Dangerous Command Guard Policy v0.1

> **Status**: 0.3.0 ship + audit `3a4fe0eb` 13 fixes integrated (Q3 #9 ship, framework v2.1 §3 + §7 #9)
> **Boundary**: AQG owns the policy + example hook. **AQG is NOT an active enforcer**.
> Actual interception = Claude Code / Codex / other PreToolUse-aware client owns.
> AQG does not hold runtime enforcement authority; does not bundle invoker;
> does not actively monitor tool calls.
> **Inspiration**: mattpocock/git-guardrails-claude-code (2026-05-04 ecosystem scan)

## Why this policy exists

Prompt-level guidance ("don't run dangerous commands") is unreliable. Even with
strong instructions in CLAUDE.md / AGENTS.md, an LLM may issue a command that
silently breaks production (deploy, branch-protection bypass, secret leak,
`rm -rf` accident). This policy provides a **technical patch** — a deterministic
pattern matcher that runs at PreToolUse hook time and surfaces matches as
structured decisions.

**It does not replace**:
- Closeout boundary discipline (the 4 + 1 AQG skill surface boundaries still lead).
- Owner authorization for production-adjacent action (still required even if
  this guard returns "allow").

It is the **last technical line of defense** for the cases the prompt-level
discipline missed.

## Risk categories + match rules

### Category 1: Branch protection bypass — **CRITICAL** (block)

> **v0.1 audit fixes**: BPB-1 / BPB-3 / BPB-4 / BPB-5 / BPB-6 use `(?=...)` lookaheads → flag-order-agnostic. BPB-3 consolidated all 3 force-push flags (`-f`, `--force`, `--force-with-lease`) per gpt #5. BPB-3b added for `+refspec` force-update form. Segment-bound `[^|;&\n]*` prevents cross-statement false positives (gpt #6 + gemini #2).

| Rule ID | Pattern | Why |
|---|---|---|
| BPB-1 | `gh pr merge ... --admin` (any flag order) | Bypasses required reviews + checks |
| BPB-3 | `git push ... (-f\|--force\|--force-with-lease) ... (main\|master\|release/...)` | Force-push to protected branch (any flag/order) |
| BPB-3b | `git push ... +[<src>:](refs/heads/)?(main\|master\|release/...)` (target-aware) | `+refspec` force-update incl. `refs/heads/` + colon-less form (#242) |
| BPB-5 | `gh api ... -X DELETE ... /branches/(main\|master)/protection` | Disable branch protection via API |
| BPB-6 | `gh api ... --method (PUT\|PATCH\|DELETE) ... /branches/<...>/protection` | Modify branch protection |

### Category 2: `rm -rf` risk — **CRITICAL** for absolute root/home; **MAJOR** for relative

> **v0.1 audit fixes (gpt #4 + gemini #1)**: All RM rules use `_RM_FORCE` macro matching all variants:
> `-rf` / `-fr` / `-Rf` / `-fR` / `-rF` / `-fR` / `-r -f` / `-f -r` / `--recursive --force` / mixed long+short forms.
> RM-1 also matches root-glob `/*` (was missing). RM-4 supports quoted variables `"$VAR"` and `'$VAR'` (gemini #6).

| Rule ID | Pattern | Severity | Why |
|---|---|---|---|
| RM-1 | `rm <RM_FORCE> /(\*\|\s\|$)` | critical | Delete root or root glob `/*` |
| RM-2 | `rm <RM_FORCE> /(bin\|etc\|usr\|var\|opt\|lib\|sbin\|home\|root\|sys\|proc\|dev)` | critical | Delete top-level system dir |
| RM-3 | `rm <RM_FORCE> ~(\s\|/\|$)` | critical | Delete home |
| RM-4 | `rm <RM_FORCE> ["']?$\{?[A-Z_]+\}?["']?(/|\s\|$)` | major | rm -rf $VAR (or quoted) — risky if VAR empty/unset |
| RM-5 | `rm <RM_FORCE> \.\./` | major | rm -rf with parent traversal |

**Note**: `rm -rf <relative-path>` on a known target is fine (developers do
this every day on `node_modules`, `tmp/`, etc.). Only patterns above flag.

**#242 shell-tokenisation**: RM rules also run on a shlex-normalised view, so
shell-equivalent root spellings `\/`, `''/` (and `"/"`) are caught. ANSI-C quoting
`$'/'` / `$'\x2f'` / `''$'/'` is a **documented residual** — shlex (POSIX) does not
decode it and a regex cannot track quote state; correct handling needs a state
machine, out of scope for an advisory example hook.

### Category 3: Secret leak — **CRITICAL** (block)

| Rule ID | Pattern | Why |
|---|---|---|
| SL-1 | `echo [^']*\$\{?(?:TOKEN\|API_KEY\|SECRET\|PASSWORD\|PWD\|CREDENTIAL)` (single-quote-excluding prefix) | Print secret env; `echo "x: $T"` blocks, `echo '$T'` (literal) allowed (#242 GD-03) |
| SL-2 | `printf [^|;&\n]*\$\{?(?:TOKEN\|API_KEY\|SECRET\|PASSWORD)` | Same as SL-1; keeps broad prefix (a single-quoted format arg before a `"$SECRET"` must still block) |
| SL-3 | `cat\s+\.?env(\.\w+)?(\s\|$)` | Print env file contents |
| SL-4 | `cat\s+.*credentials?\b` | Print credentials file |
| SL-5 | `AKIA[0-9A-Z]{16}` (literal AWS key in command) | AWS access key in plaintext command |
| SL-6 | `gh[pos]_[A-Za-z0-9_]{36,}` (literal GH token) | GitHub token in plaintext command |
| SL-7 | `sk_(?:live\|test)_[A-Za-z0-9]{24,}` (literal Stripe key) | Stripe key in plaintext command |
| SL-8 | `git log .*--all ... \| (grep\|rg) ... (token\|secret\|key)` | Grepping secret patterns from git history |

> **v0.1 audit fix (gpt #1 CRITICAL)**: SL-5/6/7/8 evidence is REDACTED in output:
> `[REDACTED:<rule_id> sha256:<first8>]`. The guard does not echo back the matched
> secret it was supposed to prevent leaking. Hash prefix preserves correlation
> across log lines without exposing the token.

### Category 4: Production write — **MAJOR** (warn + ack)

> **v0.1 audit fixes**: PW-2 supports both short `-n` and long `--namespace`/`--namespace=` (gemini #3). PW-4 only suppresses warning when `--prerelease` has truthy value (`true`/`1`/`yes`); explicit `--prerelease=false` still warns (gpt #7 + gemini #5).

| Rule ID | Pattern | Why |
|---|---|---|
| PW-1 | `(fly\|vercel\|netlify\|aws\|gcloud\|heroku\|railway) deploy` | Deploy to (likely) production |
| PW-2 | `kubectl (apply\|delete\|rollout\|patch\|replace) ... (-n\|--namespace[\s=])(prod\|production)` | k8s prod-ns mutation |
| PW-3 | `aws (ecs\|eks) (update-service\|update-cluster)` | ECS/EKS prod service update |
| PW-4 | `gh release create` (unless `--prerelease (true\|1\|yes)`) | Production release publish |
| PW-5 | `terraform apply` | TF apply |
| PW-6 | `pulumi up` | Pulumi deploy |

**Severity rationale**: many projects legitimately do these as part of normal
release flow. Default is warn so the operator confirms intent; project policy
may downgrade to allow or upgrade to block.

### Category 5: Verification bypass — **MAJOR** (warn)

| Rule ID | Pattern | Why |
|---|---|---|
| VB-1 | `git commit\b.*\b--no-verify\b` | Skip pre-commit hooks |
| VB-2 | `git push\b.*\b--no-verify\b` | Skip pre-push hooks |
| VB-3 | `git commit\b.*\b--no-gpg-sign\b` | Skip GPG signing |
| VB-4 | `git push ... --no-verify ... (main\|master\|release/...)` (any flag order) | **Upgrade to CRITICAL** when targeting protected branch (gemini #4: order-agnostic via lookahead) |

## Severity → exit code mapping

| Severity | Exit code | Behavior (default example hook) |
|---|---|---|
| `critical` | **2** | Block (PreToolUse-aware client should refuse the tool call) |
| `major` | **1** | Warn (PreToolUse-aware client should surface to user; may proceed) |
| `minor` | **0** | Allow + info log only |
| no match | **0** | Allow (silent) |

The example hook prints structured decision JSON to **stdout** (machine-readable
for clients that parse it) and human-readable reasoning to **stderr** (always
visible to operator).

## Output schema (stdout JSON)

```json
{
  "policy_version": "v0",
  "tool_name": "Bash",
  "decision": "allow|warn|block",
  "matches": [
    {
      "rule_id": "RM-1",
      "category": "rm -rf",
      "severity": "critical",
      "description": "Delete root",
      "evidence": "<the matched portion of the command>"
    }
  ]
}
```

## Configuration: additional rules

Set `AQG_DANGEROUS_GUARD_RULES=/path/to/custom-rules.json` to **append** custom
rules to the defaults. The JSON shape is a list of rule objects; severity must
be one of `minor`/`major`/`critical` (audit gpt #8 fix: invalid severity is
warned + skipped, no longer crashes evaluation).

**Disable semantics**: v0.1 only **appends**, does not replace. To effectively
allow a default rule, supply a custom rule with the same `command_regex` and
`severity: minor` (which decisions to allow). Full disable + override semantics
deferred to v1.

```json
[
  {
    "rule_id": "CUSTOM-1",
    "category": "custom",
    "severity": "critical",
    "description": "Block any 'forbidden_keyword'",
    "command_regex": "forbidden_keyword",
    "tool_name": "Bash"
  }
]
```

## Boundary disclaimer (re-emphasized)

This policy + example hook is **NOT** a guarantee. It is a deterministic
pattern matcher that catches what we know to look for. New attack patterns
emerge; obfuscation defeats regex; clients may not honor the exit code. **It
does not replace**:

- Owner authorization for production-adjacent actions
- Branch protection on the GitHub server side (this is defense-in-depth, not
  a substitute)
- Closeout boundary discipline of the 4 + 1 AQG skills
- Incident response when something does slip through

Use it as **one layer**, not the only layer.

## Refs

- Framework v2.1 §3 (Build/Adapt/Defer matrix) — AQG own boundary
- Framework v2.1 §7 #9 — roadmap registration
- mattpocock/git-guardrails-claude-code — pattern inspiration (2026-05-04 ecosystem scan)
- `scripts/aqg_dangerous_guard.py` — rule engine implementation
- `agent-packs/claude-code/hooks/dangerous-guard.example.sh` — PreToolUse hook wrapper
