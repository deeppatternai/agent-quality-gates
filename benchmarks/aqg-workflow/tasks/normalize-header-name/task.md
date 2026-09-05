# Task: normalize_header_name

Fix `normalize_header_name(value)` in `seed.py`.

It normalizes a user-supplied HTTP header name for a case-insensitive lookup.
Production accepted malformed names that changed request parsing. Preserve
valid names and reject invalid boundary input.
