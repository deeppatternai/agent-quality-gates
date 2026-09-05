# Task: parse_upload_manifest

Implement `parse_upload_manifest(payload)` in `seed.py`.

It validates a decoded upload manifest before any file is accepted, and returns
the normalized entries the storage layer will act on.

The payload is a list of entry objects. Each entry declares:

- `name` — the portable relative path the file will be stored under, below the
  upload root. It must stay inside that root.
- `size` — the declared byte count.
- `content_type` — one of `text/plain`, `text/csv`, `application/json`,
  `image/png`, `image/jpeg`.

Return a list of normalized entries, each a dict with exactly `name`, `size` and
`content_type`, in the order the accepted entries appeared. Extra keys on an
input entry are ignored rather than rejected. A single file may not exceed
10 MiB (10485760 bytes) and the manifest as a whole may not exceed 50 MiB
(52428800 bytes).

Reject an invalid manifest by raising `ValueError`. A manifest is invalid when
it is not a list, when it is empty, when any entry is malformed or violates the
rules above, or when two accepted entries would be stored under the same name.
