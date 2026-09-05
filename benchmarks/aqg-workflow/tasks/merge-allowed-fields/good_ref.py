"""Disciplined reference: explicit allowlist and type validation."""
from __future__ import annotations


def merge_allowed_fields(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("profile update must be an object")
    result = {}
    for key in ("display_name", "timezone"):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid {key}")
            result[key] = value
    if not result:
        raise ValueError("no supported profile field")
    return result
