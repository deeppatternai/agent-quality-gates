# Task: migrate_api_version

Implement `migrate_api_version(payload)` in `seed.py` for a v1-to-v2 profile
API rollout. Callers may still send the old record shape while new callers
already send the v2 shape. Return the canonical v2 record so downstream code
has one contract to consume; do not let malformed records cross the migration
boundary.
