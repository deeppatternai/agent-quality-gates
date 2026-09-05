"""Disciplined reference: malformed/non-object JSON is not configuration."""
from __future__ import annotations

import json


def load_json_object(s: str) -> dict:
    try:
        value = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value
