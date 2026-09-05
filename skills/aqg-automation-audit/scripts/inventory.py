#!/usr/bin/env python3
"""Capture deterministic automation inventory: hooks / MCP / plugins / skills / env.

Source paths (deterministic, never LLM-inferred):
  - hooks:   $skill_root/settings.json `hooks` field + plugin cache hooks.json
  - MCP:     settings.json `enabledPlugins` + best-effort `claude mcp list`
  - plugins: $skill_root/plugins/installed_plugins.json
  - skills:  $skill_root/skills/ + plugin cache <m>/<p>/<v>/skills/
  - env:     settings.json `env` field — KEYS ONLY (values redacted to placeholder)

Default `skill_root` is `~/.claude`; `--skill-root <path>` overrides for tests.

Output: JSON to stdout (or file via `--out <path>`).

Exit codes:
  0: success — inventory captured
  1: reserved — surfaces missing pieces in output, does not raise
  2: usage error — argparse propagates exit 2 on bad args
  3: reserved — config or schema error (e.g. settings.json malformed)
  70: reserved — internal error placeholder
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REDACTED = "<redacted>"


# ---------------------------------------------------------------------------
# Secret redaction (C1, audit 2b171207). The skill premise is "keys only, never
# a raw secret value". Hook command strings (user + plugin), MCP `raw`/`stderr`,
# and plugin install-path tails are free text that routinely carry tokens. Every
# such field is passed through _redact() AT CAPTURE TIME so the emitted JSON never
# carries a token. overlap_check reads the already-redacted inventory and thus
# inherits the redaction (command_hint / rule cannot re-expose a stripped token).
#
# Distinctive-prefix patterns + a URL-userinfo strip only — NOT a broad catch-all,
# so ordinary commands (e.g. `python3 .../hook.py`) pass through unchanged.
# ---------------------------------------------------------------------------

# Each pattern matches a *secret-shaped* substring; the matched span is replaced
# with REDACTED. Order does not matter (patterns are applied in sequence; later
# ones operate on the partially-redacted string). Generic key=value / bearer
# rules capture only the value portion via a group so the surrounding text stays.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # URL userinfo: scheme://user:pass@host -> scheme://<redacted>@host
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    # AWS access key id (distinctive prefix + 16 base32 chars)
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # GitHub PAT / OAuth / app tokens
    re.compile(r"\bgh[posur]_[A-Za-z0-9_]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # OpenAI keys (sk- / sk-proj- / sk-ant-)
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}\b"),
    # Slack tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # Google API key
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    # Bearer tokens (case-insensitive keyword, keep the keyword)
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]{12,}"),
    # Generic token / api key / secret / password assignment (keep key + sep)
    re.compile(
        r"(?i)((?:token|api[_-]?key|secret|password)\s*[=:]\s*)\S{8,}"
    ),
)


def _redact(text: str) -> str:
    """Replace secret-shaped substrings in free text with REDACTED.

    Conservative by design: matches distinctive token prefixes + URL userinfo +
    key=value assignments only. Ordinary commands are returned unchanged. Always
    returns a string (non-str input is coerced via str())."""
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    out = text
    # URL userinfo (index 0): keep scheme + host, drop credentials.
    out = _SECRET_PATTERNS[0].sub("://" + REDACTED + "@", out)
    # AWS / GitHub / OpenAI / Slack / Google (indices 1-6): whole-token replacement.
    for pat in _SECRET_PATTERNS[1:7]:
        out = pat.sub(REDACTED, out)
    # Bearer + generic assignment (indices 7-8): keep keyword group, redact value.
    for pat in _SECRET_PATTERNS[7:]:
        out = pat.sub(lambda m: m.group(1) + REDACTED, out)
    return out


# Allowlist of env keys whose derived state (not raw value) is safe to emit.
# Each lambda receives the raw value (string or None) and returns a coarse state
# label that overlap_check can classify on. NEVER returns the raw value for
# secret-bearing keys (token / api key etc. → "set"|"empty" only).
ENV_STATE_ALLOWLIST = {
    "AQG_ROOT":                  lambda v: "set" if (v or "").strip() else "empty",
    "AQG_METRICS":               lambda v: "set" if (v or "").strip() else "empty",
    "SEMGREP_APP_TOKEN":         lambda v: "set" if (v or "").strip() else "empty",
    "SLASH_COMMAND_TOOL_CHAR_BUDGET": lambda v: f"set:{(v or '').strip()}" if (v or "").strip().isdigit() else "empty",
}


# Module-level parse-error tracker. _read_json + _schema_error append here;
# main() promotes a non-empty tracker to exit 3.
_PARSE_ERRORS: list[str] = []


def _schema_error(where: str, detail: str) -> None:
    """Record a valid-JSON-wrong-shape error (C2). Promoted to exit 3 by main()."""
    _PARSE_ERRORS.append(f"{where}: schema error: {detail}")


def _redact_user_hooks(user_hooks: Any) -> dict[str, Any]:
    """Return a redacted COPY of the settings.json `hooks` mapping (C1 + C2).

    Hook command strings are free text that can carry tokens, so each command is
    passed through _redact(). Wrong-shaped nodes are skipped (type-guarded) and
    recorded as schema errors so main() fails closed. Immutable: builds new dicts,
    never mutates the parsed input."""
    if not isinstance(user_hooks, dict):
        if user_hooks:  # a present-but-wrong-shape value is a schema error
            _schema_error("settings.json:hooks", f"expected object, got {type(user_hooks).__name__}")
        return {}
    out: dict[str, Any] = {}
    for hook_type, entries in user_hooks.items():
        if not isinstance(entries, list):
            _schema_error(f"settings.json:hooks.{hook_type}",
                          f"expected list, got {type(entries).__name__}")
            continue
        new_entries: list[Any] = []
        for e in entries:
            if not isinstance(e, dict):
                _schema_error(f"settings.json:hooks.{hook_type}[]",
                              f"expected object, got {type(e).__name__}")
                continue
            new_e = dict(e)
            inner = e.get("hooks")
            if isinstance(inner, list):
                new_inner: list[Any] = []
                for h in inner:
                    if isinstance(h, dict) and "command" in h:
                        nh = dict(h)
                        nh["command"] = _redact(str(nh.get("command", "")))
                        new_inner.append(nh)
                    else:
                        new_inner.append(h)
                new_e["hooks"] = new_inner
            new_entries.append(new_e)
        out[hook_type] = new_entries
    return out


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _PARSE_ERRORS.append(f"{path}: {exc}")
        return {}
    except OSError as exc:
        # C2: PermissionError / read failure must fail-closed (exit 3), not crash.
        _PARSE_ERRORS.append(f"{path}: {exc}")
        return {}


def _capture_settings(skill_root: Path) -> dict[str, Any]:
    """Read settings.json. env values redacted (keys only) except for
    ENV_STATE_ALLOWLIST keys, which emit derived state (e.g. on/off, set/empty)
    so overlap_check can classify env-controlled hooks. Raw secret values are
    never emitted."""
    settings = _read_json(skill_root / "settings.json")
    if not isinstance(settings, dict):
        # C2: valid JSON, wrong top-level shape (e.g. a list) — fail closed.
        _schema_error("settings.json", f"expected object, got {type(settings).__name__}")
        settings = {}
    env = settings.get("env") or {}
    if not isinstance(env, dict):
        # C2: env as a list/str crashes .items()/.keys() — guard + fail closed.
        _schema_error("settings.json:env", f"expected object, got {type(env).__name__}")
        env = {}
    enabled = settings.get("enabledPlugins") or {}
    if not isinstance(enabled, dict):
        _schema_error("settings.json:enabledPlugins",
                      f"expected object, got {type(enabled).__name__}")
        enabled = {}
    env_states: dict[str, str] = {}
    for k, v in env.items():
        if k in ENV_STATE_ALLOWLIST:
            try:
                env_states[k] = ENV_STATE_ALLOWLIST[k](v)
            except Exception:  # noqa: BLE001
                env_states[k] = "unknown"
    return {
        "enabled_plugins": enabled,
        "user_hooks": _redact_user_hooks(settings.get("hooks")),  # C1: commands redacted
        "env_keys": sorted(env.keys()),
        "env_count": len(env),
        "env_states": env_states,  # derived states for allowlisted keys (no raw values)
    }


def _capture_installed_plugins(skill_root: Path) -> dict[str, Any]:
    """Read installed_plugins.json. Each plugin: key + installPath + version."""
    data = _read_json(skill_root / "plugins" / "installed_plugins.json")
    if not isinstance(data, dict):
        _schema_error("installed_plugins.json", f"expected object, got {type(data).__name__}")
        return {"plugins": [], "count": 0}
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        _schema_error("installed_plugins.json:plugins",
                      f"expected object, got {type(plugins).__name__}")
        return {"plugins": [], "count": 0}
    out: list[dict[str, Any]] = []
    for plugin_key, entries in plugins.items():
        if not isinstance(entries, list) or not entries:
            # C2: entries must be a non-empty list; index [0] otherwise crashes.
            if entries:
                _schema_error(f"installed_plugins.json:plugins.{plugin_key}",
                              f"expected list, got {type(entries).__name__}")
            continue
        e = entries[0]
        if not isinstance(e, dict):
            # C2: a non-dict entry crashes .get(); skip + record.
            _schema_error(f"installed_plugins.json:plugins.{plugin_key}[0]",
                          f"expected object, got {type(e).__name__}")
            continue
        out.append({
            "plugin_key": plugin_key,
            # C1: install path can embed credentials in rare cases — redact the tail.
            "install_path": _redact(str(e.get("installPath", ""))),
            "version": e.get("version", ""),
            "scope": e.get("scope", ""),
        })
    return {"plugins": out, "count": len(out)}


def _capture_plugin_hooks(skill_root: Path) -> list[dict[str, Any]]:
    """Glob plugin cache hooks.json files; flatten to (plugin/hook_type/matcher/command) tuples."""
    hooks_root = skill_root / "plugins" / "cache"
    if not hooks_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for hp in hooks_root.glob("*/*/*/hooks/hooks.json"):
        # Path: cache/<marketplace>/<plugin>/<version>/hooks/hooks.json
        parts = hp.relative_to(hooks_root).parts
        marketplace = parts[0] if len(parts) > 0 else "?"
        plugin = parts[1] if len(parts) > 1 else "?"
        version = parts[2] if len(parts) > 2 else "?"
        data = _read_json(hp)
        if not isinstance(data, dict):
            _schema_error(f"{hp}", f"expected object, got {type(data).__name__}")
            continue
        hooks_node = data.get("hooks") or {}
        if not isinstance(hooks_node, dict):
            _schema_error(f"{hp}:hooks", f"expected object, got {type(hooks_node).__name__}")
            continue
        for hook_type, entries in hooks_node.items():
            if not isinstance(entries, list):
                continue
            for e in entries:
                if not isinstance(e, dict):
                    continue  # C2: skip non-dict hook entry
                matcher = e.get("matcher", "")
                inner = e.get("hooks", []) or []
                if not isinstance(inner, list):
                    continue
                for h in inner:
                    if not isinstance(h, dict):
                        continue
                    out.append({
                        "marketplace": marketplace,
                        "plugin": plugin,
                        "version": version,
                        "hook_type": hook_type,
                        "matcher": matcher if isinstance(matcher, str) else str(matcher),
                        # C1: plugin hook command can carry tokens — redact at capture.
                        "command": _redact(str(h.get("command", ""))),
                        "type": h.get("type", ""),
                    })
    return out


def _capture_skills(skill_root: Path) -> dict[str, Any]:
    """Find SKILL.md files in user skill dir + plugin cache. Track .disabled-by-eaf suffix."""
    home_skills = skill_root / "skills"
    cache_skills_root = skill_root / "plugins" / "cache"
    user_skills: list[dict[str, Any]] = []
    plugin_skills: list[dict[str, Any]] = []
    disabled_skills: list[dict[str, Any]] = []

    # User-installed (~/.claude/skills/<skill>/SKILL.md)
    if home_skills.is_dir():
        for skill_dir in home_skills.iterdir():
            if not skill_dir.is_dir():
                continue
            name = skill_dir.name
            disabled = name.endswith(".disabled-by-eaf")
            real_name = name[: -len(".disabled-by-eaf")] if disabled else name
            entry = {
                "name": real_name,
                "source": "user",
                "path": str(skill_dir),
                "disabled": disabled,
                "description": _extract_description(skill_dir / "SKILL.md"),
            }
            (disabled_skills if disabled else user_skills).append(entry)

    # Plugin-bundled (cache/<m>/<p>/<v>/skills/<skill>/SKILL.md)
    if cache_skills_root.is_dir():
        for skill_md in cache_skills_root.glob("*/*/*/skills/*/SKILL.md"):
            parts = skill_md.relative_to(cache_skills_root).parts
            if len(parts) < 5:
                continue
            marketplace, plugin, version, _, skill_dir_name = parts[:5]
            disabled = skill_dir_name.endswith(".disabled-by-eaf")
            real_name = skill_dir_name[: -len(".disabled-by-eaf")] if disabled else skill_dir_name
            entry = {
                "name": real_name,
                "source": "plugin",
                "marketplace": marketplace,
                "plugin": plugin,
                "version": version,
                "path": str(skill_md.parent),
                "disabled": disabled,
                "description": _extract_description(skill_md),
            }
            (disabled_skills if disabled else plugin_skills).append(entry)

    return {
        "user": user_skills,
        "plugin": plugin_skills,
        "disabled": disabled_skills,
        "counts": {
            "user": len(user_skills),
            "plugin": len(plugin_skills),
            "disabled": len(disabled_skills),
        },
    }


_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


# C3: descriptions run 300-500 chars; overlap_check analyses the full text, so
# capture up to 1000 chars (was 200, which dropped reserved phrases past char 200).
_DESC_CAPTURE_LIMIT = 1000


def _extract_description(skill_md: Path) -> str:
    """Read frontmatter description; capture up to _DESC_CAPTURE_LIMIT chars.

    The cap is generous (1000) because overlap_check must see the full description
    to match reserved phrases anywhere in it; any *display* truncation is a separate
    concern handled by the renderer, not here."""
    if not skill_md.is_file():
        return ""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Frontmatter is between two --- markers
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end < 0:
        return ""
    front = text[3:end]
    m = _DESC_RE.search(front)
    if not m:
        return ""
    return m.group(1)[:_DESC_CAPTURE_LIMIT]


def _capture_mcp(skill_root: Path) -> dict[str, Any]:
    """MCP via `claude mcp list`. Best-effort — may fail if claude CLI absent.

    Set AQG_SKIP_MCP=1 to bypass the subprocess call (useful in self-tests and
    smoke-test runs where spawning `claude mcp list` is too slow or unavailable).
    """
    if os.environ.get("AQG_SKIP_MCP") == "1":
        return {"available": False, "reason": "AQG_SKIP_MCP=1", "entries": []}
    cli = shutil.which("claude")
    if not cli:
        return {"available": False, "reason": "claude CLI not on PATH", "entries": []}
    env = os.environ.copy()
    env.setdefault("CLAUDE_PROJECT_DIR", str(skill_root))
    try:
        proc = subprocess.run(
            [cli, "mcp", "list"],
            capture_output=True, text=True, timeout=10, env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"available": False, "reason": str(exc), "entries": []}
    if proc.returncode != 0:
        # C1: stderr can echo a connection URL with userinfo or a bearer token.
        return {"available": False, "reason": f"exit={proc.returncode}",
                "stderr": _redact(proc.stderr.strip()), "entries": []}
    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Parse "<name>: <transport> <url> - <status>" or simpler shapes
        if ":" in line:
            name, _, rest = line.partition(":")
            # C1: `raw` is the transport/url/status tail — redact userinfo/tokens.
            entries.append({"name": name.strip(), "raw": _redact(rest.strip())})
    return {"available": True, "entries": entries, "count": len(entries)}


def _build_inventory(skill_root: Path) -> dict[str, Any]:
    settings = _capture_settings(skill_root)
    plugins = _capture_installed_plugins(skill_root)
    plugin_hooks = _capture_plugin_hooks(skill_root)
    skills = _capture_skills(skill_root)
    mcp = _capture_mcp(skill_root)
    return {
        "schema_version": 1,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "skill_root": str(skill_root),
        "settings": settings,
        "plugins": plugins,
        "hooks": {
            "user": settings.get("user_hooks", {}),
            "plugin": plugin_hooks,
            "plugin_count": len(plugin_hooks),
        },
        "skills": skills,
        "mcp": mcp,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="inventory.py",
        description="Capture automation stack inventory (hooks/MCP/plugins/skills/env). "
                    "Read-only: writes only to stdout. Caller redirects to file (e.g. "
                    "`> .aqg/automation_audit_<date>.json`) — script never opens output paths.",
    )
    p.add_argument("--skill-root", type=Path,
                   default=Path(os.path.expanduser("~/.claude")),
                   help="root of skill installation; defaults to ~/.claude")
    p.add_argument("--json", action="store_true",
                   help="emit JSON (default behavior; flag retained for explicit invocation)")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    skill_root = args.skill_root.expanduser().resolve()
    _PARSE_ERRORS.clear()  # fresh tracker per main() call (test reuse)
    inventory = _build_inventory(skill_root)
    if _PARSE_ERRORS:
        # Surface parse errors to inventory output AND set non-zero exit per cli_contract exit 3.
        inventory["_parse_errors"] = list(_PARSE_ERRORS)
        for err in _PARSE_ERRORS:
            print(f"error: malformed JSON source: {err}", file=sys.stderr)
    payload = json.dumps(inventory, indent=2, ensure_ascii=False, default=str)
    print(payload)
    return 3 if _PARSE_ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
