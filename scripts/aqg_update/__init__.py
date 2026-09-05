"""Managed-update layer for AQG installs (docs/UPDATE_ARCHITECTURE.md).

The layer is a library with ONE public entry point, callable from any trigger
(the context helper, a SessionStart hook, `scripts/upgrade.sh`) and knowing
nothing about which one called it. That invariant is what keeps the apply path
testable without pretending a hook fired.

Only `state` exists so far; the planner, transaction and host adapters land in
later slices. Nothing here is wired into a trigger yet.
"""
