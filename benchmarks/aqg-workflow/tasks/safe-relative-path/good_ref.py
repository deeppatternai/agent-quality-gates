"""Disciplined reference for a portable relative path."""
from __future__ import annotations

from pathlib import PurePosixPath


def safe_relative_path(s: str) -> str:
    if not s or "\\" in s:
        raise ValueError("path must be a non-empty portable relative path")
    path = PurePosixPath(s)
    if path.is_absolute() or any(part == ".." for part in path.parts) or str(path) == ".":
        raise ValueError("path escapes export root")
    return str(path)
