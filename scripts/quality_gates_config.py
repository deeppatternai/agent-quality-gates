#!/usr/bin/env python3
"""Load and validate Agent Quality Gates JSON config v1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_INTERNAL = 70

CONFIG_VERSION = 1
ALLOWED_MODES = {"warn", "blocking", "off"}
ALLOWED_ROOT_KEYS = {"version", "project", "gates", "boundaries"}
ALLOWED_GATES = ("audit_adjudication", "evidence_closeout", "worktree", "required_context")
ALLOWED_BOUNDARIES = ("production", "secrets", "raw_private_data")

GATE_FIELDS = {
    "audit_adjudication": {"enabled", "mode", "audit_skip_allowed"},
    "evidence_closeout": {"enabled", "mode"},
    "worktree": {"enabled", "mode", "ci_mode"},
    "required_context": {"enabled", "mode"},
}


class ConfigError(ValueError):
    """Raised when a config is malformed or unsupported."""


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is list:
        return "array"
    if type(value) is dict:
        return "object"
    return type(value).__name__


def require_object(value: Any, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ConfigError(f"{path} must be an object, got {json_type(value)}")
    return value


def require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{path} must be a bool, got {json_type(value)}")
    return value


def require_string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ConfigError(f"{path} must be a string, got {json_type(value)}")
    if not allow_empty and not value.strip():
        raise ConfigError(f"{path} must not be empty")
    return value


def require_string_list(value: Any, path: str) -> list[str]:
    if type(value) is not list:
        raise ConfigError(f"{path} must be an array of strings, got {json_type(value)}")
    result: list[str] = []
    for idx, item in enumerate(value):
        result.append(require_string(item, f"{path}[{idx}]"))
    return result


def reject_unknown_keys(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{path} has unknown key(s): {', '.join(unknown)}")


def normalize_project(value: Any) -> dict[str, Any]:
    project = require_object(value, "project")
    reject_unknown_keys(project, {"name", "required_context_files"}, "project")
    if "name" not in project:
        raise ConfigError("project.name is required")
    if "required_context_files" not in project:
        raise ConfigError("project.required_context_files is required")
    return {
        "name": require_string(project["name"], "project.name"),
        "required_context_files": require_string_list(
            project["required_context_files"],
            "project.required_context_files",
        ),
    }


def normalize_mode(value: Any, path: str) -> str:
    mode = require_string(value, path)
    if mode not in ALLOWED_MODES:
        raise ConfigError(f"{path} must be one of: {', '.join(sorted(ALLOWED_MODES))}")
    return mode


def normalize_gate(name: str, value: Any) -> dict[str, Any]:
    gate = require_object(value, f"gates.{name}")
    reject_unknown_keys(gate, GATE_FIELDS[name], f"gates.{name}")
    if "enabled" not in gate:
        raise ConfigError(f"gates.{name}.enabled is required")
    if "mode" not in gate:
        raise ConfigError(f"gates.{name}.mode is required")

    enabled = require_bool(gate["enabled"], f"gates.{name}.enabled")
    mode = normalize_mode(gate["mode"], f"gates.{name}.mode")
    if enabled and mode == "off":
        raise ConfigError(f"gates.{name}.mode cannot be off when enabled is true")
    if not enabled and mode != "off":
        raise ConfigError(f"gates.{name}.mode must be off when enabled is false")

    normalized: dict[str, Any] = {"enabled": enabled, "mode": mode}
    if name == "audit_adjudication":
        normalized["audit_skip_allowed"] = require_string_list(
            gate.get("audit_skip_allowed", []),
            f"gates.{name}.audit_skip_allowed",
        )
    elif name == "worktree":
        normalized["ci_mode"] = require_bool(
            gate.get("ci_mode", False),
            f"gates.{name}.ci_mode",
        )
    return normalized


def normalize_gates(value: Any) -> dict[str, Any]:
    gates = require_object(value, "gates")
    unknown = sorted(set(gates) - set(ALLOWED_GATES))
    if unknown:
        raise ConfigError(f"gates has unknown gate name(s): {', '.join(unknown)}")

    normalized: dict[str, Any] = {
        "audit_adjudication": {
            "enabled": False,
            "mode": "off",
            "audit_skip_allowed": [],
        },
        "evidence_closeout": {"enabled": False, "mode": "off"},
        "worktree": {"enabled": False, "mode": "off", "ci_mode": False},
        "required_context": {"enabled": False, "mode": "off"},
    }
    for name, gate in gates.items():
        normalized[name] = normalize_gate(name, gate)
    return normalized


def normalize_boundaries(value: Any) -> dict[str, Any]:
    boundaries = require_object(value, "boundaries")
    unknown = sorted(set(boundaries) - set(ALLOWED_BOUNDARIES))
    if unknown:
        raise ConfigError(f"boundaries has unknown key(s): {', '.join(unknown)}")
    missing = [name for name in ALLOWED_BOUNDARIES if name not in boundaries]
    if missing:
        raise ConfigError(f"boundaries missing required key(s): {', '.join(missing)}")

    normalized: dict[str, Any] = {}
    for name in ALLOWED_BOUNDARIES:
        item = require_object(boundaries[name], f"boundaries.{name}")
        reject_unknown_keys(
            item,
            {"require_explicit_statement"},
            f"boundaries.{name}",
        )
        if "require_explicit_statement" not in item:
            raise ConfigError(f"boundaries.{name}.require_explicit_statement is required")
        normalized[name] = {
            "require_explicit_statement": require_bool(
                item["require_explicit_statement"],
                f"boundaries.{name}.require_explicit_statement",
            )
        }
    return normalized


def normalize_config(raw: Any) -> dict[str, Any]:
    config = require_object(raw, "config")
    reject_unknown_keys(config, ALLOWED_ROOT_KEYS, "config")
    if "version" not in config:
        raise ConfigError("version is required")
    if type(config["version"]) is not int:
        raise ConfigError(f"version must be integer 1, got {json_type(config['version'])}")
    if config["version"] != CONFIG_VERSION:
        raise ConfigError(f"unsupported version {config['version']}; expected 1")
    if "project" not in config:
        raise ConfigError("project is required")
    if "gates" not in config:
        raise ConfigError("gates is required")
    if "boundaries" not in config:
        raise ConfigError("boundaries is required")

    return {
        "version": CONFIG_VERSION,
        "project": normalize_project(config["project"]),
        "gates": normalize_gates(config["gates"]),
        "boundaries": normalize_boundaries(config["boundaries"]),
    }


def load_json_config(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc


def summarize(normalized: dict[str, Any]) -> str:
    enabled = [
        name
        for name, gate in normalized["gates"].items()
        if gate.get("enabled")
    ]
    enabled_text = ", ".join(enabled) if enabled else "<none>"
    return f"OK: quality-gates config v1 loaded; enabled gates: {enabled_text}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and normalize Agent Quality Gates JSON config v1.",
    )
    parser.add_argument("--config", required=True, help="path to quality-gates.json")
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="print normalized config JSON to stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        normalized = normalize_config(load_json_config(Path(args.config).expanduser()))
        if args.print_json:
            print(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(summarize(normalized))
        return 0
    except ConfigError as exc:
        print(f"CONFIG_ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - keep CLI failures classified
        print(f"INTERNAL_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
