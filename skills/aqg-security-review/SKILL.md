---
name: aqg-security-review
description: Review code for OWASP Top 10, CWE Top 25, and secure-by-default library usage in any repository. Use PROACTIVELY when writing authentication or authorization, handling user input or file uploads, working with secrets / credentials / API keys, creating new API endpoints, implementing payment / PII / sensitive-data handling, calling external APIs, modifying cryptography, or adding/modifying dependency manifests (package.json / requirements.txt / Cargo.toml / go.mod / Gemfile / pyproject.toml). Complementary to semgrep SAST and audit-mcp `/audit`; not a substitute. Defaults to the current git root and supports `--repo` override. Does not replace the host built-in /review slash command.
---

# AQG Security Review

Use this skill as the **in-session checklist** layer of AQG's three-layer security model: a structured 6-step prompt-driven review covering OWASP Top 10, CWE Top 25, and secure-by-default library usage. Output integrates directly into `aqg-evidence-closeout`.

## Three-Layer Complementarity

| layer | tool | scope | runs |
|---|---|---|---|
| in-session checklist | **aqg-security-review** (this) | reviewer-driven OWASP / CWE / secure-defaults walk-through | as you write code |
| deterministic SAST | semgrep | regex / AST pattern matching | CI + on demand |
| LLM external review | audit-mcp via `/audit` (fast / standard / deep) | cross-model semantic second-opinion | before commit / PR merge |

The three do not overlap and do not substitute for each other. Run all three when the change is security-touching.

> **Catalog snapshot**: the OWASP / CWE / secure-defaults tables are a point-in-time snapshot (OWASP Top 10 2021, CWE Top 25 2023 + 2024 net-new). Treat them as a checklist baseline, not a live authority — re-verify against the latest OWASP / CWE releases periodically.

## How To Run

Resolve the AQG root via the shared helper, then run the security-review helper:

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script="$aqg_root/skills/aqg-security-review/scripts/aqg_security_review.py"
python3 "$script" --repo "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```

The helper prints the OWASP / CWE / secure-defaults reference tables plus a structured ledger skeleton. Flags: `--surface auth,input,...` to filter by surface; `--json` for machine-readable output; `--list owasp|cwe|defaults` to print only one section.

## Workflow

1. **Identify Surface** — list the security surfaces touched: `auth` / `input` / `secrets` / `injection` / `serialization` / `xss` / `csrf` / `crypto` / `dependencies` / `logging`.
2. **OWASP Top 10 Mapping** — for each surface, walk the 10 categories; each finding records severity (`CRITICAL` / `HIGH` / `MEDIUM` / `LOW`) and ≥1 evidence pointer (`file:line`).
3. **CWE Checklist Review** — manually map observed code evidence to CWE Top 25 (hardcoded creds / SQL string concat / shell+input / unsafe deserialize / weak crypto / SSRF / path traversal / etc.); use semgrep for automated regex/AST scanning (this skill does not scan). Each hit records CWE id + severity + fix sketch.
4. **Defense Layer Verification** — three independent sub-checks:
   - **a. Control existence**: auth / input validation / rate limit / CSRF / output escape / secret management — present?
   - **b. Secure-by-default library use**: project uses mature libraries (Helmet, DOMPurify, Bleach, Tink, defusedxml, Gorilla CSRF, ssrf_filter, SerialKiller, Mustache) instead of custom security implementations? Custom = `MEDIUM` (general code) / `HIGH` (PII / payment / auth).
   - **c. Defense-in-depth**: layered (e.g. CSRF token + SameSite cookie + Origin check) vs single-point.
5. **Decision** — emit `accept` / `reject` / `needs-secret-rotation` (no fuzzy "looks OK"). `reject` blocks merge until findings are fixed; `needs-secret-rotation` triggers immediate revoke + rotate before any other action.
6. **Evidence Hand-off** — emit a YAML block that imports directly into `aqg-evidence-closeout`.

## Decision Output Shape (closeout-importable)

```yaml
security_review:
  surfaces_audited: [auth, input, secrets]
  owasp_findings:
    - category: A03_injection
      severity: HIGH
      pattern: user input concatenated into SQL
      evidence: src/users.py:42
      fix: parameterize via prepared statement
  cwe_findings:
    - cwe: CWE-89
      severity: HIGH
      evidence: src/users.py:42
  secure_defaults:
    - layer: csrf
      library: Gorilla CSRF (^1.7)
      anti_pattern_avoided: custom CSRF token impl
    - layer: html_escape
      library: Bleach (^6.0)
      anti_pattern_avoided: hand-rolled escape
  defense_in_depth:
    auth: [oauth, mfa, rate_limit]
  decision: accept  # one of: accept | reject | needs-secret-rotation
  decision_reason: prepared statement migration committed in src/users.py
  audit_id: null  # set to the audit_id if cross-checked
```

The block is **manually pasted** into the `.aqg/current_ledger.md` Evidence section by the agent. Auto-import via `aqg_closeout --security-review-file <path>` is a v0.2 follow-up (current `aqg_closeout` helper does not yet read this block — see closeout helper CLI for current options).

## Boundaries

- This skill does NOT run regex / AST scanning — that is semgrep's role; invoke via `mcp__plugin_semgrep_semgrep__semgrep_scan` or CI.
- This skill does NOT call external models — that is audit-mcp's role; for cross-model verification run `/audit` at the depth `docs/policies/audit-trigger.md` selects. Do not default to `standard`: this skill's trigger list is that policy's Gate A sensitivity list, so a change that fired one of those triggers is already `deep`. A read-only pass over unchanged code is not.
- This skill does NOT modify code — output is findings + decision; integration is the agent's responsibility.
- Production / deploy / restart / credentials / raw private data / Owner-admin actions remain separate authorization gates — never claimed done from this skill alone.
- **Owner-only 5 items** — escalate; never decide:
  1. **business direction** (which markets, which features, ship vs cut)
  2. **strategy** (build vs buy, in-house vs vendor, architecture pivot)
  3. **taste** (UX / brand / wording / aesthetic judgment)
  4. **tacit knowledge** (unwritten team conventions, project history, intent inferred from past sessions)
  5. **long-horizon + irreversible actions** (production deploy, credential rotation, data migration, irreversible architecture / data-model decisions, payment / identity / privacy semantics)

## References

- OWASP Top 10: <https://owasp.org/Top10/> — the helper tables are the **2021** edition with key **2025 net-new checks appended** (A01 fail-open → OWASP 2025 A10 Mishandling of Exceptional Conditions; A06 dependency-confusion → OWASP 2025 A03 Software Supply Chain Failures). The full 2025 re-ranking is NOT mirrored — cross-check the live catalog (<https://owasp.org/Top10/2025/en/>).
- CWE Top 25: <https://cwe.mitre.org/top25/> — the helper tables are the **2023** edition with selected **net-new entries appended** (CWE-200 Exposure of Sensitive Information — 2024 #17 / 2025 #20; CWE-400 Uncontrolled Resource Consumption — 2024 #24, dropped from 2025). The current live release is **CWE Top 25:2025**; the full re-rank is NOT mirrored — cross-check the live catalog.
- Awesome Secure Defaults: <https://github.com/tldrsec/awesome-secure-defaults>
- Complementary AQG layers: `skills/aqg-evidence-closeout/SKILL.md` (ledger import target), `skills/aqg-systematic-debugging/SKILL.md` (6-step style), `skills/aqg-audit-adjudication/SKILL.md` (audit findings adjudication)
