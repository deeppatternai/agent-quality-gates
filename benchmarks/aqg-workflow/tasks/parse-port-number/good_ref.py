from __future__ import annotations


def parse_port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid TCP port: {port}")
    return port
