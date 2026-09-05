"""Lazy reference: parses JSON but trusts its top-level type."""
from __future__ import annotations

import json


def load_json_object(s: str) -> dict:
    return json.loads(s)
