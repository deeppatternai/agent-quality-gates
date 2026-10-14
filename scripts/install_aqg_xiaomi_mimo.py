#!/usr/bin/env python3
"""Install AQG user-scope skills and rules for Xiaomi MiMo."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import install_aqg_rules
from scripts import install_aqg_work_clients


CLIENT_ID = "xiaomi-mimo"
REPO_ROOT = Path(__file__).absolute().parent.parent


class InstallError(RuntimeError):
    pass


def _absolute_path(name: str, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallError(f"{name} must be absolute: {value}")
    return path


def resolve_mimo_config_root(value: str | None, home: str | None) -> Path:
    if value:
        return _absolute_path("--config-root", value)
    mimocode_home = os.environ.get("MIMOCODE_HOME")
    if mimocode_home:
        return _absolute_path("MIMOCODE_HOME", mimocode_home) / "config"
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return _absolute_path("XDG_CONFIG_HOME", xdg_config_home) / "mimocode"
    base = Path(home).expanduser().absolute() if home else Path.home()
    return base / ".config" / "mimocode"


def _action(args: argparse.Namespace) -> str:
    for name in ("apply", "verify", "uninstall", "is_installed"):
        if getattr(args, name):
            return name
    raise AssertionError("argparse requires one action")


def _work_args(args: argparse.Namespace, config_root: Path, action: str) -> list[str]:
    result = [
        "--client",
        CLIENT_ID,
        "--scope",
        "user",
        "--client-root",
        str(config_root),
        "--aqg-root",
        str(args.aqg_root),
        "--mode",
        args.mode,
        f"--{action.replace('_', '-')}",
    ]
    if args.strict_link:
        result.append("--strict-link")
    if args.no_hooks:
        result.append("--no-hooks")
    return result


def _rules_args(args: argparse.Namespace, config_root: Path, action: str) -> list[str]:
    return [
        "--client",
        CLIENT_ID,
        "--client-root",
        str(config_root),
        "--aqg-root",
        str(args.aqg_root),
        f"--{action.replace('_', '-')}",
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--is-installed", action="store_true")
    parser.add_argument("--aqg-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--config-root")
    parser.add_argument("--home", help="HOME override used only when config env vars are absent")
    parser.add_argument("--mode", choices=("link", "copy"), default="link")
    parser.add_argument("--strict-link", action="store_true")
    parser.add_argument("--no-hooks", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config_root = resolve_mimo_config_root(args.config_root, args.home)
        action = _action(args)
        if action == "uninstall":
            rules_rc = install_aqg_rules.main(_rules_args(args, config_root, action))
            skills_rc = install_aqg_work_clients.main(
                _work_args(args, config_root, action)
            )
            return 0 if rules_rc == 0 and skills_rc == 0 else 1

        skills_rc = install_aqg_work_clients.main(
            _work_args(args, config_root, action)
        )
        if skills_rc != 0:
            return 1
        rules_rc = install_aqg_rules.main(_rules_args(args, config_root, action))
        return 0 if rules_rc == 0 else 1
    except Exception as exc:  # aqg: top-level boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
