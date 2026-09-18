#!/usr/bin/env python3
"""Print OWASP Top 10 / CWE Top 25 / secure-by-default checklist for in-session review.

This helper is the data + output side of the `aqg-security-review` skill (see
`skills/aqg-security-review/SKILL.md`). It prints structured reference tables
that the reviewer walks through during the 6-step workflow defined in the skill,
then emits a closeout-ready ledger skeleton.

Position in AQG's three-layer security model:
  - aqg-security-review (this) = in-session prompt-driven checklist
  - semgrep                    = deterministic SAST
  - audit-mcp `/audit`        = LLM external review

This script does NOT run automated regex / AST / secret scanning. It prints the
reference tables and a ledger skeleton for the reviewer to fill from
current-session evidence. For automated SAST run semgrep; for cross-model
verification call `/audit`.

Exit codes:
  0: success — checklist printed; closeout-ready skeleton emitted
  1: reserved — not currently emitted (placeholder for a future surface-blocker)
  2: usage error — argparse on bad flags; or a non-empty --surface that filters
     to no valid surface (fail-closed, not an empty checklist)
  3: reserved — config / schema error, not currently emitted
  70: reserved — internal error placeholder, not currently emitted
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any


# ============================================================================
# Data tables (OWASP 2025 / CWE Top 25 2025 / secure-defaults 8 cat.)
#
# CATALOG SNAPSHOT - point-in-time as of 2026-09-16 (OWASP Top 10 = 2025
# edition; CWE Top 25 = 2025 release). This is a STATIC snapshot, NOT a live
# authority: when OWASP/CWE publish a newer edition, re-verify these tables and
# bump this date; a stale catalog must not be presented as current guidance.
# ============================================================================


# OWASP Top 10 (2025 edition). Each entry: 3 patterns + severity + fix sketch.
# Severities reflect typical impact in production; project context can override.
OWASP_TOP_10: list[dict[str, Any]] = [
    {
        "id": "A01:2025",
        "name": "Broken Access Control",
        "patterns": [
            {"pattern": "missing authorization check on state-changing endpoint",
             "severity": "HIGH",
             "fix": "add explicit role / ownership check before mutate"},
            {"pattern": "predictable resource id (sequential int) without owner check",
             "severity": "HIGH",
             "fix": "use UUID + verify resource.owner_id == requester.id"},
            {"pattern": "client-side-only role gating (frontend hides admin button, API exposed)",
             "severity": "CRITICAL",
             "fix": "enforce role check server-side on every privileged route"},
            {"pattern": "server-side URL fetch or resource access bypasses authorization boundaries (SSRF / IDOR / forced browsing)",
             "severity": "HIGH",
             "fix": "deny by default; enforce allowlists, resource ownership, and private-network blocks"},
        ],
    },
    {
        "id": "A02:2025",
        "name": "Security Misconfiguration",
        "patterns": [
            {"pattern": "default credentials or sample config shipped to production",
             "severity": "CRITICAL",
             "fix": "rotate at deploy; fail-closed if defaults detected at boot"},
            {"pattern": "missing security headers (CSP / HSTS / X-Frame-Options / X-Content-Type-Options)",
             "severity": "MEDIUM",
             "fix": "use Helmet.js (Node) / secure_headers (Ruby) / equivalent middleware"},
            {"pattern": "verbose error messages / stack traces returned to user",
             "severity": "MEDIUM",
             "fix": "generic user error; full stack to server logs only"},
        ],
    },
    {
        "id": "A03:2025",
        "name": "Software Supply Chain Failures",
        "patterns": [
            {"pattern": "no dependency scanning, SBOM, or vulnerability alerting in CI",
             "severity": "HIGH",
             "fix": "Dependabot / Renovate + SCA audit gate + SBOM tracking"},
            {"pattern": "lockfile not committed / not used in CI (`npm install` instead of `npm ci`)",
             "severity": "MEDIUM",
             "fix": "commit lockfile; CI uses frozen install (`npm ci` / `uv sync --frozen` / `cargo --locked`)"},
            {"pattern": "dependency confusion or untrusted artifact source (registry/source not pinned, unsigned artifact)",
             "severity": "HIGH",
             "fix": "scope internal packages; pin registry per scope; verify signatures, provenance, and hashes"},
        ],
    },
    {
        "id": "A04:2025",
        "name": "Cryptographic Failures",
        "patterns": [
            {"pattern": "MD5 / SHA-1 used for password hashing or signature",
             "severity": "HIGH",
             "fix": "use Argon2id / bcrypt for passwords; SHA-256+ for signatures"},
            {"pattern": "hand-rolled AES wrapper (custom IV, ECB mode, key stored alongside data)",
             "severity": "CRITICAL",
             "fix": "use Google Tink / libsodium / Themis high-level AEAD primitives"},
            {"pattern": "secrets transmitted over HTTP / stored in plaintext config",
             "severity": "HIGH",
             "fix": "TLS-only transport; secrets via secret manager (AWS SM / Vault / Doppler)"},
        ],
    },
    {
        "id": "A05:2025",
        "name": "Injection",
        "patterns": [
            {"pattern": "user input concatenated into SQL string",
             "severity": "HIGH",
             "fix": "parameterized queries / ORM bindings; never f-string SQL"},
            {"pattern": "user input passed to shell / `subprocess.run(shell=True)`",
             "severity": "CRITICAL",
             "fix": "shell=False + argument list; or use pathlib + library APIs"},
            {"pattern": "unsafe template rendering (eval, dynamic Jinja with autoescape=False)",
             "severity": "HIGH",
             "fix": "logic-less template (Mustache / Handlebars / Liquid); autoescape=True"},
        ],
    },
    {
        "id": "A06:2025",
        "name": "Insecure Design",
        "patterns": [
            {"pattern": "no rate limit on auth / password-reset / OTP endpoints",
             "severity": "HIGH",
             "fix": "global + per-IP + per-account rate limits; lockout on burst"},
            {"pattern": "trust boundary missing — frontend computes price / discount, server accepts",
             "severity": "HIGH",
             "fix": "all sensitive computation server-side; treat client as untrusted"},
            {"pattern": "no threat model for sensitive feature (payment, file upload, OAuth)",
             "severity": "MEDIUM",
             "fix": "STRIDE / abuse-case walk before merge"},
        ],
    },
    {
        "id": "A07:2025",
        "name": "Authentication Failures",
        "patterns": [
            {"pattern": "session token in localStorage (XSS-stealable)",
             "severity": "HIGH",
             "fix": "HttpOnly + Secure + SameSite=Lax/Strict cookie"},
            {"pattern": "weak password policy (no length / breach check / MFA path)",
             "severity": "MEDIUM",
             "fix": "min 12 chars; HaveIBeenPwned check; offer / require MFA on sensitive ops"},
            {"pattern": "predictable session id / no rotation on login / privilege change",
             "severity": "HIGH",
             "fix": "cryptographic random; rotate on auth state change"},
        ],
    },
    {
        "id": "A08:2025",
        "name": "Software or Data Integrity Failures",
        "patterns": [
            {"pattern": "untrusted deserialization (pickle / yaml.load / unsafe Java ObjectInputStream)",
             "severity": "CRITICAL",
             "fix": "JSON / yaml.safe_load / JEP 290 ObjectInputFilter / format-specific safe parser"},
            {"pattern": "CI pipeline pulls unsigned artifact / unverified install script",
             "severity": "HIGH",
             "fix": "pin sha256 / use signed releases / SLSA provenance"},
            {"pattern": "auto-update mechanism without integrity verification",
             "severity": "HIGH",
             "fix": "TLS + signature verification + rollback path"},
        ],
    },
    {
        "id": "A09:2025",
        "name": "Security Logging and Alerting Failures",
        "patterns": [
            {"pattern": "auth failures / privileged actions not logged",
             "severity": "MEDIUM",
             "fix": "structured log with actor / action / target / timestamp / outcome"},
            {"pattern": "secrets / PII / passwords appear in log messages",
             "severity": "HIGH",
             "fix": "redaction filter at log layer; never log raw credentials or tokens"},
            {"pattern": "no alerting on anomaly (login spike, 5xx burst, exfil pattern)",
             "severity": "MEDIUM",
             "fix": "metrics + alert thresholds; on-call rotation"},
        ],
    },
    {
        "id": "A10:2025",
        "name": "Mishandling of Exceptional Conditions",
        "patterns": [
            {"pattern": "authorization fails open when permission checks raise or return an unexpected value",
             "severity": "CRITICAL",
             "fix": "default deny; treat check errors and unknown states as denial"},
            {"pattern": "partial state is committed after a security-critical operation fails",
             "severity": "HIGH",
             "fix": "wrap in transactions / compensating rollback; make failure atomic"},
            {"pattern": "unchecked return value from crypto, auth, parser, or verifier library",
             "severity": "HIGH",
             "fix": "check every security-critical return/result; propagate or fail closed"},
        ],
    },
]


# CWE Top 25 (2025 release). Compact form: id + name + severity + fix sketch.
CWE_TOP_25: list[dict[str, str]] = [
    {"cwe": "CWE-79", "name": "Improper Neutralization of Input During Web Page Generation ('Cross-site Scripting')", "severity": "HIGH", "fix": "DOMPurify (JS) / nh3 (Py) / Ammonia (Rust); template autoescape"},
    {"cwe": "CWE-89", "name": "Improper Neutralization of Special Elements used in an SQL Command ('SQL Injection')", "severity": "HIGH", "fix": "parameterized query / ORM bind; never string-concat SQL"},
    {"cwe": "CWE-352", "name": "Cross-Site Request Forgery (CSRF)", "severity": "HIGH", "fix": "Gorilla CSRF / anti-csrf token + SameSite cookie + Origin check"},
    {"cwe": "CWE-862", "name": "Missing Authorization", "severity": "HIGH", "fix": "explicit `assert_can(user, action, resource)` on every privileged op"},
    {"cwe": "CWE-787", "name": "Out-of-bounds Write", "severity": "CRITICAL", "fix": "bounds check; use safe collection / Vec / std::array"},
    {"cwe": "CWE-22", "name": "Improper Limitation of a Pathname to a Restricted Directory ('Path Traversal')", "severity": "HIGH", "fix": "resolve real path + verify under allowlisted root; reject `..` segments"},
    {"cwe": "CWE-416", "name": "Use After Free", "severity": "CRITICAL", "fix": "RAII / smart pointers / borrow checker (Rust); audit raw `delete`"},
    {"cwe": "CWE-125", "name": "Out-of-bounds Read", "severity": "HIGH", "fix": "bounds check; safe slice; avoid raw pointer arithmetic"},
    {"cwe": "CWE-78", "name": "Improper Neutralization of Special Elements used in an OS Command ('OS Command Injection')", "severity": "CRITICAL", "fix": "subprocess argv list; no `shell=True` with user input"},
    {"cwe": "CWE-94", "name": "Improper Control of Generation of Code ('Code Injection')", "severity": "CRITICAL", "fix": "no eval / Function() / exec() with user input; sandboxed evaluator if unavoidable"},
    {"cwe": "CWE-120", "name": "Buffer Copy without Checking Size of Input ('Classic Buffer Overflow')", "severity": "CRITICAL", "fix": "size-checked copy APIs; bounds-checked containers; fuzz unsafe parsers"},
    {"cwe": "CWE-434", "name": "Unrestricted Upload of File with Dangerous Type", "severity": "HIGH", "fix": "validate mime + magic bytes + extension; store outside web root"},
    {"cwe": "CWE-476", "name": "NULL Pointer Dereference", "severity": "MEDIUM", "fix": "Optional / Result / null check before deref"},
    {"cwe": "CWE-121", "name": "Stack-based Buffer Overflow", "severity": "CRITICAL", "fix": "bounds-check stack buffers; avoid fixed-size stack copies; prefer safe containers"},
    {"cwe": "CWE-502", "name": "Deserialization of Untrusted Data", "severity": "CRITICAL", "fix": "yaml.safe_load / JSON only / JEP 290 ObjectInputFilter; never pickle untrusted"},
    {"cwe": "CWE-122", "name": "Heap-based Buffer Overflow", "severity": "CRITICAL", "fix": "bounds-check heap buffers; prefer safe allocation wrappers; fuzz parsers"},
    {"cwe": "CWE-863", "name": "Incorrect Authorization", "severity": "HIGH", "fix": "policy engine (OPA / Cedar) over scattered conditionals; test matrix"},
    {"cwe": "CWE-20", "name": "Improper Input Validation", "severity": "HIGH", "fix": "schema-based validation (Zod / Pydantic / serde); allowlist not blocklist"},
    {"cwe": "CWE-284", "name": "Improper Access Control", "severity": "HIGH", "fix": "centralize deny-by-default access controls; test unauthenticated and under-authorized paths"},
    {"cwe": "CWE-200", "name": "Exposure of Sensitive Information to an Unauthorized Actor", "severity": "HIGH", "fix": "allowlist response fields; never return stack traces, internal IDs, or full user objects to under-authorized callers"},
    {"cwe": "CWE-306", "name": "Missing Authentication for Critical Function", "severity": "CRITICAL", "fix": "require auth on every state-changing endpoint; no debug bypass"},
    {"cwe": "CWE-918", "name": "Server-Side Request Forgery (SSRF)", "severity": "HIGH", "fix": "explicit host allowlist; deny private + link-local IPs; mitigate DNS rebinding"},
    {"cwe": "CWE-77", "name": "Improper Neutralization of Special Elements used in a Command ('Command Injection')", "severity": "CRITICAL", "fix": "argv list; escape via library, never manual"},
    {"cwe": "CWE-639", "name": "Authorization Bypass Through User-Controlled Key", "severity": "HIGH", "fix": "derive resource keys server-side; enforce ownership after lookup; test IDOR paths"},
    {"cwe": "CWE-770", "name": "Allocation of Resources Without Limits or Throttling", "severity": "HIGH", "fix": "per-endpoint CPU/mem/time budgets; max pagination limits; file-size and recursion-depth caps"},
]


# Secure-by-default library categories (8 categories per Owner spec).
# Source: https://github.com/tldrsec/awesome-secure-defaults
SECURE_DEFAULTS: list[dict[str, Any]] = [
    {
        "category": "HTTP security headers",
        "libraries": [
            {"name": "Helmet.js", "ecosystem": "Node.js", "url": "https://helmetjs.github.io/"},
            {"name": "secure_headers", "ecosystem": "Ruby", "url": "https://github.com/github/secure_headers"},
        ],
        "anti_pattern": "hand-set headers; missing CSP / HSTS / X-Frame-Options",
    },
    {
        "category": "XSS / HTML sanitization",
        "libraries": [
            {"name": "DOMPurify", "ecosystem": "JavaScript / TypeScript", "url": "https://github.com/cure53/DOMPurify"},
            {"name": "nh3", "ecosystem": "Python (Ammonia bindings — Bleach successor)", "url": "https://github.com/messense/nh3"},
            {"name": "Ammonia", "ecosystem": "Rust", "url": "https://github.com/rust-ammonia/ammonia"},
        ],
        "anti_pattern": "hand-rolled HTML escape; regex strip-tags; `dangerouslySetInnerHTML` without sanitize. Note: Mozilla deprecated Bleach in 2023 — migrate to nh3.",
    },
    {
        "category": "CSRF protection",
        "libraries": [
            {"name": "Gorilla CSRF", "ecosystem": "Go", "url": "https://github.com/gorilla/csrf"},
            {"name": "anti-csrf", "ecosystem": "PHP", "url": "https://github.com/paragonie/anti-csrf"},
        ],
        "anti_pattern": "self-implemented CSRF token; no SameSite cookie attribute",
    },
    {
        "category": "Cryptographic primitives",
        "libraries": [
            {"name": "Google Tink", "ecosystem": "Java / Go / C++ / Obj-C / Python (TS alpha)", "url": "https://github.com/tink-crypto/tink"},
            {"name": "Themis", "ecosystem": "14+ platforms", "url": "https://github.com/cossacklabs/themis"},
        ],
        "anti_pattern": "hand-rolled AES wrapper; custom IV management; ECB mode; constant-time check absent. Note: legacy `google/tink` monorepo archived — current active repos under `tink-crypto/` org.",
    },
    {
        "category": "Input validation (regex / XML)",
        "libraries": [
            {"name": "safe-regex", "ecosystem": "JavaScript (heuristic only — false positives/negatives documented)", "url": "https://github.com/davisjam/safe-regex"},
            {"name": "defusedxml", "ecosystem": "Python", "url": "https://github.com/tiran/defusedxml"},
        ],
        "anti_pattern": "catastrophic regex (nested quantifiers); raw `xml.etree` on untrusted XML. Note: safe-regex is a heuristic — pair with hard regex timeout / RE2-style safe engine for production.",
    },
    {
        "category": "SSRF defense",
        "libraries": [
            {"name": "ssrf_filter", "ecosystem": "Ruby", "url": "https://github.com/arkadiyt/ssrf_filter"},
            {"name": "ssrf-req-filter", "ecosystem": "Node.js", "url": "https://github.com/y-mehta/ssrf-req-filter"},
        ],
        "anti_pattern": "direct user URL fetch; no IP allowlist; no DNS rebinding mitigation",
    },
    {
        "category": "Safe deserialization",
        "libraries": [
            {"name": "JEP 290 ObjectInputFilter", "ecosystem": "Java 9+ stdlib", "url": "https://openjdk.org/jeps/290"},
            {"name": "Jackson safe-typing", "ecosystem": "Java (instead of default polymorphic types)", "url": "https://github.com/FasterXML/jackson-databind/wiki/Mapper-Features"},
            {"name": "yaml.safe_load", "ecosystem": "Python (PyYAML third-party safe API)", "url": "https://pyyaml.org/wiki/PyYAMLDocumentation"},
        ],
        "anti_pattern": "raw `ObjectInputStream` without filter / `pickle.loads` / `yaml.load` on untrusted data; legacy `SerialKiller` (last release 2016, unmaintained — use JEP 290 ObjectInputFilter instead).",
    },
    {
        "category": "Logic-less templates (anti-injection)",
        "libraries": [
            {"name": "Mustache", "ecosystem": "multi", "url": "https://mustache.github.io/"},
            {"name": "Handlebars", "ecosystem": "JavaScript", "url": "https://handlebarsjs.com/"},
            {"name": "Liquid", "ecosystem": "Ruby / JS", "url": "https://shopify.github.io/liquid/"},
        ],
        "anti_pattern": "string interpolation into user-rendered HTML / template content; Jinja `autoescape=False`. Scope: HTML / text / template rendering only — SQL injection mitigation lives in OWASP A03 (parameterized queries), shell injection in CWE-78 (argv list APIs); logic-less templates do not address those.",
    },
]


SURFACES: list[str] = [
    "auth", "input", "secrets", "injection", "serialization",
    "xss", "csrf", "crypto", "dependencies", "logging",
]


# ============================================================================
# Output rendering
# ============================================================================


def _print_owasp() -> None:
    print("## OWASP Top 10 (2025 current baseline)\n")
    print("| id | category | severity range | example patterns |")
    print("|---|---|---|---|")
    for entry in OWASP_TOP_10:
        sev = sorted({p["severity"] for p in entry["patterns"]})
        ex = "; ".join(p["pattern"] for p in entry["patterns"])
        print(f"| {entry['id']} | {entry['name']} | {'/'.join(sev)} | {ex} |")
    print()


def _print_cwe() -> None:
    print("## CWE Top 25 (2025 current baseline)\n")
    print("| cwe | name | severity | fix sketch |")
    print("|---|---|---|---|")
    for c in CWE_TOP_25:
        print(f"| {c['cwe']} | {c['name']} | {c['severity']} | {c['fix']} |")
    print()


def _print_defaults() -> None:
    print("## Secure-by-default libraries (8 categories)\n")
    print("| category | recommended libs | anti-pattern |")
    print("|---|---|---|")
    for d in SECURE_DEFAULTS:
        libs = "; ".join(f"{l['name']} ({l['ecosystem']})" for l in d["libraries"])
        print(f"| {d['category']} | {libs} | {d['anti_pattern']} |")
    print()


def _print_skeleton(repo: Path, surfaces: list[str]) -> None:
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("## Closeout-ready security_review block\n")
    print("```yaml")
    print("security_review:")
    print(f"  reviewed_at: {today}")
    print(f"  repo: {json.dumps(str(repo))}")
    print(f"  surfaces_audited: [{', '.join(surfaces)}]")
    print("  owasp_findings: []  # TODO fill from §2 walk")
    print("  cwe_findings: []    # TODO fill from section 3 review")
    print("  secure_defaults: [] # TODO fill from §4b: layer / library / anti_pattern_avoided")
    print("  defense_in_depth: {} # TODO fill from §4c")
    print("  decision: TODO     # accept | reject | needs-secret-rotation")
    print("  decision_reason: TODO")
    print("  audit_id: null     # set if cross-checked via /audit")
    print("```\n")


def _emit_json(repo: Path, surfaces: list[str], section: str = "all") -> None:
    payload = {
        "repo": str(repo),
        "surfaces_audited": surfaces,
        "ledger_skeleton": {
            "security_review": {
                "reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "repo": str(repo),
                "surfaces_audited": surfaces,
                "owasp_findings": [],
                "cwe_findings": [],
                "secure_defaults": [],
                "defense_in_depth": {},
                "decision": None,
                "decision_reason": None,
                "audit_id": None,
            }
        },
    }
    if section in ("owasp", "all"):
        payload["owasp_top_10"] = OWASP_TOP_10
    if section in ("cwe", "all"):
        payload["cwe_top_25"] = CWE_TOP_25
    if section in ("defaults", "all"):
        payload["secure_defaults"] = SECURE_DEFAULTS
    print(json.dumps(payload, indent=2, ensure_ascii=False))


# ============================================================================
# CLI
# ============================================================================


def _sanitize_inline(text: str) -> str:
    """Collapse a value to a safe single-line string for a markdown heading:
    strip ANSI escapes, then replace any control char (newline / tab / etc.)
    with a space. The YAML skeleton renders repo via json.dumps separately (C1)."""
    no_ansi = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return re.sub(r"[\x00-\x1f\x7f]", " ", no_ansi).strip()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aqg_security_review.py",
        description="Print OWASP / CWE / secure-defaults checklist + closeout-ready skeleton.",
    )
    p.add_argument("--repo", type=Path, default=Path.cwd(),
                   help="repo root path; defaults to current working directory")
    p.add_argument("--surface", default="",
                   help="comma-separated surfaces (auth,input,secrets,...); empty = all")
    p.add_argument("--list", choices=["owasp", "cwe", "defaults", "all"], default="all",
                   help="print only one table (default: all)")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of markdown")
    return p


def _resolve_surfaces(arg: str) -> list[str]:
    if not arg.strip():
        return list(SURFACES)
    requested = [s.strip() for s in arg.split(",") if s.strip()]
    unknown = [s for s in requested if s not in SURFACES]
    if unknown:
        print(f"warn: unknown surfaces ignored: {unknown}; valid: {SURFACES}",
              file=sys.stderr)
    return [s for s in requested if s in SURFACES]


def main() -> int:
    args = _build_parser().parse_args()
    surfaces = _resolve_surfaces(args.surface)
    # P2 (audit 3907d9af): fail-closed — a non-empty --surface that filters to
    # nothing must not emit an empty-surface checklist.
    if args.surface.strip() and not surfaces:
        print(f"ERROR: no valid surface in --surface {args.surface!r}; "
              f"valid: {SURFACES}", file=sys.stderr)
        return 2

    if args.json:
        _emit_json(args.repo, surfaces, args.list)
        return 0

    print(f"# AQG Security Review — checklist for {_sanitize_inline(str(args.repo))}\n")
    print(f"surfaces: {', '.join(surfaces)}\n")
    print("Catalogs: OWASP Top 10:2025 / CWE Top 25:2025 current baselines "
          "(static snapshot as of 2026-09-16); cross-check the live catalog for "
          "future releases.\n")
    print("Three-layer position: in-session checklist (this) + semgrep SAST + /audit.\n")

    if args.list in ("owasp", "all"):
        _print_owasp()
    if args.list in ("cwe", "all"):
        _print_cwe()
    if args.list in ("defaults", "all"):
        _print_defaults()
    _print_skeleton(args.repo, surfaces)

    print("Next: walk section 2 (OWASP) -> section 3 (CWE) -> section 4 "
          "(defense layers) -> section 5 (decision) -> section 6 (paste the "
          "closeout block into `.aqg/current_ledger.md`).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
