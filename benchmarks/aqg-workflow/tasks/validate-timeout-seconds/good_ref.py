from __future__ import annotations


def validate_timeout_seconds(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a timeout")
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be an integer") from exc
    if str(value).strip() != str(timeout) or not 1 <= timeout <= 300:
        raise ValueError("timeout must be an integer from 1 to 300 seconds")
    return timeout
