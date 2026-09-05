from __future__ import annotations

import re

_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def normalize_header_name(value: str) -> str:
    if not isinstance(value, str) or not value or _TOKEN.fullmatch(value) is None:
        raise ValueError("invalid HTTP header name")
    return value.lower()
