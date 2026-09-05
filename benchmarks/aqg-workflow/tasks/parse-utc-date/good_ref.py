"""Disciplined reference: exact wire shape plus calendar validation."""
from __future__ import annotations

import re
from datetime import date

_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


def parse_utc_date(s: str) -> date:
    if not _DATE.fullmatch(s):
        raise ValueError("expected YYYY-MM-DD")
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError("invalid calendar date") from exc
