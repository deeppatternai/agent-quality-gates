#!/usr/bin/env python3
"""Session-mode fingerprint collection for `aqg_doctor --mode session` (Wave 1 P0).

Implemented per ENGINEERING_FRAMEWORK.md §6 (allowlist + desktop-surface checklist).
The companion guards `_surface_redaction.assert_safe_fingerprint` / `check_fingerprint`
form a belt-and-suspenders second layer; this module itself is responsible for
ensuring the returned dict contains only §6 allowlist fields — the first line of defense.

The triple-audit (audit_id 2e5407d6) had 12 accepted findings, all incorporated:
- gpt-5.5 #1: caller (doctor.py) does not directly emit raw violation strings (handled in doctor.py)
- gpt-5.5 #2: caller redacts paths at emit time (handled in doctor.py)
- gpt-5.5 #3: `_cli_version` extracts the first semver-ish token, rejects path/URL
- gpt-5.5 #4: when `probe_cli=False`, *_version / gh_auth take the None path and do not call subprocess
- gpt-5.5 #5: model_default + sandbox explicitly set *_status=warn marked not_probed
- gemini #1: python3 version uses sys.version_info, does not call subprocess
- gemini #2 + o3 #5: `_file_stats` 5MB size guard + chunked hash
- gemini #3: `_json_counts` isinstance guards against len(scalar) TypeError
- gemini #4: claude_home / codex_home derived from *_skills_dir.parent
- o3 #1: caller already refactored the `_run_install_mode` helper first (handled in doctor.py)
- o3 #2: `_gh_auth_summary` strict regex extracts host + scope_count, NEVER username
- o3 #3: `_env_dir_status` docstring + tests codify that it never returns raw path/value

API:
    collect_session_fingerprint(*, aqg_root, claude_skills_dir, codex_skills_dir,
                                  probe_cli=True) -> dict[str, Any]

No third-party dependencies; stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


# ===== Constants =====

# File hash size limit (gemini #2 + o3 #5). Over the limit → sha256_first8=None,
# still store size_bytes (for diagnostics) — do not hash the whole file.
HASH_SIZE_LIMIT_BYTES = 5 * 1024 * 1024  # 5 MiB
HASH_CHUNK_BYTES = 4096

# *_version regex (gpt-5.5 #3): extract a semver-ish substring (not the whole token).
# This way "GNU bash, version 5.2.21(1)-release (...)" still extracts "5.2.21", while
# "git version 2.45.2 /usr/bin/git" still extracts only "2.45.2" — the path part is
# naturally ignored by search and never enters the fingerprint. The pattern only accepts
# digits+dots+optional prerelease segment, and rejects any substring containing
# `/`, `\`, `://`, or whitespace (excluded by the regex itself).
_VERSION_PATTERN = re.compile(r"\b(\d+\.\d+(?:\.\d+){0,2}(?:[-+][\w.]+)?)\b")
_VERSION_MAX_LEN = 64
# Early-reject: first line is too long overall → reject (guards against weird wrappers)
_VERSION_LINE_MAX_LEN = 1024

# gh auth status host extraction regex (o3 #2).
# Typical output: "github.com\n  ✓ Logged in to github.com account alice (...)\n
#                 ✓ Token: ..."  We extract only the hostname, not the username.
_GH_AUTH_HOST_RE = re.compile(r"\b([a-zA-Z0-9](?:[a-zA-Z0-9.\-]{0,126}[a-zA-Z0-9])?)\b")
_GH_AUTH_LOGGED_IN_RE = re.compile(r"Logged in to (\S+)", re.IGNORECASE)
# Token scope line format: "✓ Token scopes: 'repo', 'workflow', 'read:org'"
_GH_AUTH_SCOPES_RE = re.compile(r"Token scopes:\s*([^\n]+)", re.IGNORECASE)

# subprocess default timeout (follows doctor.py)
_DEFAULT_CLI_TIMEOUT = 10

# *_status placeholder for non-probable categories (gpt-5.5 #5).
# The warn status explicitly states "not_probed in Wave 1", so downstream consumers
# can distinguish "not applicable" / "not implemented" / "not probed".
_NOT_PROBED_STATUS = "warn"


# ===== File / JSON helpers =====


def _file_stats(path: Path) -> dict[str, Any]:
    """Safely collect file metadata. NEVER reads file content into memory beyond hash chunks.

    Returns a dict:
        - {"exists": False} (file does not exist)
        - {"exists": True, "size_bytes": int, "sha256_first8": str | None}
          sha256_first8 is None when the file is > HASH_SIZE_LIMIT_BYTES (gemini #2 + o3 #5).

    NEVER returns raw content / path. Any IO exception → {"exists": False}.
    """
    try:
        if not path.is_file():
            return {"exists": False}
        st = path.stat()
        size = int(st.st_size)
        result: dict[str, Any] = {"exists": True, "size_bytes": size}
        if size > HASH_SIZE_LIMIT_BYTES:
            result["sha256_first8"] = None
            return result
        # chunked stream hash to prevent OOM (gemini #2)
        hasher = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(HASH_CHUNK_BYTES)
                if not chunk:
                    break
                hasher.update(chunk)
        result["sha256_first8"] = hasher.hexdigest()[:8]
        return result
    except (OSError, ValueError):
        return {"exists": False}


def _json_counts(path: Path, *, key_to_field: dict[str, str]) -> dict[str, int | None]:
    """Read a JSON file and safely count the child count of top-level keys.

    key_to_field: e.g. {"hooks": "hooks_count", "mcpServers": "mcp_servers_count"}.
    The returned dict always contains all of key_to_field.values(); on parse
    failure / type error / missing file the corresponding value is None
    (gemini #3: guards against len(scalar) TypeError).

    NEVER returns the raw JSON content; only counts.
    """
    out: dict[str, int | None] = {field: None for field in key_to_field.values()}
    try:
        if not path.is_file():
            return out
        # #243 f1: bound parse cost — skip oversized JSON (parallels _file_stats's
        # HASH_SIZE_LIMIT_BYTES guard). An over-limit settings.json returns all-None
        # counts instead of materializing the whole document via json.load.
        if path.stat().st_size > HASH_SIZE_LIMIT_BYTES:
            return out
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return out
        for json_key, field_name in key_to_field.items():
            val = data.get(json_key)
            # triple-audit gemini #3 accepted: scalar / None cannot be len()'d
            if isinstance(val, (dict, list)):
                out[field_name] = len(val)
            elif val is None:
                out[field_name] = 0
            else:
                # scalar type (bool / int / str) → unsure how to count; return None
                out[field_name] = None
    except (OSError, ValueError, json.JSONDecodeError, RecursionError):
        # any exception → all None, do not raise. RecursionError (#243 f2): deeply-nested
        # JSON below the size limit still blows json.load's recursion stack.
        pass
    return out


# ===== Env helpers =====


def _env_dir_status(env_name: str, *, verify_aqg_root: bool = False) -> dict[str, Any]:
    """Return ONLY {"set", "is_directory"[, "verify_root"]} for an env var.

    **CONTRACT (triple-audit o3 #3 accepted)**: this helper never returns the
    env's raw value / actual path string. Downstream consumers only receive a
    boolean + status enum. The docstring + tests enforce this jointly.

    When verify_aqg_root=True, additionally do an AQG checkout sanity check
    (has VERSION file + scripts/ subdir), emitting verify_root: "pass" | "fail".
    """
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        out: dict[str, Any] = {"set": False, "is_directory": False}
        if verify_aqg_root:
            out["verify_root"] = "fail"
        return out
    try:
        path = Path(raw).expanduser()
        is_dir = path.is_dir()
    except (OSError, ValueError, RuntimeError):
        # RuntimeError (#243 f3): Path.expanduser() raises when HOME is unset and
        # the uid has no passwd entry, or for an unknown `~user` — return a clean
        # non-leaking status instead of crashing the manual session fingerprint.
        is_dir = False
        path = None
    out = {"set": True, "is_directory": is_dir}
    if verify_aqg_root:
        ok = (
            is_dir
            and path is not None
            and (path / "VERSION").is_file()
            and (path / "scripts").is_dir()
        )
        out["verify_root"] = "pass" if ok else "fail"
    return out


# ===== CLI version helpers =====


def _python_version() -> str:
    """Python interpreter version. NEVER subprocess (gemini #1 accepted).

    Uses sys.version_info to get the version of the interpreter currently
    running doctor (unlike a `python3 --version` subprocess: that would pick up
    the system python on PATH, which may differ from the actual venv).
    """
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _extract_version_token(raw_first_line: str) -> str | None:
    """Extract first semver-ish substring from CLI version output.

    triple-audit gpt-5.5 #3 accepted: `*_version` strings may contain a path / URL
    that passes the redaction guard (`_surface_redaction` does not reject
    `/` / `\\\\` / `://`). This helper uses the
    regex `\\b(\\d+\\.\\d+(?:\\.\\d+){0,2}(?:[-+]\\w+)?)\\b` to search for the first match —
    the pattern itself rejects `/`, `\\`, `://`, and whitespace, and only accepts
    a digit-led, dot-separated version number.

    Examples:
        "git version 2.45.2"                              -> "2.45.2"
        "gh version 2.81.0 (2025-05-19)"                  -> "2.81.0"
        "GNU bash, version 5.2.21(1)-release (...)"       -> "5.2.21"
        "Python 3.13.0"                                   -> "3.13.0"
        "git version 2.45.2 /usr/bin/git"                 -> "2.45.2"
        "https://example.com 1.0"                         -> "1.0"
        "no digits here only words"                       -> None
        ""                                                -> None
    """
    if not raw_first_line:
        return None
    if len(raw_first_line) > _VERSION_LINE_MAX_LEN:
        return None
    match = _VERSION_PATTERN.search(raw_first_line)
    if not match:
        return None
    candidate = match.group(1)
    if len(candidate) > _VERSION_MAX_LEN:
        return None
    return candidate


def _cli_version(cli_name: str, *, probe_cli: bool, timeout: int = _DEFAULT_CLI_TIMEOUT) -> str | None:
    """Run `<cli> --version`, extract version token via _extract_version_token.

    probe_cli=False (passed through from doctor `--no-cli`) → return None directly,
    do not call subprocess (triple-audit gpt-5.5 #4 accepted: --no-cli must truly
    not invoke any CLI).

    Any IO / parse failure → None, do not raise.
    """
    if not probe_cli:
        return None
    try:
        proc = subprocess.run(
            [cli_name, "--version"],
            text=True,
            errors="replace",  # #243 f4: invalid UTF-8 → U+FFFD, not UnicodeDecodeError crash
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            return None
        first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        return _extract_version_token(first_line)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


# ===== gh auth helper =====


def _gh_auth_summary(*, probe_cli: bool, timeout: int = _DEFAULT_CLI_TIMEOUT) -> dict[str, Any]:
    """Return {"logged_in", "host", "scopes_count"} from `gh auth status`.

    **CONTRACT (triple-audit o3 #2 accepted)**: NEVER includes the GitHub username,
    full hostname-with-path, token, or any other PII. Only:
      - logged_in: bool
      - host: str (only when logged_in; standardized hostname like "github.com")
      - scopes_count: int (only when logged_in; count of comma-separated scopes)

    probe_cli=False → {"logged_in": False} without calling subprocess (triple-audit gpt-5.5 #4).
    NEVER calls `gh auth status --show-token`.
    """
    if not probe_cli:
        return {"logged_in": False}
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            text=True,
            errors="replace",  # #243 f4: invalid UTF-8 → U+FFFD, not UnicodeDecodeError crash
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {"logged_in": False}

    output = proc.stdout or ""
    if proc.returncode != 0:
        return {"logged_in": False}

    # extract host (strict regex; NEVER extract username)
    host_match = _GH_AUTH_LOGGED_IN_RE.search(output)
    if not host_match:
        return {"logged_in": False}
    raw_host = host_match.group(1).strip().rstrip(":")
    # secondary guard: host should not contain '/' or '@' (guards against username@host)
    if "/" in raw_host or "@" in raw_host:
        return {"logged_in": False}
    if not _GH_AUTH_HOST_RE.match(raw_host):
        return {"logged_in": False}

    # extract scopes count
    scopes_count = 0
    scopes_match = _GH_AUTH_SCOPES_RE.search(output)
    if scopes_match:
        raw_scopes = scopes_match.group(1)
        # scopes look like "'repo', 'workflow', 'read:org'"
        # split on comma; filter out empty strings
        scopes_count = sum(1 for s in raw_scopes.split(",") if s.strip())

    return {
        "logged_in": True,
        "host": raw_host,
        "scopes_count": scopes_count,
    }


# ===== PATH helper =====


def _path_dirs_count() -> int:
    """Return number of unique non-empty PATH entries.

    NEVER returns the actual paths.
    """
    raw = os.environ.get("PATH", "")
    if not raw:
        return 0
    parts = [p for p in raw.split(os.pathsep) if p.strip()]
    # de-dup while preserving order: PATH commonly has duplicate entries
    seen: set[str] = set()
    unique_count = 0
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique_count += 1
    return unique_count


# ===== Main API =====


def collect_session_fingerprint(
    *,
    aqg_root: Path | None,
    claude_skills_dir: Path,
    codex_skills_dir: Path,
    probe_cli: bool = True,
) -> dict[str, Any]:
    """Collect a 10-category desktop client surface fingerprint.

    The returned dict strictly conforms to the ENGINEERING_FRAMEWORK.md §6 allowlist.
    The caller should additionally run `_surface_redaction.check_fingerprint(result)`
    for belt-and-suspenders verification.

    Args:
        aqg_root: the AQG checkout already resolved by the current doctor (when
            None, verify_root still fails)
        claude_skills_dir: Claude Code skills directory (default: ~/.claude/skills);
            home is derived as .parent (triple-audit gemini #4 accepted)
        codex_skills_dir: Codex skills directory; home is derived the same way
        probe_cli: False → CLI versions / gh auth skip subprocess (triple-audit gpt-5.5 #4)
    """
    claude_home = claude_skills_dir.parent
    codex_home = codex_skills_dir.parent

    fingerprint: dict[str, Any] = {}

    # === 1. User-level prompt files ===
    fingerprint["claude_md"] = _file_stats(claude_home / "CLAUDE.md")
    fingerprint["codex_agents_md"] = _file_stats(codex_home / "AGENTS.md")

    # === 2. User-level skills dirs ===
    fingerprint["claude_skills_dir"] = _skills_dir_stats(claude_skills_dir)
    fingerprint["codex_skills_dir"] = _skills_dir_stats(codex_skills_dir)

    # === 3. User-level hooks + 4. MCP servers (merged into settings.json) ===
    settings_json = claude_home / "settings.json"
    settings_stats = _file_stats(settings_json)
    if settings_stats.get("exists"):
        counts = _json_counts(
            settings_json,
            key_to_field={
                "hooks": "hooks_count",
                "permissions": "permissions_count",
                "mcpServers": "mcp_servers_count",
            },
        )
        settings_stats.update(counts)
    else:
        settings_stats["hooks_count"] = None
        settings_stats["permissions_count"] = None
        settings_stats["mcp_servers_count"] = None
    fingerprint["claude_settings_json"] = settings_stats

    # === 5. Environment variables ===
    fingerprint["env_AQG_ROOT"] = _env_dir_status("AQG_ROOT", verify_aqg_root=True)
    fingerprint["env_XDG_DATA_HOME"] = _env_dir_status("XDG_DATA_HOME")
    fingerprint["env_CODEX_HOME"] = _env_dir_status("CODEX_HOME")

    # === 6. CLI versions ===
    fingerprint["python3_version"] = _python_version()  # NEVER subprocess (gemini #1)
    fingerprint["git_version"] = _cli_version("git", probe_cli=probe_cli)
    fingerprint["gh_version"] = _cli_version("gh", probe_cli=probe_cli)
    fingerprint["bash_version"] = _cli_version("bash", probe_cli=probe_cli)

    # === 7. Model default (not_probed Wave 1; gpt-5.5 #5 accepted) ===
    fingerprint["model_default_status"] = _NOT_PROBED_STATUS

    # === 8. Permissions / sandbox (not_probed Wave 1; gpt-5.5 #5 accepted) ===
    fingerprint["sandbox_status"] = _NOT_PROBED_STATUS

    # === 9. PATH ===
    fingerprint["path_dirs_count"] = _path_dirs_count()

    # === 10. Auth state ===
    fingerprint["gh_auth"] = _gh_auth_summary(probe_cli=probe_cli)

    return fingerprint


def _skills_dir_stats(skills_dir: Path) -> dict[str, Any]:
    """{"exists": bool, "skills_count": int} for a skills install root.

    skills_count = number of child entries under that directory (subdirs + symlinks).
    NEVER lists names; only count.
    """
    try:
        if not skills_dir.is_dir():
            return {"exists": False, "skills_count": 0}
        # include dir + symlink; exclude hidden (starting with .)
        count = sum(1 for entry in skills_dir.iterdir() if not entry.name.startswith("."))
        return {"exists": True, "skills_count": count}
    except OSError:
        return {"exists": False, "skills_count": 0}


# ===== Self-test =====


def self_test() -> int:
    """Quick sanity self-test (full unit coverage in tests/test_session_fingerprint.py)."""
    import tempfile

    # _python_version does not call subprocess
    v = _python_version()
    assert v.count(".") == 2, f"python version format: {v}"

    # _path_dirs_count > 0 (PATH env is present)
    assert _path_dirs_count() >= 0

    # _file_stats on a missing file
    fs = _file_stats(Path("/nonexistent/path/x"))
    assert fs == {"exists": False}, fs

    # _file_stats on a real file (create a small tmp)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as t:
        t.write("hello")
        tmp_path = Path(t.name)
    try:
        fs = _file_stats(tmp_path)
        assert fs["exists"] is True
        assert fs["size_bytes"] == 5
        assert fs["sha256_first8"] == hashlib.sha256(b"hello").hexdigest()[:8]
    finally:
        tmp_path.unlink()

    # _extract_version_token
    assert _extract_version_token("git version 2.45.2") == "2.45.2"
    assert _extract_version_token("Python 3.13.0") == "3.13.0"
    assert _extract_version_token("git version 2.45.2 /usr/bin/git") == "2.45.2"
    assert _extract_version_token("https://example.com 1.0") == "1.0"
    assert _extract_version_token("no digits here only words") is None
    assert _extract_version_token("") is None

    # _env_dir_status NEVER contains the raw value
    os.environ["__SESSION_FP_TEST__"] = "/tmp/secret/path/should/not/leak"
    try:
        result = _env_dir_status("__SESSION_FP_TEST__")
        for v_ in result.values():
            assert "/tmp/secret/path" not in str(v_), f"raw path leak in: {result}"
    finally:
        del os.environ["__SESSION_FP_TEST__"]

    # collect_session_fingerprint does not crash
    fp = collect_session_fingerprint(
        aqg_root=Path(__file__).resolve().parent.parent,
        claude_skills_dir=Path.home() / ".claude" / "skills",
        codex_skills_dir=Path.home() / ".codex" / "skills",
        probe_cli=False,  # skip subprocess to speed up self-test
    )
    # probe_cli=False → each CLI version is None
    assert fp["git_version"] is None
    assert fp["gh_version"] is None
    assert fp["bash_version"] is None
    assert fp["python3_version"] is not None  # still runs (uses sys.version_info)
    assert fp["gh_auth"] == {"logged_in": False}
    # not_probed status
    assert fp["model_default_status"] == _NOT_PROBED_STATUS
    assert fp["sandbox_status"] == _NOT_PROBED_STATUS
    # path_dirs_count is an int
    assert isinstance(fp["path_dirs_count"], int)

    # run the _surface_redaction guard — fingerprint must be safe
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _surface_redaction import check_fingerprint

    redaction = check_fingerprint(fp)
    assert redaction.is_safe, f"self-test fingerprint failed redaction: {redaction.violations}"

    print("OK: _session_fingerprint self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
