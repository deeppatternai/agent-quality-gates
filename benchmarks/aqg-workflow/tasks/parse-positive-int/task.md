# Task: parse_positive_int

Fix `parse_positive_int(s)` in `seed.py`.

It parses a user-supplied page-size query param (e.g. `?page_size=5`) into an
integer and returns it.

The current production helper accepts values that caused a pagination incident.
That is the whole ticket — like a real bug report, it does **not** spell out the
edge cases. Decide the input contract from the function's role and implement it
consistently.
