#!/usr/bin/env python3
"""Detect AQG-workflow overlaps in an inventory.json (from `inventory.py`).

Overlap rules (deterministic; expand as new conflict patterns surface):
  - PreToolUse hook on Bash/Edit/Write     → conflicts aqg-code-construction (severity: blocking)
  - PostToolUse hook on Edit/Write         → conflicts aqg-evidence-closeout (severity: noise|blocking)
  - UserPromptSubmit hook injecting text   → noise candidate (system-reminder pollution)
  - SessionStart hook with same banner     → duplicate initialization (severity: noise|cosmetic)
  - plugin-hook liveness cross-join        → disabled / stale-cache plugin hooks are downgraded
    to cosmetic (only enabled + installed-current versions are live conflicts; fail-safe keeps a
    hook live when enable/version data is missing, so a real conflict is never silenced)
  - skill description containing reserved  → AQG 6-skill suite routing collision
    keywords (verification/debug/audit/checklist/closeout)
  - env missing AQG_ROOT                    → AQG cannot run from this host
  - env missing AQG_METRICS                 → recommended (metrics ledger off); not blocking

Output: markdown findings table to stdout (default) or JSON via --json.

Exit codes:
  0: success — findings table emitted (even if empty)
  1: reserved — never raises, surfaces findings in output
  2: usage error — argparse propagates exit 2 on bad args
  3: config / schema error — inventory.json malformed
  70: reserved — internal error placeholder
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def _matcher_tokens(matcher: str) -> set[str]:
    """Split a hooks.json matcher string into tokens.

    Matchers are Claude Code regex-style:
      - "Edit"        -> {"Edit"}
      - "Edit|Write"  -> {"Edit", "Write"}
      - "*"           -> {"*"}  (caller treats specially)
      - ""            -> {""}    (empty matcher)
    """
    if not matcher:
        return {""}
    return {p.strip() for p in re.split(r"[|,]", matcher) if p.strip()}


# ============================================================================
# Plugin-hook liveness cross-join (state-aware filtering)
# ============================================================================
#
# A plugin hook is scanned from the cache (cache/<m>/<p>/<v>/hooks/hooks.json),
# but its plugin may be DISABLED (absent from settings.enabledPlugins) or the
# cache dir may be a STALE leftover of a superseded version. Such hooks cannot
# fire, so reporting them as live conflicts is a false positive. We cross-join
# each hook with enabledPlugins + the installed-current version to classify it.
#
# Fail-safe: only POSITIVE evidence downgrades a hook. If enabledPlugins is empty
# (no data) we never infer "disabled"; if the installed-current version is unknown
# we never infer "stale". Missing data keeps a hook live so a genuine conflict is
# never silenced — the skill errs toward over-reporting, not under-reporting.


def _installed_versions(plugins_node: dict[str, Any]) -> dict[str, str]:
    """Map `<plugin>@<marketplace>` -> installed-current version, from the
    inventory `plugins.plugins[]` snapshot. Wrong shapes are skipped (C2), and
    an entry with a FALSY version is skipped entirely: an empty / absent version
    is "unknown", NOT a known-empty version. Storing "" here would later read as
    a concrete current version that makes every real cache version look stale —
    the exact fail-safe hole the liveness check must avoid."""
    out: dict[str, str] = {}
    entries = plugins_node.get("plugins")
    if not isinstance(entries, list):
        return out
    for p in entries:
        if isinstance(p, dict) and p.get("plugin_key") and p.get("version"):
            out[p["plugin_key"]] = p["version"]
    return out


def _plugin_hook_liveness(
    hook: dict[str, Any],
    enabled_plugins: dict[str, Any],
    installed_versions: dict[str, str],
) -> tuple[str, str]:
    """Classify a plugin hook as live / disabled / stale (see section header).

    Returns (state, detail). `detail` is empty for live hooks and a short human
    reason for disabled/stale ones. Disabled is checked before stale (a disabled
    plugin's version is moot).

    Every downgrade requires *complete, positive* evidence — missing/partial data
    keeps the hook live:
      - incomplete plugin identity (no plugin/marketplace) → live, never disabled
        (the gap is in the caller's data, not proof the hook is off);
      - a falsy installed-current OR hook version → live, never stale (only two
        concrete, differing versions prove a superseded cache dir)."""
    plugin = hook.get("plugin")
    marketplace = hook.get("marketplace")
    if not plugin or not marketplace:
        return ("live", "")  # incomplete identity is not positive evidence
    key = f"{plugin}@{marketplace}"
    # Enabled check — only when enabledPlugins data is actually present.
    if enabled_plugins and not enabled_plugins.get(key):
        return ("disabled", f"plugin '{key}' not in enabledPlugins")
    # Stale-version check — only when BOTH versions are concrete (a falsy value
    # on either side = unknown = keep live).
    current = installed_versions.get(key)
    version = hook.get("version")
    if current and version and version != current:
        return ("stale", f"cache version {version!r} superseded by installed {current!r}")
    return ("live", "")


def _with_liveness(finding: dict[str, Any], liveness: str, detail: str) -> dict[str, Any]:
    """Return a NEW finding annotated with liveness (immutable — no mutation).

    Live hooks keep their severity. Disabled/stale hooks are downgraded to
    `cosmetic` with the rule suffixed, so a reader sees the hook exists in cache
    but cannot fire (not a live conflict)."""
    if liveness == "live":
        return {**finding, "liveness": "live"}
    return {
        **finding,
        "severity": "cosmetic",
        "liveness": liveness,
        "rule": f"{finding.get('rule', '')} — NOT LIVE ({liveness}: {detail})",
    }


# ============================================================================
# Overlap rules
# ============================================================================


# Skill description keywords that overlap AQG's 6-skill routing. Any plugin skill
# whose description contains these (and is not itself an aqg-* skill) is flagged.
# NB: keys must be reserved PHRASES, not bare base words (SKILL.md §3). A bare
# word like "verification" over-fires (e.g. "email verification tool"), so the
# key is the phrase "verification routing". "closeout" is kept as a bare key —
# SKILL.md explicitly sanctions it as a reserved AQG term.
AQG_ROUTING_KEYWORDS = {
    "verification routing": "aqg-evidence-closeout (verification routing)",
    "verify before": "aqg-evidence-closeout",
    "evidence-back": "aqg-evidence-closeout",
    "debug workflow": "aqg-systematic-debugging",
    "debugging session": "aqg-systematic-debugging",
    "audit adjudication": "aqg-audit-adjudication",
    "audit findings": "aqg-audit-adjudication",
    "checklist before commit": "aqg-code-construction",
    "closeout": "aqg-evidence-closeout",
    "fact-forcing": "aqg-code-construction",  # construction-gate signature
}

# AQG-required env vars; absence of any flags broken installation.
AQG_REQUIRED_ENV = ["AQG_ROOT"]
# AQG-recommended env vars; absence flags incomplete config (not blocking).
AQG_RECOMMENDED_ENV = ["AQG_METRICS"]

# Hook matchers that overlap AQG workflows.
PRETOOL_BAD_MATCHERS = {"Bash", "Edit", "Write", "MultiEdit"}
POSTTOOL_BAD_MATCHERS = {"Edit", "Write", "MultiEdit"}


def _check_user_hooks(user_hooks: dict[str, Any]) -> list[dict[str, Any]]:
    """User-level hooks (settings.json). Currently informational only —
    user explicitly added these. Plugin hooks are the noise/blocking source."""
    findings: list[dict[str, Any]] = []
    for hook_type, entries in (user_hooks or {}).items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):  # C2: skip non-dict hook entry
                continue
            findings.append({
                "category": "user_hook",
                "item": f"settings.json:{hook_type}",
                "matcher": e.get("matcher", "*"),
                "severity": "cosmetic",
                "overlap_with": "user-installed",
                "rule": "user-level hook (informational)",
            })
    return findings


def _check_session_start_duplicates(
    plugin_hooks: list[dict[str, Any]],
    enabled_plugins: dict[str, Any] | None = None,
    installed_versions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Detect SessionStart hooks with identical command base across plugins.

    Two plugins emitting the same SessionStart banner / context init creates
    duplicate noise. Group by first-60-char command base; flag groups with ≥2
    distinct (plugin, version) sources. Only LIVE hooks are grouped — a stale
    cache version of the SAME plugin must not count as a second source, else one
    plugin's old+new cache dirs self-report a phantom cross-plugin duplicate.
    """
    enabled_plugins = enabled_plugins if isinstance(enabled_plugins, dict) else {}
    installed_versions = installed_versions if isinstance(installed_versions, dict) else {}
    findings: list[dict[str, Any]] = []
    by_cmd_base: dict[str, list[dict[str, Any]]] = {}
    for h in plugin_hooks:
        if not isinstance(h, dict):  # C2: skip non-dict plugin-hook element
            continue
        if h.get("hook_type") != "SessionStart":
            continue
        # Only live hooks are real sources; skip disabled/stale-cache versions.
        liveness, _ = _plugin_hook_liveness(h, enabled_plugins, installed_versions)
        if liveness != "live":
            continue
        cmd = (h.get("command") or "").strip()
        if not cmd:
            continue
        cmd_base = cmd[:60]
        by_cmd_base.setdefault(cmd_base, []).append(h)
    for cmd_base, hooks in by_cmd_base.items():
        sources = sorted({(h.get("plugin", "?"), h.get("version", "?")) for h in hooks})
        if len(sources) >= 2:
            plugins_str = ", ".join(f"{p}@{v}" for p, v in sources)
            findings.append({
                "category": "plugin_hook",
                "item": f"SessionStart:duplicate({len(sources)})",
                "matcher": "*",
                "severity": "noise",
                "overlap_with": "session-init-duplication",
                "rule": f"SessionStart command shared across plugins: {plugins_str}",
                "command_hint": cmd_base[:120],
                "liveness": "live",  # only live hooks reach here (stale/disabled filtered above)
            })
    return findings


def _check_plugin_hooks(
    plugin_hooks: list[dict[str, Any]],
    enabled_plugins: dict[str, Any] | None = None,
    installed_versions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    enabled_plugins = enabled_plugins if isinstance(enabled_plugins, dict) else {}
    installed_versions = installed_versions if isinstance(installed_versions, dict) else {}
    findings: list[dict[str, Any]] = []
    for h in plugin_hooks:
        if not isinstance(h, dict):  # C2: skip non-dict plugin-hook element
            continue
        plugin = h.get("plugin", "?")
        version = h.get("version", "?")
        ht = h.get("hook_type", "")
        matcher = h.get("matcher", "")
        cmd = h.get("command", "")
        item = f"{plugin}@{version}:{ht}"
        tokens = _matcher_tokens(matcher)
        # Wildcard matcher ("*") matches everything — treat as conflict against any AQG matcher set
        wildcard = "*" in tokens
        # Cross-join with enabledPlugins + installed-current version: a disabled or
        # stale-cache hook is downgraded by _with_liveness (not a live conflict).
        liveness, detail = _plugin_hook_liveness(h, enabled_plugins, installed_versions)
        if ht == "PreToolUse" and (wildcard or tokens & PRETOOL_BAD_MATCHERS):
            hit = "*" if wildcard else ", ".join(sorted(tokens & PRETOOL_BAD_MATCHERS))
            findings.append(_with_liveness({
                "category": "plugin_hook",
                "item": item,
                "matcher": matcher,
                "severity": "blocking",
                "overlap_with": "aqg-code-construction",
                "rule": f"PreToolUse:{hit} can intercept code-construction flow",
                "command_hint": cmd[:120],
            }, liveness, detail))
        elif ht == "PostToolUse" and (wildcard or tokens & POSTTOOL_BAD_MATCHERS):
            hit = "*" if wildcard else ", ".join(sorted(tokens & POSTTOOL_BAD_MATCHERS))
            findings.append(_with_liveness({
                "category": "plugin_hook",
                "item": item,
                "matcher": matcher,
                "severity": "noise",
                "overlap_with": "aqg-evidence-closeout",
                "rule": f"PostToolUse:{hit} runs after every edit (noise unless useful)",
                "command_hint": cmd[:120],
            }, liveness, detail))
        elif ht == "UserPromptSubmit":
            findings.append(_with_liveness({
                "category": "plugin_hook",
                "item": item,
                "matcher": matcher or "*",
                "severity": "noise",
                "overlap_with": "session-context-budget",
                "rule": "UserPromptSubmit injects text on every turn — noise candidate",
                "command_hint": cmd[:120],
            }, liveness, detail))
    return findings


def _check_skill_descriptions(skills: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for source_key in ("plugin",):  # only plugin skills can clash with AQG routing
        entries = skills.get(source_key)
        if not isinstance(entries, list):  # C2: wrong-shaped node must not crash
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "")
            desc = (entry.get("description") or "").lower()
            if name.startswith("aqg-"):
                continue  # AQG own skills are not "overlap"
            for kw, layer in AQG_ROUTING_KEYWORDS.items():
                if kw in desc:
                    findings.append({
                        "category": "skill_description",
                        "item": f"{entry.get('marketplace', '?')}:{name}",
                        "severity": "silent",
                        "overlap_with": layer,
                        "rule": f"description contains '{kw}' — may steal routing",
                    })
                    break  # one finding per skill is enough
    return findings


def _check_env(env_keys: list[str],
               env_states: dict[str, str] | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    present = set(env_keys or [])
    for k in AQG_REQUIRED_ENV:
        if k not in present:
            findings.append({
                "category": "env",
                "item": k,
                "severity": "blocking",
                "overlap_with": "aqg-startup-preflight",
                "rule": f"required env '{k}' missing — AQG cannot resolve root",
            })
    for k in AQG_RECOMMENDED_ENV:
        if k not in present:
            findings.append({
                "category": "env",
                "item": k,
                "severity": "noise",
                "overlap_with": f"capability gated by {k}",
                "rule": f"recommended env '{k}' missing — capability degraded but not blocking",
            })
    return findings


def _check_disabled(skills: dict[str, Any]) -> list[dict[str, Any]]:
    """Surface already-disabled skills (verdict: already handled, no action)."""
    findings: list[dict[str, Any]] = []
    disabled = skills.get("disabled")
    if not isinstance(disabled, list):  # C2: wrong-shaped node must not crash
        return findings
    for entry in disabled:
        if not isinstance(entry, dict):
            continue
        findings.append({
            "category": "disabled_skill",
            "item": f"{entry.get('marketplace', entry.get('source', '?'))}:{entry.get('name', '?')}",
            "severity": "cosmetic",
            "overlap_with": "previously disabled",
            "rule": "skill already disabled (.disabled-by-eaf suffix)",
        })
    return findings


def _check_mcp(mcp: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not mcp.get("available", False):
        findings.append({
            "category": "mcp",
            "item": "<all>",
            "severity": "noise",
            "overlap_with": "claude mcp list",
            "rule": f"MCP inventory unavailable: {mcp.get('reason', '?')}",
        })
        return findings
    entries = mcp.get("entries")
    if not isinstance(entries, list):  # C2: wrong-shaped node must not crash
        return findings
    for e in entries:
        if not isinstance(e, dict):
            continue
        raw = (e.get("raw") or "").lower()
        if "fail" in raw or "error" in raw or "pending" in raw or "oauth" in raw:
            findings.append({
                "category": "mcp",
                "item": e.get("name", "?"),
                "severity": "blocking",
                "overlap_with": "MCP transport",
                "rule": f"MCP not healthy: {e.get('raw', '?')[:100]}",
            })
    return findings


def _as_dict(value: Any) -> dict[str, Any]:
    """C2: coerce an inventory sub-node to a dict (wrong shapes -> {})."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """C2: coerce an inventory sub-node to a list (wrong shapes -> [])."""
    return value if isinstance(value, list) else []


def analyze(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    # C2: a non-dict inventory (or wrong-shaped sub-nodes) must not crash analyze.
    inventory = _as_dict(inventory)
    findings: list[dict[str, Any]] = []
    hooks = _as_dict(inventory.get("hooks"))
    plugin_hooks = _as_list(hooks.get("plugin"))
    skills = _as_dict(inventory.get("skills"))
    settings = _as_dict(inventory.get("settings"))
    # Liveness cross-join context for plugin hooks: enabledPlugins (which plugins
    # are on) + installed-current version per plugin. A stale-cache or disabled
    # plugin's hook is downgraded instead of reported as a live conflict.
    enabled_plugins = _as_dict(settings.get("enabled_plugins"))
    installed_versions = _installed_versions(_as_dict(inventory.get("plugins")))
    findings.extend(_check_user_hooks(_as_dict(hooks.get("user"))))
    findings.extend(_check_plugin_hooks(plugin_hooks, enabled_plugins, installed_versions))
    findings.extend(_check_session_start_duplicates(plugin_hooks, enabled_plugins, installed_versions))
    findings.extend(_check_skill_descriptions(skills))
    findings.extend(_check_env(_as_list(settings.get("env_keys")),
                               _as_dict(settings.get("env_states"))))
    findings.extend(_check_disabled(skills))
    findings.extend(_check_mcp(_as_dict(inventory.get("mcp"))))
    return findings


# ============================================================================
# Output rendering
# ============================================================================


SEVERITY_RANK = {"blocking": 0, "noise": 1, "silent": 2, "cosmetic": 3}


def _escape_md_cell(s: str) -> str:
    """Escape pipe and newline so cell content does not corrupt the table."""
    if s is None:
        return "-"
    return str(s).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _render_markdown(findings: list[dict[str, Any]]) -> str:
    findings_sorted = sorted(findings, key=lambda f: (
        SEVERITY_RANK.get(f.get("severity", "cosmetic"), 9),
        f.get("category", ""),
        f.get("item", ""),
    ))
    lines = [
        "# Automation overlap findings",
        "",
        f"_total: {len(findings_sorted)} finding(s)_",
        "",
        "| severity | category | item | overlap with | rule |",
        "|---|---|---|---|---|",
    ]
    for f in findings_sorted:
        lines.append(
            f"| {_escape_md_cell(f.get('severity', '?'))} "
            f"| {_escape_md_cell(f.get('category', '?'))} "
            f"| {_escape_md_cell(f.get('item', '?'))} "
            f"| {_escape_md_cell(f.get('overlap_with', '-'))} "
            f"| {_escape_md_cell(f.get('rule', '-'))} |"
        )
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="overlap_check.py",
        description="Detect AQG-workflow overlaps in an inventory.json.",
    )
    p.add_argument("--inventory", type=Path, required=True,
                   help="path to inventory JSON from inventory.py")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of markdown")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    if not args.inventory.is_file():
        print(f"error: inventory not found: {args.inventory}", file=sys.stderr)
        return 3
    try:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: malformed inventory JSON: {exc}", file=sys.stderr)
        return 3
    findings = analyze(inventory)
    if args.json:
        print(json.dumps({"findings": findings, "count": len(findings)}, indent=2,
                         ensure_ascii=False))
    else:
        print(_render_markdown(findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
