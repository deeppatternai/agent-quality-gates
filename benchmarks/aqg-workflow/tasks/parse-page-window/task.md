# Task: parse_page_window

Fix `parse_page_window(s)` in `seed.py` after an API listing incident.

It parses an `offset:limit` query value for an API listing endpoint and returns
an `(offset, limit)` pair. Keep it safe for a public pagination boundary.
