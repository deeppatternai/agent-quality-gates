"""#243 — _session_fingerprint manual-only DX robustness regression (HE-01~04).

Re-audit `70e8d19e` (gpt-5.5) reconstructed the lost HE-01~04 findings. Each
test FAILS against the pre-fix scripts/_session_fingerprint.py and passes after.
Severity ceiling: manual-only DX (`doctor --mode session` is the only caller; no
CI workflow), so the fixes are non-crash / non-hang hardening, not security.

CI-gated via tests/behavior/ (the top-level tests/test_session_fingerprint.py is
NOT in any workflow's run list — see behavior-tests.yml).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _session_fingerprint as sf  # noqa: E402


# --- f1: _json_counts must bound parse cost (size guard like _file_stats) ------

def test_f1_json_counts_oversized_skips_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "HASH_SIZE_LIMIT_BYTES", 50)
    big = tmp_path / "settings.json"
    big.write_text('{"hooks": [' + ",".join(["0"] * 200) + "]}", encoding="utf-8")
    assert big.stat().st_size > 50  # over the (patched) limit
    r = sf._json_counts(big, key_to_field={"hooks": "hooks_count"})
    # pre-fix: json.load parses the whole doc → hooks_count == 200 (unbounded);
    # post-fix: oversized → None counts (no full parse).
    assert r["hooks_count"] is None


# --- f2: _json_counts must not crash on deeply-nested JSON (RecursionError) ----

def test_f2_json_counts_deeply_nested_no_crash(tmp_path):
    deep = tmp_path / "settings.json"
    deep.write_text("[" * 5000 + "]" * 5000, encoding="utf-8")  # < size limit
    # pre-fix: json.load raises RecursionError (not in the except tuple) → crash;
    # post-fix: caught → None counts.
    r = sf._json_counts(deep, key_to_field={"hooks": "hooks_count"})
    assert r["hooks_count"] is None


# --- f3: _env_dir_status must not crash on ~unknownuser / missing HOME ---------

def test_f3_env_dir_status_unknown_user_no_crash(monkeypatch):
    monkeypatch.setenv("__FP_E243__", "~nonexistentuser999000/x")
    # pre-fix: Path.expanduser() raises RuntimeError (only OSError/ValueError
    # caught) → crash; post-fix: caught → set True, is_directory False, no leak.
    r = sf._env_dir_status("__FP_E243__")
    assert r["set"] is True
    assert r["is_directory"] is False


# --- f4: _cli_version must not crash on invalid-UTF8 CLI output ----------------

def test_f4_cli_version_invalid_bytes_no_crash(tmp_path):
    fake = tmp_path / "fakecli"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'sys.stdout.buffer.write(bytes([255, 254]) + b" version 1.2.3\\n")\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    # pre-fix: subprocess.run(text=True) decode raises UnicodeDecodeError
    # (a ValueError subclass, NOT in the FileNotFoundError/TimeoutExpired/OSError
    # except) → crash; post-fix (errors="replace"): no crash.
    r = sf._cli_version(str(fake), probe_cli=True)
    assert r is None or isinstance(r, str)
