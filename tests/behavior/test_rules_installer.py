"""Behavior contracts for the user-level AQG rules-block installer.

Claude Code and Codex were the only supported clients whose always-resident
rules channel no installer ever wrote: the registry declared a `rules_surface`
for both, `aqg_doctor` checked it, and nothing laid it down — so a scripted
install (the path Decision Engine takes) produced skills and hooks with an
empty resident channel, and the Gate A criteria never reached the model.

Unlike the four adapters that predate it, this installer delimits its region
with BEGIN *and* END markers. `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`
are files the user also hand-edits, and the older "managed region runs to end
of file" convention silently owns anything the user appends after it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install_aqg_rules.py"
HEADING = "## Agent Quality Gates (AQG) engineering discipline"
BEGIN = "<!-- BEGIN AQG rules"
END = "<!-- END AQG rules -->"

CLIENTS = (
    ("claude-code", (".claude", "CLAUDE.md")),
    ("codex", (".codex", "AGENTS.md")),
)


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AQG_BACKUP_DIR"] = str(home / ".aqg-central")
    env.pop("CODEX_HOME", None)
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--aqg-root", str(REPO), "--home", str(home), *args],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _target(home: Path, parts: tuple[str, str]) -> Path:
    return home.joinpath(*parts)


def _template_block() -> str:
    text = (REPO / "examples" / "aqg-codex-agents.example.md").read_text(encoding="utf-8")
    return text[text.find(HEADING):].rstrip()


# --- apply -------------------------------------------------------------------


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_creates_the_file_when_it_does_not_exist(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """The gap being closed: a missing rules file is created, not skipped."""
    target = _target(tmp_path, parts)
    assert not target.exists()

    result = _run(tmp_path, "--client", client, "--apply")

    assert result.returncode == 0, result.stderr
    body = target.read_text(encoding="utf-8")
    assert BEGIN in body and END in body
    assert HEADING in body


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_resolves_the_root_placeholder(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """A literal <AQG_ROOT> in a rules file is a dangling pointer."""
    _run(tmp_path, "--client", client, "--apply")

    body = _target(tmp_path, parts).read_text(encoding="utf-8")
    assert "<AQG_ROOT>" not in body
    assert f"{REPO}/docs/policies/audit-trigger.md" in body


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_keeps_content_the_user_already_had(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    target.write_text("# My own rules\n\nAlways use tabs.\n", encoding="utf-8")

    assert _run(tmp_path, "--client", client, "--apply").returncode == 0

    body = target.read_text(encoding="utf-8")
    assert "Always use tabs." in body
    assert body.index("Always use tabs.") < body.index(BEGIN)


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_is_idempotent(client: str, parts: tuple[str, str], tmp_path: Path) -> None:
    _run(tmp_path, "--client", client, "--apply")
    first = _target(tmp_path, parts).read_text(encoding="utf-8")

    assert _run(tmp_path, "--client", client, "--apply").returncode == 0

    assert _target(tmp_path, parts).read_text(encoding="utf-8") == first


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_replaces_only_the_managed_region(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Content on BOTH sides of the region survives a re-apply.

    The older adapters treat "marker to end of file" as managed, which owns
    whatever the user appends afterwards. These two files are hand-edited.
    """
    target = _target(tmp_path, parts)
    _run(tmp_path, "--client", client, "--apply")
    body = target.read_text(encoding="utf-8")
    target.write_text("# Above\n\n" + body.rstrip() + "\n\n# Below\n", encoding="utf-8")

    assert _run(tmp_path, "--client", client, "--apply").returncode == 0

    after = target.read_text(encoding="utf-8")
    assert "# Above" in after and "# Below" in after
    assert after.count(BEGIN) == 1
    assert after.index("# Above") < after.index(BEGIN) < after.index("# Below")


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_refuses_to_write_through_a_symlink(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("not mine\n", encoding="utf-8")
    target.symlink_to(elsewhere)

    result = _run(tmp_path, "--client", client, "--apply")

    assert result.returncode != 0
    assert "symlink" in (result.stderr + result.stdout).lower()
    assert elsewhere.read_text(encoding="utf-8") == "not mine\n"


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_refuses_a_malformed_region_instead_of_guessing(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """A half-deleted region is ambiguous — refuse, do not re-derive it."""
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    original = f"# Mine\n\n{BEGIN} (managed) -->\nno end marker follows\n"
    target.write_text(original, encoding="utf-8")

    result = _run(tmp_path, "--client", client, "--apply")

    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_backs_up_a_file_it_overwrites(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    target.write_text("# Mine\n", encoding="utf-8")

    _run(tmp_path, "--client", client, "--apply")

    stored = list((tmp_path / ".aqg-central").rglob(parts[1]))
    assert stored, "no backup of the pre-existing rules file"
    assert stored[0].read_text(encoding="utf-8") == "# Mine\n"


# --- uninstall ---------------------------------------------------------------


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_uninstall_removes_the_region_and_keeps_the_users_file(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    target.write_text("# Above\n", encoding="utf-8")
    _run(tmp_path, "--client", client, "--apply")
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n# Below\n")

    assert _run(tmp_path, "--client", client, "--uninstall").returncode == 0

    after = target.read_text(encoding="utf-8")
    assert target.is_file()
    assert "# Above" in after and "# Below" in after
    assert BEGIN not in after and HEADING not in after


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_uninstall_is_a_no_op_when_nothing_is_installed(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    assert _run(tmp_path, "--client", client, "--uninstall").returncode == 0
    assert not _target(tmp_path, parts).exists()


# --- verify / is-installed ---------------------------------------------------


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_verify_fails_before_apply_and_passes_after(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    assert _run(tmp_path, "--client", client, "--verify").returncode != 0
    _run(tmp_path, "--client", client, "--apply")
    assert _run(tmp_path, "--client", client, "--verify").returncode == 0


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_verify_catches_a_hand_edited_region(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Verify compares the region's content, not just the markers' presence."""
    target = _target(tmp_path, parts)
    _run(tmp_path, "--client", client, "--apply")
    body = target.read_text(encoding="utf-8")
    target.write_text(body.replace("Blast radius outranks size", "whatever"), encoding="utf-8")

    assert _run(tmp_path, "--client", client, "--verify").returncode != 0


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_is_installed_reflects_state(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    assert _run(tmp_path, "--client", client, "--is-installed").returncode != 0
    _run(tmp_path, "--client", client, "--apply")
    assert _run(tmp_path, "--client", client, "--is-installed").returncode == 0


# --- content + registry wiring ----------------------------------------------


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_the_written_region_is_the_shipped_block_verbatim(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """No second copy of the discipline: what lands is what the template says."""
    _run(tmp_path, "--client", client, "--apply")
    body = _target(tmp_path, parts).read_text(encoding="utf-8")
    region = body[body.index(BEGIN):body.index(END)]

    expected = _template_block().replace("<AQG_ROOT>", str(REPO))
    assert expected in region


def test_the_registry_routes_both_core_clients_through_the_installer() -> None:
    sys.path.insert(0, str(REPO))
    from scripts.aqg_client_registry import get_client  # noqa: PLC0415

    for client_id in ("codex", "claude-code"):
        spec = get_client(client_id)
        for attribute in (
            "installer_command",
            "verify_command",
            "uninstall_command",
            "is_installed_command",
        ):
            commands = getattr(spec, attribute)
            assert any("install_aqg_rules.py" in command for command in commands), (
                f"{client_id} {attribute} does not reach the rules installer"
            )


def test_a_failed_rules_write_does_not_fail_the_install() -> None:
    """Owner constraint: the resident block is worth a warning, not a broken install.

    install_aqg_clients.py runs each command with check=True and breaks the
    client's chain on the first non-zero exit, so the apply command has to
    absorb its own failure. Verify deliberately does not — a verify that lies
    is worse than one that reports the gap.
    """
    sys.path.insert(0, str(REPO))
    from scripts.aqg_client_registry import get_client  # noqa: PLC0415

    for client_id in ("codex", "claude-code"):
        spec = get_client(client_id)
        apply_command = next(
            command for command in spec.installer_command if "install_aqg_rules.py" in command
        )
        assert "||" in apply_command, f"{client_id} apply aborts the install on a rules failure"
        verify_command = next(
            command for command in spec.verify_command if "install_aqg_rules.py" in command
        )
        assert "||" not in verify_command, f"{client_id} verify swallows a rules failure"


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_refuses_to_duplicate_a_legacy_unmarked_block(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Everyone who installed before this script has an unmarked block.

    It was pasted by an agent following AI_SETUP's hand-edit procedure, so it
    carries no markers and the region scan cannot see it. Appending would leave
    the file stating the discipline twice, and adopting it would mean guessing
    where the user's own prose resumes — so refuse and say what to remove.
    """
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    legacy = f"# My rules\n\n{HEADING}\n\n- some older wording\n"
    target.write_text(legacy, encoding="utf-8")

    result = _run(tmp_path, "--client", client, "--apply")

    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == legacy
    combined = (result.stdout + result.stderr).lower()
    assert "unmarked" in combined or "legacy" in combined


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_a_managed_region_is_not_mistaken_for_a_legacy_block(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """The heading inside our own region must not trip the legacy guard."""
    _run(tmp_path, "--client", client, "--apply")
    assert _run(tmp_path, "--client", client, "--apply").returncode == 0


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_uninstall_does_not_claim_success_over_a_legacy_block(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Exit 0 with the block still in the file is a false success.

    A legacy unmarked block is still an AQG block. Reporting "nothing to remove"
    tells the user their machine is clean while the discipline is still resident,
    and an uninstall that lies is worse than one that says it cannot proceed.
    """
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    legacy = f"# My rules\n\n{HEADING}\n\n- some older wording\n"
    target.write_text(legacy, encoding="utf-8")

    result = _run(tmp_path, "--client", client, "--uninstall")

    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == legacy
    combined = (result.stdout + result.stderr).lower()
    assert "unmarked" in combined or "legacy" in combined


# --- fixes from audit aud_ADLH2rZiYBK6xF7h ------------------------------------


def test_the_fail_open_warning_does_not_claim_the_other_channels_are_fine() -> None:
    """Panel finding (google, blocking): the warning was factually false.

    Skills and hooks are present on disk but operationally dormant without the
    resident block: Gate A is what decides whether a change needs an audit at
    all, and it is asked before any skill is invoked. Telling the user the rest
    is "unaffected" is the same false reassurance this whole change exists to
    remove.
    """
    sys.path.insert(0, str(REPO))
    from scripts.aqg_client_registry import get_client  # noqa: PLC0415

    for client_id in ("codex", "claude-code"):
        command = next(
            c for c in get_client(client_id).installer_command if "install_aqg_rules.py" in c
        )
        assert "unaffected" not in command, f"{client_id} still claims the rest is fine"
        assert "Gate A" in command, f"{client_id} does not say what stops working"


def test_uninstall_is_absorbed_the_same_way_apply_is() -> None:
    """Panel finding (opus f2): the asymmetry could mark a client FAILED.

    The rules step is last in both uninstall lists, so nothing is stranded after
    it, but a hand-edited region should not turn a working uninstall into a
    failed one either.
    """
    sys.path.insert(0, str(REPO))
    from scripts.aqg_client_registry import get_client  # noqa: PLC0415

    for client_id in ("codex", "claude-code"):
        command = next(
            c for c in get_client(client_id).uninstall_command if "install_aqg_rules.py" in c
        )
        assert "||" in command, f"{client_id} uninstall aborts on a rules failure"


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_is_installed_says_why_when_a_legacy_block_is_present(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Panel finding (opus f5): "absent" and "legacy present" are different states."""
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    target.write_text(f"# Mine\n\n{HEADING}\n\n- old\n", encoding="utf-8")

    result = _run(tmp_path, "--client", client, "--is-installed")

    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "unmarked" in combined or "legacy" in combined


def test_codex_home_is_honoured_on_the_path_no_test_used_to_reach(tmp_path: Path) -> None:
    """Panel finding (opus f4): --home short-circuits the production branch.

    Every other test passes --home, which returns before client_root() consults
    CODEX_HOME — so the branch the real install takes had zero coverage.
    """
    codex_home = tmp_path / "relocated-codex"
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    env["AQG_BACKUP_DIR"] = str(tmp_path / ".aqg-central")
    result = subprocess.run(
        [sys.executable, str(INSTALLER), "--client", "codex", "--apply",
         "--aqg-root", str(REPO)],
        cwd=REPO, text=True, capture_output=True, check=False, env=env,
    )

    assert result.returncode == 0, result.stderr
    # CODEX_HOME is the client root itself, not a directory containing `.codex`.
    assert (codex_home / "AGENTS.md").is_file()
    assert not (codex_home / ".codex").exists()


def test_home_wins_over_codex_home_when_both_are_set(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(tmp_path / "ignored")
    env["AQG_BACKUP_DIR"] = str(tmp_path / ".aqg-central")
    subprocess.run(
        [sys.executable, str(INSTALLER), "--client", "codex", "--apply",
         "--aqg-root", str(REPO), "--home", str(tmp_path / "home")],
        cwd=REPO, text=True, capture_output=True, check=False, env=env,
    )

    assert (tmp_path / "home" / ".codex" / "AGENTS.md").is_file()
    assert not (tmp_path / "ignored").exists()


def test_the_installer_and_doctor_agree_on_the_codex_rules_path(tmp_path: Path) -> None:
    """Doctor reporting one path while the installer writes another would
    reproduce the exact bug this change exists to fix."""
    sys.path.insert(0, str(REPO / "scripts"))
    import install_aqg_rules as installer  # noqa: PLC0415

    codex_home = tmp_path / "elsewhere"
    os.environ["CODEX_HOME"] = str(codex_home)
    try:
        assert installer.rules_path(installer.CLIENTS["codex"], None) == (
            codex_home / "AGENTS.md"
        )
    finally:
        os.environ.pop("CODEX_HOME", None)


@pytest.mark.parametrize("client,parts", CLIENTS)
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not a Windows ACL contract")
def test_apply_preserves_the_files_permission_mode(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Panel finding (opus f8): mkstemp creates 0600 and os.replace carries it.

    A rules file the user had made group-readable would silently narrow, which
    contradicts the "only that region changes" guarantee AI_SETUP gives.
    """
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    target.write_text("# Mine\n", encoding="utf-8")
    target.chmod(0o644)

    _run(tmp_path, "--client", client, "--apply")

    assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_does_not_rewrite_the_users_line_endings(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Panel finding (opus f8): universal-newline read + newline='\\n' write
    converts a CRLF file to LF throughout, which is every line outside the
    region — and Windows is a supported host."""
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# Mine\r\nkeep my CRLF\r\nlast line\r\n")

    _run(tmp_path, "--client", client, "--apply")

    body = target.read_bytes()
    # Interior lines keep their endings. The single join newline between the
    # user's content and the appended region is LF, which is the region's own.
    assert b"# Mine\r\nkeep my CRLF\r\n" in body


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_verify_names_relocation_instead_of_blaming_the_user(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Panel finding (opus f9): the checkout path is baked in and re-derived.

    Move the checkout and verify said "edited by hand, or written by an older
    version" — two causes, neither of them the real one, while AI_SETUP tells
    the reader to report it rather than paper over it.
    """
    _run(tmp_path, "--client", client, "--apply")
    target = _target(tmp_path, parts)
    target.write_bytes(target.read_bytes().replace(str(REPO).encode(), b"/somewhere/else"))

    result = _run(tmp_path, "--client", client, "--verify")

    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "checkout" in combined or "relocat" in combined
    assert "/somewhere/else" in (result.stdout + result.stderr)


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_apply_aborts_if_the_file_changed_since_it_was_read(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Panel finding (feat f1, blocking): read-modify-write loses a concurrent edit.

    Exercised through the seam rather than by racing two processes: the guard
    must compare what it is about to replace against what it read.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import install_aqg_rules as installer  # noqa: PLC0415

    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    target.write_text("# First\n", encoding="utf-8")
    profile = installer.CLIENTS[client]

    with pytest.raises(installer.InstallError, match="changed"):
        installer._write_with_backup(
            profile, target, "replacement\n", REPO, expected="# Something else\n"
        )
    assert target.read_text(encoding="utf-8") == "# First\n"


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_prose_mentioning_the_heading_is_not_taken_for_a_legacy_block(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Panel finding (feat f3): a user who writes about the block was locked out."""
    target = _target(tmp_path, parts)
    target.parent.mkdir(parents=True)
    target.write_text(
        f"# Notes\n\nSee the `{HEADING}` section in the AQG repo for why.\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--client", client, "--apply")

    assert result.returncode == 0, result.stderr
    assert "See the" in target.read_text(encoding="utf-8")


def test_an_invalid_explicit_root_is_an_error_and_the_fallback_is_validated(
    tmp_path: Path,
) -> None:
    """Panel finding (opus f7): the fallback skipped the check the others enforce.

    doctor's fix line deliberately omits --aqg-root and relies on that fallback.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import install_aqg_rules as installer  # noqa: PLC0415

    with pytest.raises(installer.InstallError, match="not an AQG checkout"):
        installer._resolve_aqg_root(str(tmp_path))
    saved = os.environ.pop("AQG_ROOT", None)
    try:
        assert installer._resolve_aqg_root(None) == REPO
    finally:
        if saved is not None:
            os.environ["AQG_ROOT"] = saved


@pytest.mark.parametrize("client,parts", CLIENTS)
def test_a_symlinked_client_directory_is_followed_by_design(
    client: str, parts: tuple[str, str], tmp_path: Path
) -> None:
    """Panel finding (opus f6 / feat f2): only the leaf file is refused.

    Symlinking the whole config directory into a dotfiles repo is a normal,
    supported layout, so refusing it would break real users. Recording the
    decision here so the behaviour is chosen rather than incidental, and so the
    AI_SETUP wording that promises symlink refusal stays scoped to the file.
    """
    real = tmp_path / "dotfiles" / parts[0]
    real.mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / parts[0]).symlink_to(real)

    result = _run(tmp_path / "home", "--client", client, "--apply")

    assert result.returncode == 0, result.stderr
    assert (real / parts[1]).is_file()
