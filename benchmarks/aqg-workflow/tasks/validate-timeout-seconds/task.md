# Task: validate_timeout_seconds

Fix `validate_timeout_seconds(value)` in `seed.py`.

It converts a caller-supplied request timeout into integer seconds. Production
accepted values that disabled the timeout or tied up a worker for too long.
Define the safe supported range and reject invalid input.
