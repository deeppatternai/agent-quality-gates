"""Lazy reference: accepts any date syntax accepted by the library."""
from __future__ import annotations

from datetime import datetime


def parse_utc_date(s: str):
    return datetime.fromisoformat(s).date()
