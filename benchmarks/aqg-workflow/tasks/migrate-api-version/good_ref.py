from __future__ import annotations


def migrate_api_version(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) - {"id", "name", "profile"}:
        raise TypeError("unsupported profile record")
    user_id = payload.get("id")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("id is required")
    if "name" in payload and "profile" not in payload:
        name = payload["name"]
    elif set(payload) == {"id", "profile"} and isinstance(payload["profile"], dict):
        if set(payload["profile"]) != {"display_name"}:
            raise ValueError("invalid v2 profile")
        name = payload["profile"]["display_name"]
    else:
        raise ValueError("unsupported profile shape")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("display name is required")
    return {"id": user_id, "profile": {"display_name": name}}
