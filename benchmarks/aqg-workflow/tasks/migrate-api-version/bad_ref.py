from __future__ import annotations


def migrate_api_version(payload: dict) -> dict:
    if "profile" in payload:
        return payload
    return {"id": payload["id"], "profile": {"display_name": payload.get("name", "")}}
