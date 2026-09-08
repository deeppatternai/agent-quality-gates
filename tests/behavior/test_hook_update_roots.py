"""Persisted hook commands follow a root swap in every installer family.

The client processes are not emulated: execute their generated command with a
harmless adapter probe, proving both the executable and --aqg-root move forward.
Codex's separate digest/trust behavior is covered by test_reinstall_auto_update.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.aqg_update.stage import _replace_root_link

REPO = Path(__file__).resolve().parents[2]
CASES = [
    ("cursor", "install_cursor_support.py", "scripts/cursor_aqg_hook.py"),
    *[(client, "install_aqg_work_clients.py", "scripts/cursor_aqg_hook.py")
      for client in ("codebuddy", "workbuddy-ai", "kimi-code", "qoderwork")],
    *[(client, "install_aqg_qoder.py", "agent-packs/qoder/hooks/qoder_hook_adapter.py")
      for client in ("qoder", "qoder-cn", "qoder-cli", "qoder-cli-cn")],
    *[(client, "install_aqg_agent_clients.py", "scripts/agent_client_aqg_hook.py")
      for client in ("trae", "trae-cn", "devin")],
    ("pi", "install_aqg_pi.py", "scripts/pi_aqg_hook.py"),
]


@pytest.mark.parametrize("client,installer,adapter", CASES)
def test_saved_hook_command_follows_generation_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    client: str, installer: str, adapter: str,
) -> None:
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    versions = [tmp_path / "versions" / name for name in ("one", "two")]
    for version in versions:
        (version / "scripts").mkdir(parents=True)
        (version / "skills").mkdir()
        (version / "VERSION").write_text(version.name, encoding="utf-8")
        shutil.copyfile(REPO / "scripts" / installer, version / "scripts" / installer)
        probe = version / adapter
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(
            "import json, sys\nfrom pathlib import Path\n"
            "root = Path(sys.argv[sys.argv.index('--aqg-root') + 1])\n"
            f"print(json.dumps({{'adapter': {version.name!r}, "
            "'root': (root / 'VERSION').read_text(encoding='utf-8')}))\n",
            encoding="utf-8",
        )
    root = tmp_path / "AQG entrance"
    root.symlink_to(versions[0], target_is_directory=True)
    spec = importlib.util.spec_from_file_location(
        "_aqg_hook_swap_installer", root / "scripts" / installer,
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    selected = module._resolve_aqg_root(str(root)) if hasattr(module, "_resolve_aqg_root") else module.REPO_ROOT
    hook = "pretooluse_secret_scan.sh"
    if client == "cursor":
        command = module._hook_specs()["preToolUse"]["command"]
    elif installer == "install_aqg_work_clients.py":
        command = module._quoted_command("PreToolUse", selected)
    elif installer == "install_aqg_qoder.py":
        command = module._hook_command(selected, hook)
    elif client == "pi":
        command = module._command_argv(selected, hook)
    else:
        command = module._hook_command(selected, client, hook)

    def execute():
        if installer == "install_aqg_qoder.py" and os.name == "nt":
            from scripts.run_aqg_codex_hook import _find_bash
            argv, shell = [str(_find_bash()), "-c", command], False
        else:
            argv, shell = command, isinstance(command, str)
        completed = subprocess.run(
            argv, shell=shell, cwd=tmp_path, text=True, encoding="utf-8",
            capture_output=True, timeout=15,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    assert execute() == {"adapter": "one", "root": "one"}
    next_link = tmp_path / "next"
    next_link.symlink_to(versions[1], target_is_directory=True)
    _replace_root_link(str(next_link), root)
    # Execute the SAME saved command; a new renderer could hide the old pin.
    assert execute() == {"adapter": "two", "root": "two"}
