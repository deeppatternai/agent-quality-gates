"""Disciplined reference for upload-manifest validation."""
from __future__ import annotations

from pathlib import PurePosixPath

_ALLOWED_TYPES = frozenset(
    {"text/plain", "text/csv", "application/json", "image/png", "image/jpeg"}
)
_MAX_FILE = 10 * 1024 * 1024
_MAX_TOTAL = 50 * 1024 * 1024


def _normalized_name(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("name must be a non-empty portable relative path")
    if "\x00" in value:
        raise ValueError("name must not contain a NUL byte")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("name must be relative")
    parts = path.parts
    if not parts or any(part in ("..", ".") for part in parts):
        raise ValueError("name escapes the upload root")
    normalized = str(path)
    if normalized in (".", ""):
        raise ValueError("name escapes the upload root")
    return normalized


def _checked_size(value: object) -> int:
    # bool is an int subclass; a flag is not a byte count.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("size must be an integer")
    if value <= 0:
        raise ValueError("size must be positive")
    if value > _MAX_FILE:
        raise ValueError("size exceeds the per-file limit")
    return value


def _checked_type(value: object) -> str:
    if not isinstance(value, str) or value not in _ALLOWED_TYPES:
        raise ValueError("content_type is not accepted")
    return value


def parse_upload_manifest(payload: object) -> list[dict]:
    if not isinstance(payload, list) or isinstance(payload, (str, bytes)):
        raise ValueError("manifest must be a list")
    if not payload:
        raise ValueError("manifest must not be empty")
    accepted: list[dict] = []
    seen: set[str] = set()
    total = 0
    for entry in payload:
        if not isinstance(entry, dict):
            raise ValueError("entry must be an object")
        for key in ("name", "size", "content_type"):
            if key not in entry:
                raise ValueError(f"entry is missing {key}")
        name = _normalized_name(entry["name"])
        size = _checked_size(entry["size"])
        content_type = _checked_type(entry["content_type"])
        if name in seen:
            raise ValueError("duplicate stored name")
        seen.add(name)
        total += size
        if total > _MAX_TOTAL:
            raise ValueError("manifest exceeds the total size limit")
        accepted.append({"name": name, "size": size, "content_type": content_type})
    return accepted
