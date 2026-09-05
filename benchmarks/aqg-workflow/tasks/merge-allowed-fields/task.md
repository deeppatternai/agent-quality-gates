# Task: merge_allowed_fields

Implement `merge_allowed_fields(data)` in `seed.py`.

It prepares a profile update from a decoded request object. Only the supported
profile fields should reach the stored update. Unsupported fields are ignored
rather than rejected when at least one supported field is present; a request
that contains no supported update is rejected. Supported fields must still have
their documented types.
