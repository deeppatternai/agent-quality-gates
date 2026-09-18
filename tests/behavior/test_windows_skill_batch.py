from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
INSTALLERS = (
    (REPO / "scripts" / "install.sh", ("--no-hooks",)),
    (
        REPO / "agent-packs" / "claude-code" / "install.sh",
        ("--mode", "link", "--no-hooks"),
    ),
)


def _bash() -> str:
    bash = shutil.which("bash")
    if bash:
        return bash
    pytest.skip("bash is unavailable")


@pytest.mark.parametrize("installer,extra", INSTALLERS)
def test_real_shell_chain_batches_all_skills_in_one_python_process(
    installer: Path, extra: tuple[str, ...], tmp_path: Path
) -> None:
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    log = tmp_path / "python calls.log"
    shim = fake_bin / "python3"
    shim.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$AQG_TEST_PYTHON_LOG"\n'
        'exec "$AQG_TEST_REAL_PYTHON" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home ; '中文"
    destination = home / "skill routes"
    env = os.environ.copy()
    env.update(
        {
            "AQG_SKIP_HOOK_PROMPT": "1",
            "AQG_TEST_PYTHON_LOG": str(log),
            "AQG_TEST_REAL_PYTHON": sys.executable,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
        }
    )
    command = [_bash(), str(installer), *extra, "--dest", str(destination)]

    first = subprocess.run(
        command, cwd=REPO, env=env, text=True, capture_output=True, timeout=120
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert "batch installed 16 skills" in first.stdout
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if "aqg_skill_install.py" in line]
    assert len(calls) == 1, calls
    assert calls[0].count("--item") == 16, calls[0]
    before = {path.name: os.lstat(path).st_ino for path in destination.iterdir()}

    second = subprocess.run(
        command, cwd=REPO, env=env, text=True, capture_output=True, timeout=120
    )
    assert second.returncode == 0, second.stdout + second.stderr
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if "aqg_skill_install.py" in line]
    assert len(calls) == 2, calls
    assert {path.name: os.lstat(path).st_ino for path in destination.iterdir()} == before


def test_installer_does_not_search_for_powershell_or_cmd() -> None:
    source = (REPO / "scripts" / "aqg_skill_install.py").read_text(encoding="utf-8")
    assert "shutil.which" not in source
    assert "powershell" not in source.lower()
    assert "cmd.exe" not in source.lower()
