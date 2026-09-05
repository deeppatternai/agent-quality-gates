"""Lazy reference: only blocks an absolute POSIX path."""
from __future__ import annotations


def safe_relative_path(s: str) -> str:
    if s.startswith("/"):
        raise ValueError("absolute")
    return s
