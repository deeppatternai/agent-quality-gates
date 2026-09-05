"""Lazy reference: passes request fields directly to storage."""
from __future__ import annotations


def merge_allowed_fields(data: dict) -> dict:
    return dict(data)
