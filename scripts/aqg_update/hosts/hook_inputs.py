"""Conservative proof that a root swap leaves installed hook definitions valid.

Compare source bytes, never execute the staged release's installer. A changed
input needs normal reconciliation, even if it might render the same command.
Codex pins each hook script and its runner in the trusted command digest.
"""
from pathlib import Path


def unchanged(
    client_id: str, current: Path, target: Path, *, include_script_bodies: bool = True,
) -> bool:
    """Compare definitions/roster, and also hook bodies for digest-pinned hosts.

    Claude may execute changed signed shell bodies without changing its command
    configuration. Its live inspector still cannot interpret a changed target
    installer, so definition and roster equality remain necessary.
    False means equality was not proved, including unknown clients; it does not
    assert that a change was observed. Only registered update adapters supply
    hook evidence to the planner.
    """
    installers = {
        "claude-code": ("scripts/install_aqg_hooks.py",),
        "codex": (
            "scripts/install_aqg_hooks.py",
            "scripts/install_aqg_codex_hooks.py",
            "scripts/run_aqg_codex_hook.py",
        ),
    }.get(client_id)
    if installers is None:
        return False
    hooks = Path("agent-packs/claude-code/hooks")
    try:
        old = {p.relative_to(current) for p in (current / hooks).glob("*.sh")}
        new = {p.relative_to(target) for p in (target / hooks).glob("*.sh")}
        if not old or old != new:
            return False
        definitions = {Path(name) for name in installers}
        return all(
            (current / name).is_file() and not (current / name).is_symlink()
            and (target / name).is_file() and not (target / name).is_symlink()
            and (
                not include_script_bodies and name not in definitions
                or (current / name).read_bytes() == (target / name).read_bytes()
            )
            for name in old | definitions
        )
    except OSError:
        return False
