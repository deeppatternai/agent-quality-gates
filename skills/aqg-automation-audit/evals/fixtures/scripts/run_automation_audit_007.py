#!/usr/bin/env python3
"""Hermetic redaction and plugin-liveness harness."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
INVENTORY = ROOT / "skills/aqg-automation-audit/scripts/inventory.py"
OVERLAP = ROOT / "skills/aqg-automation-audit/scripts/overlap_check.py"
OUT = Path.cwd() / "aqg_automation_audit_boundary/007-redaction-liveness"
CANARY = "ghp_" + "FAKE" * 9


def sha_tree(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aqg-audit-007-") as tmp:
        root = Path(tmp) / "host"
        (root / "plugins/cache/mp/live/2.0/hooks").mkdir(parents=True)
        (root / "plugins/cache/mp/stale/1.0/hooks").mkdir(parents=True)
        (root / "plugins/cache/mp/disabled/1.0/hooks").mkdir(parents=True)
        (root / "plugins/installed_plugins.json").parent.mkdir(parents=True, exist_ok=True)
        settings = {
            "enabledPlugins": {"live@mp": True, "stale@mp": True},
            "env": {"AQG_ROOT": "/tmp/aqg", "TOKEN": CANARY},
        }
        (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        (root / "plugins/installed_plugins.json").write_text(
            json.dumps({"plugins": {
                "live@mp": [{"version": "2.0", "installPath": str(root / "plugins/cache/mp/live/2.0")}],
                "stale@mp": [{"version": "2.0", "installPath": str(root / "plugins/cache/mp/stale/1.0")}],
                "disabled@mp": [{"version": "1.0", "installPath": str(root / "plugins/cache/mp/disabled/1.0")}],
            }}),
            encoding="utf-8",
        )
        hooks = [
            (root / "plugins/cache/mp/live/2.0/hooks/hooks.json", "live", "Write", f"echo {CANARY}"),
            (root / "plugins/cache/mp/stale/1.0/hooks/hooks.json", "stale", "Bash", "stale-hook"),
            (root / "plugins/cache/mp/disabled/1.0/hooks/hooks.json", "disabled", "Edit", "disabled-hook"),
        ]
        for path, _, matcher, command in hooks:
            path.write_text(json.dumps({"hooks": {"PreToolUse": [
                {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}
            ]}}), encoding="utf-8")
        before = sha_tree(root)
        env = {**os.environ, "AQG_SKIP_MCP": "1"}
        inv = subprocess.run(
            [sys.executable, str(INVENTORY), "--skill-root", str(root), "--json"],
            text=True, capture_output=True, env=env, check=False,
        )
        inventory_path = Path(tmp) / "inventory.json"
        inventory_path.write_text(inv.stdout, encoding="utf-8")
        findings = subprocess.run(
            [sys.executable, str(OVERLAP), "--inventory", str(inventory_path), "--json"],
            text=True, capture_output=True, env=env, check=False,
        )
        after = sha_tree(root)
        (OUT / "inventory.json").write_text(inv.stdout, encoding="utf-8")
        (OUT / "findings.json").write_text(findings.stdout, encoding="utf-8")
        (OUT / "inventory.exit").write_text(str(inv.returncode) + "\n", encoding="utf-8")
        (OUT / "findings.exit").write_text(str(findings.returncode) + "\n", encoding="utf-8")
        (OUT / "snapshot.before").write_text(before + "\n", encoding="utf-8")
        (OUT / "snapshot.after").write_text(after + "\n", encoding="utf-8")
        (OUT / "commands.log").write_text(
            f"AQG_SKIP_MCP=1 python3 {INVENTORY} --skill-root <temp> --json\n"
            f"python3 {OVERLAP} --inventory - --json\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
