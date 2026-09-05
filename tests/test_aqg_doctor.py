#!/usr/bin/env python3
"""Regression tests for aqg_doctor.py."""

from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def run_doctor(args: list[str], env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Strip GitHub Actions noise
    for key in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_EVENT_PATH"):
        env.pop(key, None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "scripts/aqg_doctor.py", *args],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class AqgDoctorTest(unittest.TestCase):
    def test_text_output_uses_windows_console_safe_status_markers(self) -> None:
        sys.path.insert(0, str(REPO / "scripts"))
        try:
            import aqg_doctor

            output_bytes = io.BytesIO()
            windows_stdout = io.TextIOWrapper(output_bytes, encoding="gbk")
            with mock.patch.object(aqg_doctor.sys, "stdout", windows_stdout):
                aqg_doctor.emit_text(
                    [aqg_doctor.CheckResult("PASS", "fixture", "present")]
                )
                windows_stdout.flush()
            output = output_bytes.getvalue().decode("gbk")
            self.assertIn("[+] PASS fixture: present", output)
        finally:
            sys.path.remove(str(REPO / "scripts"))

    def test_real_repo_passes(self) -> None:
        proc = run_doctor(["--no-cli", "--json"])
        self.assertIn(proc.returncode, (0, 1), proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        # The repo itself should PASS: VERSION + key scripts are present
        names = {r["name"]: r["status"] for r in payload["results"]}
        self.assertEqual(names.get("aqg_root"), "PASS", payload)
        self.assertEqual(names.get("version"), "PASS", payload)
        self.assertEqual(names.get("script:scripts/run_quality_gates.py"), "PASS")
        self.assertEqual(names.get("script:scripts/_secret_patterns.py"), "PASS")

    def test_pyyaml_check_present_and_passes(self) -> None:
        """doctor registers a PyYAML health check; the test env has PyYAML installed → PASS."""
        proc = run_doctor(["--no-cli", "--json"])
        payload = json.loads(proc.stdout)
        names = {r["name"]: r["status"] for r in payload["results"]}
        self.assertIn("pyyaml", names, "doctor must register a pyyaml health check")
        self.assertEqual(names["pyyaml"], "PASS", payload)

    def test_pyyaml_check_warns_when_missing(self) -> None:
        """Missing PyYAML → WARN (not FAIL: the skill files themselves don't need it,
        only the validator/YAML parsing does); fix points to requirements.txt (single SOT)."""
        import importlib
        sys.path.insert(0, str(REPO / "scripts"))
        try:
            doctor = importlib.import_module("aqg_doctor")
        finally:
            sys.path.pop(0)
        # sys.modules["yaml"]=None makes the in-function `import yaml` raise ImportError
        with mock.patch.dict(sys.modules, {"yaml": None}):
            result = doctor.check_pyyaml()
        self.assertEqual(result.status, "WARN")
        self.assertEqual(result.name, "pyyaml")
        self.assertIn("requirements.txt", result.fix or "")
        self.assertNotEqual(result.status, "FAIL")  # missing PyYAML is WARN, never FAIL

    def test_pyyaml_check_warns_on_old_version(self) -> None:
        """PyYAML installed but below the 6.0 declared in requirements.txt → WARN
        (doctor validates the manifest minimum version; audit fc86aa91 f1)."""
        import importlib
        import types
        sys.path.insert(0, str(REPO / "scripts"))
        try:
            doctor = importlib.import_module("aqg_doctor")
        finally:
            sys.path.pop(0)
        fake_yaml = types.SimpleNamespace(__version__="5.4.1")
        with mock.patch.dict(sys.modules, {"yaml": fake_yaml}):
            result = doctor.check_pyyaml()
        self.assertEqual(result.status, "WARN")
        self.assertIn("older", result.detail.lower())
        self.assertIn("requirements.txt", result.fix or "")
        # boundary: 6.0 exactly + 6.0.3 must PASS (not flagged old)
        for ok in ("6.0", "6.0.3", "6.1"):
            with mock.patch.dict(sys.modules, {"yaml": types.SimpleNamespace(__version__=ok)}):
                self.assertEqual(doctor.check_pyyaml().status, "PASS", ok)

    def test_bad_aqg_root_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            proc = run_doctor(
                ["--no-cli", "--json"],
                env_overrides={"AQG_ROOT": tmp_s},
            )
            payload = json.loads(proc.stdout)
            names = {r["name"]: r["status"] for r in payload["results"]}
            # The temp directory has no VERSION and no scripts → multiple FAIL
            self.assertEqual(names.get("version"), "FAIL", payload)
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(payload["ok"])

    def test_missing_aqg_root_env_does_not_fallback_to_doctor_location(self) -> None:
        # P1: If AQG_ROOT is explicitly set but wrong, doctor must report that
        # fact instead of silently falling back to the checkout containing
        # aqg_doctor.py. 0.1.4 finding #8: detail now contains only path + a short
        # source tag; the "not a directory" info moved into source to avoid stuttering.
        with tempfile.TemporaryDirectory() as tmp_s:
            missing = str(Path(tmp_s) / "missing-aqg-root")
            proc = run_doctor(
                ["--no-cli", "--json"],
                env_overrides={"AQG_ROOT": missing},
            )
            payload = json.loads(proc.stdout)
            names = {r["name"]: r for r in payload["results"]}
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(names["aqg_root"]["status"], "FAIL", payload)
            # detail contains path + source tag (e.g. "/tmp/x/missing-aqg-root (AQG_ROOT env (not a directory))")
            self.assertIn(missing, names["aqg_root"]["detail"])
            self.assertIn("not a directory", names["aqg_root"]["detail"])

    def test_missing_skills_dirs_warn_not_fail(self) -> None:
        # Non-existent skills dirs should WARN ("if not installed it shouldn't FAIL; this machine may use only one agent")
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            proc = run_doctor(
                [
                    "--no-cli",
                    "--json",
                    "--claude-skills-dir",
                    str(tmp / "no-claude"),
                    "--codex-skills-dir",
                    str(tmp / "no-codex"),
                ]
            )
            payload = json.loads(proc.stdout)
            statuses = [r["status"] for r in payload["results"] if r["name"].endswith("_root")]
            # claude_skill_root and codex_skill_root should both be WARN (not FAIL)
            warn_roots = [s for s in statuses if s == "WARN"]
            self.assertEqual(len(warn_roots), 2, statuses)

    def test_python_version_fix_hint_uses_min_python_constant(self) -> None:
        # 0.1.4 finding #9: the fix hint must use MIN_PYTHON rather than a hardcoded
        # string, to prevent the hint from drifting when MIN_PYTHON is bumped (e.g. 3.9 → 3.11).
        # Use patch.object to temporarily push MIN_PYTHON to an unreachable high version, triggering the FAIL path.
        from unittest.mock import patch

        sys.path.insert(0, str(REPO / "scripts"))
        try:
            import aqg_doctor

            # Push MIN_PYTHON to (99, 99) so any real Python will FAIL
            with patch.object(aqg_doctor, "MIN_PYTHON", (99, 99)):
                result = aqg_doctor.check_python_version()
            self.assertEqual(result.status, "FAIL")
            self.assertIsNotNone(result.fix)
            self.assertIn("Python 99.99+", result.fix or "")
            # detail should also reflect the patched MIN_PYTHON
            self.assertIn("99.99", result.detail)

            # Also verify: without patching, the fix hint uses the string form of the real MIN_PYTHON
            real_min = aqg_doctor._python_version_string(aqg_doctor.MIN_PYTHON)
            with patch.object(aqg_doctor.sys, "version_info", (0, 0, 0, "final", 0)):
                result = aqg_doctor.check_python_version()
            self.assertEqual(result.status, "FAIL")
            self.assertIn(f"Python {real_min}+", result.fix or "")
        finally:
            if str(REPO / "scripts") in sys.path:
                sys.path.remove(str(REPO / "scripts"))

    def test_readme_install_command_invokes_doctor_via_python3(self) -> None:
        # 0.1.6 finding: if the install flow directly execve's `"$repo/scripts/aqg_doctor.py"`,
        # it requires the doctor file to have +x permission. +x can be lost across different
        # filesystems / git core.fileMode (observed "Permission denied" on one machine). Guard:
        # every quoted "$.../scripts/aqg_doctor.py" invocation must be prefixed with `python3 `,
        # never a bare execve.
        #
        # The README "one command" install now delegates to scripts/install_aqg.sh, which runs
        # the doctor internally — the README no longer invokes the doctor directly. So the +x
        # guard now applies to install_aqg.sh (the real invocation site), and stays a regression
        # guard on the README (must never re-introduce a bare-execve doctor call).
        import re

        readme = (REPO / "README.md").read_text(encoding="utf-8")
        install_sh = (REPO / "scripts" / "install_aqg.sh").read_text(encoding="utf-8")

        # A quoted, variable-prefixed path to the doctor:
        #   "$VAR/scripts/aqg_doctor.py"  or  "${VAR}/scripts/aqg_doctor.py"
        # Scope note: this guard is deliberately pinned to the project's canonical,
        # always-double-quoted invocation form (`python3 "$VAR/scripts/aqg_doctor.py"`).
        # It does NOT model arbitrary shell (unquoted paths, `python3 -u`, an absolute
        # interpreter, or prose mentions): broadening the positive and the `(?<!python3 )`
        # negative in lockstep would either flag benign doc prose or contradict each
        # other. If the invocation *shape* ever changes, this test fails loudly (RED) and
        # the guard should be updated with it. Do not re-add a README positive for the
        # doctor path — the delegation to install_aqg.sh is intentional.
        doctor_call = r'"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?/scripts/aqg_doctor\.py"'

        # positive: the install script must invoke the doctor via `python3 <doctor>`.
        self.assertRegex(
            install_sh,
            r"python3 " + doctor_call,
            'install_aqg.sh must invoke the doctor via `python3 "$AQG_ROOT/scripts/aqg_doctor.py"`',
        )

        # negative: no bare-execve doctor call (not prefixed by `python3 `) in either the
        # README one-liner or the install script.
        bare_pattern = re.compile(r"(?<!python3 )" + doctor_call)
        for name, text in (("README.md", readme), ("scripts/install_aqg.sh", install_sh)):
            bare_matches = bare_pattern.findall(text)
            self.assertEqual(
                bare_matches,
                [],
                f"bare execve call to doctor still present in {name}: {bare_matches}; "
                f"must use the `python3 ...` form to avoid a +x dependency",
            )

    def test_readme_cat_commands_wrap_in_bash_lc(self) -> None:
        # 0.1.7 finding (zhoupeng observed a PowerShell error): if a cat command in the README
        # contains bash variable-substitution syntax (old `${XDG_DATA_HOME:-$HOME/.local/share}`,
        # now `$HOME/.deeppattern/...`), PowerShell won't expand `$HOME` / `${VAR:-...}`
        # → the path resolves to an empty string → No such file or directory.
        # Guard: any cat command line with such a `$`-variable path must be wrapped in `bash -lc '...'`,
        # so PowerShell can also paste it directly (bash -lc hands the command to bash to interpret).
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for line in readme.splitlines():
            stripped = line.strip()
            # Command-line signature: contains cat + a `$`-variable path (XDG template or $HOME/.deeppattern)
            if "cat " in stripped and (
                "${XDG_DATA_HOME" in stripped or "$HOME/.deeppattern" in stripped
            ):
                self.assertIn(
                    "bash -lc '",
                    stripped,
                    f"cat command with a `$`-variable path must be wrapped in `bash -lc '...'` "
                    f"(PowerShell compatible): {stripped[:120]}",
                )

    def test_json_shape(self) -> None:
        proc = run_doctor(["--no-cli", "--json"])
        payload = json.loads(proc.stdout)
        self.assertIn("ok", payload)
        self.assertIn("results", payload)
        self.assertIsInstance(payload["results"], list)
        for entry in payload["results"]:
            self.assertIn(entry["status"], {"PASS", "WARN", "FAIL"})
            self.assertIn("name", entry)
            self.assertIn("detail", entry)


if __name__ == "__main__":
    unittest.main()
