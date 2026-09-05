"""Lazy-but-plausible reference: correct on the happy path, but skips the
trust-boundary check, so it accepts "0" and "-3" as a page size. This is the
code a bare "write less" reflex produces; selftest.py requires the instrument
to CATCH it (else the benchmark can't tell discipline from carelessness)."""
from __future__ import annotations


def parse_positive_int(s: str) -> int:
    return int(s)
