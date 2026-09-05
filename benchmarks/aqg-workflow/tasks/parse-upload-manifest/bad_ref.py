"""Lazy reference: checks the obvious things and trusts the rest."""
from __future__ import annotations


def parse_upload_manifest(payload: object) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("manifest must be a list")
    out = []
    for entry in payload:
        if entry["name"].startswith("/"):
            raise ValueError("absolute")
        out.append({
            "name": entry["name"],
            "size": entry["size"],
            "content_type": entry["content_type"],
        })
    return out
