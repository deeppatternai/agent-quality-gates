"""Disciplined reference: only a single-root relative path is allowed."""
from __future__ import annotations

from urllib.parse import urlsplit


def build_redirect_path(next_path: str) -> str:
    if not next_path or "\\" in next_path or "\r" in next_path or "\n" in next_path:
        raise ValueError("invalid redirect")
    parsed = urlsplit(next_path)
    if parsed.scheme or parsed.netloc or not next_path.startswith("/") or next_path.startswith("//"):
        raise ValueError("redirect must remain relative")
    return next_path
