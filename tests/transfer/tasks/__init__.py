"""Transfer Test Pack v1 task modules.

Each module exposes a `run(repo: Path) -> TaskOutcome` function. The runner
loads modules dynamically by `task<N>_<name>` convention and converts the
returned TaskOutcome into the §4 task record schema.
"""
