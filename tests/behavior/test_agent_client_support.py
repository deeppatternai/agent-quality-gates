"""Behavior contracts for Trae-family, Zed, and Devin AQG support."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install_aqg_agent_clients.py"
ADAPTER = REPO / "scripts" / "agent_client_aqg_hook.py"
CLIENTS = ("trae", "trae-cn", "trae-work", "trae-work-cn", "zed", "devin")
HOOK_CLIENTS = ("trae", "trae-cn", "devin")
NO_HOOK_CLIENTS = ("trae-work", "trae-work-cn", "zed")

# AQG-024 host-recovery blocker 3 (2026-09-03 incident report): trae-cn and
# trae-work-cn project scope shares the international ".trae" project
# directory with no verified TRAE CN product evidence for that sharing. Until
# evidence exists, project scope for these two profiles fails closed; only
# user scope is exercised by the general project-scope round trip below.
UNVERIFIED_PROJECT_SCOPE_CLIENTS = ("trae-cn", "trae-work-cn")
PROJECT_SCOPE_VERIFIED_CLIENTS = tuple(
    client for client in CLIENTS if client not in UNVERIFIED_PROJECT_SCOPE_CLIENTS
)

# R24-07: TRAE Work / Work CN read the same user skills root as the Trae IDE of
# their own region (`TRAE SOLO.app` -> `~/.trae`, `TRAE SOLO CN.app` -> `~/.trae-cn`).
SHARED_SKILL_ROOTS = {
    "trae": ".trae",
    "trae-work": ".trae",
    "trae-cn": ".trae-cn",
    "trae-work-cn": ".trae-cn",
}
SHARED_ROOT_PAIRS = (
    ("trae", "trae-work"),
    ("trae-work", "trae"),
    ("trae-cn", "trae-work-cn"),
    ("trae-work-cn", "trae-cn"),
)


def _skill_names() -> list[str]:
    return sorted(path.name for path in (REPO / "skills").glob("aqg-*") if path.is_dir())


def _installed_skill_names(skills_root: Path) -> list[str]:
    return sorted(path.name for path in skills_root.glob("aqg-*"))


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env is not None:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
    )


def _common(client: str, home: Path, *extra: str) -> tuple[str, ...]:
    return (
        "--client",
        client,
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--mode",
        "copy",
        *extra,
    )


@pytest.fixture(autouse=True)
def _isolate_central_backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route every installer run's central backup store into this test's tmp dir.

    The subprocess installer inherits ``os.environ``, so setting the override here
    keeps backups out of ``dirname(AQG_ROOT)/aqg-backups`` (the repo) for the whole
    file, not just the assertions below.
    """
    central = tmp_path / ".aqg-central"
    monkeypatch.setenv("AQG_BACKUP_DIR", str(central))
    monkeypatch.delenv("AQG_ROOT", raising=False)
    monkeypatch.delenv("AQG_BACKUP_KEEP", raising=False)
    return central


def _manifests(central: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in central.rglob("manifest.json")
    ]


@pytest.mark.parametrize("client", ("trae", "devin"))
def test_agent_client_uninstall_backs_up_to_central_store(
    client: str, tmp_path: Path, _isolate_central_backups: Path
) -> None:
    home = tmp_path / "home"
    common = _common(client, home)
    assert _run("--apply", *common).returncode == 0

    uninstall = _run("--uninstall", *common)
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr

    manifests = _manifests(_isolate_central_backups)
    assert manifests, "uninstall wrote no central backup manifest"
    install_runs = [m for m in manifests if m["origin"] == "install"]
    assert install_runs
    for data in install_runs:
        assert data["client"] == client
        assert data["scope"] == "user"
        assert Path(data["source_root"]).is_absolute()
        assert data["entries"], "manifest recorded no backed-up entries"
        for entry in data["entries"]:
            assert entry["relpath"] and ".." not in Path(entry["relpath"]).parts
    # A copy-mode skill dir was stashed as a real, restorable tree.
    skill_dirs = [
        entry
        for data in install_runs
        for entry in data["entries"]
        if entry["kind"] == "dir" and entry["relpath"].split("/")[-1].startswith("aqg-")
    ]
    assert skill_dirs, "no managed skill directory was backed up"


def test_agent_client_zed_backs_up_disjoint_roots_to_central_store(
    tmp_path: Path, _isolate_central_backups: Path
) -> None:
    # Zed writes skills under ~/.agents/skills but config/rules under its own
    # config root, so one run must open a session per disjoint source_root.
    home = tmp_path / "home"
    common = _common("zed", home)
    assert _run("--apply", *common).returncode == 0
    assert _run("--uninstall", *common).returncode == 0

    zed_manifests = [m for m in _manifests(_isolate_central_backups) if m["client"] == "zed"]
    source_roots = {Path(m["source_root"]) for m in zed_manifests if m["origin"] == "install"}
    assert len(source_roots) >= 2, (
        f"expected disjoint skills/config source_roots, got {source_roots}"
    )


@pytest.mark.parametrize("client", ("trae", "devin"))
def test_agent_client_migrates_and_removes_legacy_inplace_backups(
    client: str, tmp_path: Path, _isolate_central_backups: Path
) -> None:
    home = tmp_path / "home"
    if client == "trae":
        config_root = home / ".trae"
    elif os.name == "nt":
        config_root = home / "AppData" / "Roaming" / "devin"
    else:
        config_root = home / ".config" / "devin"
    legacy = config_root / ".aqg-backups"
    legacy.mkdir(parents=True)
    (legacy / "old-sentinel.txt").write_text("LEGACY", encoding="utf-8")

    apply = _run("--apply", *_common(client, home))
    assert apply.returncode == 0, apply.stdout + apply.stderr

    assert not legacy.exists(), "legacy in-place backup dir was not removed after migration"
    migrated = list(_isolate_central_backups.rglob("old-sentinel.txt"))
    assert migrated, "legacy backup content was not migrated into the central store"
    assert migrated[0].read_text(encoding="utf-8") == "LEGACY"
    assert any("migrated-" in part for part in migrated[0].parts)
    legacy_manifests = [
        m for m in _manifests(_isolate_central_backups) if m["origin"] == "legacy-inplace"
    ]
    assert legacy_manifests and legacy_manifests[0]["client"] == client


@pytest.mark.parametrize("client", CLIENTS)
def test_user_scope_apply_verify_is_installed_uninstall_round_trip(client: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    common = _common(client, home)

    apply = _run("--apply", *common)
    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert "support_level: partial" in apply.stdout

    verify = _run("--verify", *common)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    installed = _run("--is-installed", *common)
    assert installed.returncode == 0, installed.stdout + installed.stderr

    uninstall = _run("--uninstall", *common)
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    missing = _run("--is-installed", *common)
    assert missing.returncode == 1


@pytest.mark.parametrize("client", PROJECT_SCOPE_VERIFIED_CLIENTS)
def test_project_scope_installs_managed_rule_without_touching_home(client: str, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    real_home_sentinel = tmp_path / "real-home-sentinel"
    real_home_sentinel.mkdir()
    common = _common(
        client,
        real_home_sentinel,
        "--scope",
        "project",
        "--project-root",
        str(project),
        "--no-hooks",
    )

    apply = _run("--apply", *common)

    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert (project / "AGENTS.md").read_text(encoding="utf-8").count("AQG-MANAGED") == 1
    assert not any(real_home_sentinel.iterdir())


@pytest.mark.parametrize("client", HOOK_CLIENTS)
def test_hook_clients_install_managed_hook_events(client: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    apply = _run("--apply", *_common(client, home))
    assert apply.returncode == 0, apply.stdout + apply.stderr

    if client == "devin":
        devin_root = (
            home / "AppData" / "Roaming" / "devin"
            if os.name == "nt"
            else home / ".config" / "devin"
        )
        settings = json.loads((devin_root / "config.json").read_text(encoding="utf-8"))
        hooks = settings["hooks"]
        assert "PostCompaction" in hooks
    else:
        root_name = ".trae-cn" if client == "trae-cn" else ".trae"
        hooks = json.loads((home / root_name / "hooks.json").read_text(encoding="utf-8"))
    for event in ("SessionStart", "PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit"):
        assert event in hooks


@pytest.mark.parametrize("client", NO_HOOK_CLIENTS)
def test_no_hook_clients_do_not_write_hook_files_by_default(client: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    apply = _run("--apply", *_common(client, home))
    assert apply.returncode == 0, apply.stdout + apply.stderr

    assert not list(home.rglob("hooks.json"))
    assert not list(home.rglob("config.json"))


def test_zed_user_scope_uses_home_agents_skills_not_config_agents(tmp_path: Path) -> None:
    home = tmp_path / "home"

    apply = _run("--apply", *_common("zed", home))

    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert (home / ".agents" / "skills" / "aqg-startup-preflight").is_dir()
    assert not (home / "AppData" / ".agents").exists()
    assert not (home / ".config" / ".agents").exists()


def test_zed_user_scope_uses_appdata_rules_and_home_agents_skills(tmp_path: Path) -> None:
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"

    apply = _run("--apply", *_common("zed", home), env={"APPDATA": str(appdata)})

    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert (home / ".agents" / "skills" / "aqg-startup-preflight").is_dir()
    assert (appdata / "Zed" / "AGENTS.md").is_file()
    assert not (home / "AppData" / ".agents").exists()
    assert not (home / ".config" / ".agents").exists()


def test_zed_user_scope_ignores_path_binary_for_install_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    zed = bin_dir / ("zed.exe" if os.name == "nt" else "zed")
    zed.write_text("", encoding="utf-8")
    try:
        zed.chmod(0o755)
    except OSError:
        pass

    apply = _run(
        "--apply",
        *_common("zed", home),
        env={"APPDATA": str(appdata), "PATH": str(bin_dir)},
    )

    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert (home / ".agents" / "skills" / "aqg-startup-preflight").is_dir()
    assert (appdata / "Zed" / "AGENTS.md").is_file()
    assert not (bin_dir / ".agents").exists()
    assert not (bin_dir / "AGENTS.md").exists()


def test_zed_user_scope_unix_fallback_rules_and_home_agents_skills(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Unix fallback assertion is for POSIX runners")
    home = tmp_path / "home"

    apply = _run("--apply", *_common("zed", home), env={"APPDATA": ""})

    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert (home / ".agents" / "skills" / "aqg-startup-preflight").is_dir()
    assert (home / ".config" / "zed" / "AGENTS.md").is_file()


def test_devin_user_scope_uses_appdata_for_skills_rules_and_config(tmp_path: Path) -> None:
    # Official evidence: Devin CLI Windows global skills/config live under
    # `%APPDATA%\devin`, while repository skills use `.agents/skills`.
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"

    apply = _run("--apply", *_common("devin", home), env={"APPDATA": str(appdata)})

    assert apply.returncode == 0, apply.stdout + apply.stderr
    root = appdata / "devin"
    assert (root / "skills" / "aqg-startup-preflight").is_dir()
    assert (root / "AGENTS.md").is_file()
    assert (root / "config.json").is_file()
    assert f"at {root}" in apply.stdout
    assert not (home / ".config" / "devin").exists()


def test_devin_user_scope_unix_fallback_uses_config_root(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Unix fallback assertion is for POSIX runners")
    home = tmp_path / "home"

    apply = _run("--apply", *_common("devin", home), env={"APPDATA": ""})

    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert (home / ".config" / "devin" / "skills" / "aqg-startup-preflight").is_dir()
    assert (home / ".config" / "devin" / "AGENTS.md").is_file()
    assert (home / ".config" / "devin" / "config.json").is_file()


def test_adapter_fails_closed_for_bad_managed_id(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(ADAPTER),
            "--client",
            "trae",
            "--aqg-root",
            str(REPO),
            "--hook",
            "pretooluse_secret_scan.sh",
            "--managed-id",
            "wrong",
        ],
        input=json.dumps({"cwd": str(tmp_path)}),
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 2
    assert json.loads(proc.stdout)["permissionDecision"] == "deny"


# --------------------------------------------------------------------------
# AQG-024 Work-profile roots and shared-root ownership (R24-07, R24-08)
# --------------------------------------------------------------------------


def test_aqg_ships_the_expected_sixteen_skills() -> None:
    # The shared-root assertions below compare against this set, so pin the
    # count once rather than repeating a magic number in every assertion.
    assert len(_skill_names()) == 16


@pytest.mark.parametrize(
    ("client", "shared_root"),
    [("trae-work", ".trae"), ("trae-work-cn", ".trae-cn")],
)
def test_work_profiles_install_into_the_shared_trae_skills_root(
    client: str,
    shared_root: str,
    tmp_path: Path,
) -> None:
    # R24-07: TRAE Work reads `~/.trae/skills`; a private `.trae-work*` skills
    # root puts the 16 skills where the product will never look for them.
    home = tmp_path / "home"

    apply = _run("--apply", *_common(client, home))

    assert apply.returncode == 0, apply.stdout + apply.stderr
    skills_root = home / shared_root / "skills"
    assert _installed_skill_names(skills_root) == _skill_names()
    assert (skills_root / "aqg-startup-preflight" / "SKILL.md").is_file()
    assert not list(home.glob(".trae-work*")), sorted(
        path.name for path in home.iterdir()
    )

    verify = _run("--verify", *_common(client, home))
    assert verify.returncode == 0, verify.stdout + verify.stderr


@pytest.mark.parametrize("first, second", SHARED_ROOT_PAIRS)
def test_shared_root_install_order_converges_on_one_managed_skill_set(
    first: str,
    second: str,
    tmp_path: Path,
) -> None:
    # R24-08: both install orders must converge; the second apply is a no-op on
    # the skills the first one placed, not a duplicate or a partial overwrite.
    home = tmp_path / "home"
    skills_root = home / SHARED_SKILL_ROOTS[first] / "skills"

    for client in (first, second):
        apply = _run("--apply", *_common(client, home))
        assert apply.returncode == 0, apply.stdout + apply.stderr

    assert _installed_skill_names(skills_root) == _skill_names()
    for client in (first, second):
        verify = _run("--verify", *_common(client, home))
        assert verify.returncode == 0, verify.stdout + verify.stderr


@pytest.mark.parametrize("removed, surviving", SHARED_ROOT_PAIRS)
def test_uninstalling_one_shared_profile_keeps_the_other_profile_working(
    removed: str,
    surviving: str,
    tmp_path: Path,
) -> None:
    # R24-08: both uninstall orders. Removing one profile must not strip the
    # skills the co-resident profile still needs, and must never touch a skill
    # the user owns.
    home = tmp_path / "home"
    skills_root = home / SHARED_SKILL_ROOTS[removed] / "skills"
    skills_root.mkdir(parents=True)
    user_skill = skills_root / "team-playbook"
    user_skill.mkdir()
    (user_skill / "SKILL.md").write_text("user-owned", encoding="utf-8")

    for client in (removed, surviving):
        apply = _run("--apply", *_common(client, home))
        assert apply.returncode == 0, apply.stdout + apply.stderr

    first_uninstall = _run("--uninstall", *_common(removed, home))
    assert first_uninstall.returncode == 0, first_uninstall.stdout + first_uninstall.stderr

    assert _installed_skill_names(skills_root) == _skill_names()
    still_installed = _run("--is-installed", *_common(surviving, home))
    assert still_installed.returncode == 0, still_installed.stdout + still_installed.stderr
    assert (user_skill / "SKILL.md").read_text(encoding="utf-8") == "user-owned"

    last_uninstall = _run("--uninstall", *_common(surviving, home))
    assert last_uninstall.returncode == 0, last_uninstall.stdout + last_uninstall.stderr

    assert _installed_skill_names(skills_root) == []
    assert _run("--is-installed", *_common(surviving, home)).returncode == 1
    assert (user_skill / "SKILL.md").read_text(encoding="utf-8") == "user-owned"


# --------------------------------------------------------------------------
# AQG-024 host-recovery blocker 2 (2026-09-03 incident report): a pre-024
# shared root that predates the owners ledger must self-heal ownership so a
# sibling profile's uninstall cannot delete skills the legacy profile still
# needs. Reproduces the exact independent-Test-Owner finding: applying a
# sibling onto legacy content, then uninstalling only the sibling, used to
# wipe the whole shared skills root because the ledger only ever recorded the
# sibling that happened to (re-)apply under the new ownership-tracking code.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legacy", "sibling", "root"),
    [("trae", "trae-work", ".trae"), ("trae-cn", "trae-work-cn", ".trae-cn")],
)
def test_legacy_shared_root_without_owners_ledger_self_heals_and_protects_sibling(
    legacy: str,
    sibling: str,
    root: str,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    # Simulate a pre-024 install: skills + markers exist (as any AQG version
    # has always written them), but the owners ledger - introduced by 024 -
    # was never written because the legacy code predates that concept.
    legacy_apply = _run("--apply", *_common(legacy, home))
    assert legacy_apply.returncode == 0, legacy_apply.stdout + legacy_apply.stderr
    owners_ledger = home / root / "managed-links" / ".aqg-agent-client-owners.json"
    assert owners_ledger.is_file(), "fixture assumption broken: apply always wrote a ledger"
    owners_ledger.unlink()

    sibling_apply = _run("--apply", *_common(sibling, home))
    assert sibling_apply.returncode == 0, sibling_apply.stdout + sibling_apply.stderr

    sibling_uninstall = _run("--uninstall", *_common(sibling, home))
    assert sibling_uninstall.returncode == 0, sibling_uninstall.stdout + sibling_uninstall.stderr

    skills_root = home / root / "skills"
    assert _installed_skill_names(skills_root) == _skill_names(), (
        "sibling uninstall deleted skills the legacy (ledger-less) profile still needs"
    )
    legacy_still_installed = _run("--is-installed", *_common(legacy, home))
    assert legacy_still_installed.returncode == 0, (
        legacy_still_installed.stdout + legacy_still_installed.stderr
    )

    # The legacy profile's own later uninstall completes the cleanup.
    legacy_uninstall = _run("--uninstall", *_common(legacy, home))
    assert legacy_uninstall.returncode == 0, legacy_uninstall.stdout + legacy_uninstall.stderr
    assert _installed_skill_names(skills_root) == []


@pytest.mark.parametrize(
    ("client", "root"),
    [("trae", ".trae"), ("trae-work", ".trae"), ("trae-cn", ".trae-cn"), ("trae-work-cn", ".trae-cn")],
)
def test_fresh_solo_apply_still_fully_uninstalls_without_a_never_installed_sibling(
    client: str,
    root: str,
    tmp_path: Path,
) -> None:
    # Regression guard: self-healing a legacy no-ledger root must not become a
    # blanket "always claim the sibling too" rule. A genuinely fresh install
    # (no pre-existing skills) must still fully uninstall on its own.
    home = tmp_path / "home"
    assert _run("--apply", *_common(client, home)).returncode == 0
    assert _run("--uninstall", *_common(client, home)).returncode == 0

    assert _installed_skill_names(home / root / "skills") == []
    assert _run("--is-installed", *_common(client, home)).returncode == 1


# --------------------------------------------------------------------------
# AQG-024 P1 (2026-09-03 post-fix independent re-verification): the legacy root
# above is only ever reached through a *sibling apply* first, so the fixed
# apply's self-heal always runs before any deletion. The real pre-024 upgrade
# path is a direct uninstall - a ledger-less shared root and a sibling profile
# that never ran the fixed apply, uninstalled straight away. That path reached
# the deletion branch with no legacy check at all and wiped the 16 skills the
# ledger-less profile still needed. Locked in both directions for both regions.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("legacy, uninstalled", SHARED_ROOT_PAIRS)
def test_legacy_shared_root_survives_a_direct_sibling_uninstall(
    legacy: str,
    uninstalled: str,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = SHARED_SKILL_ROOTS[legacy]
    skills_root = home / root / "skills"

    legacy_apply = _run("--apply", *_common(legacy, home))
    assert legacy_apply.returncode == 0, legacy_apply.stdout + legacy_apply.stderr

    # Exactly the pre-024 state: managed skills and their markers exist, the 024
    # ownership ledger does not. Unlink the precise dotfile - a glob that misses
    # it yields a fixture that cannot reproduce the defect and passes vacuously.
    owners_ledger = home / root / "managed-links" / ".aqg-agent-client-owners.json"
    assert owners_ledger.is_file(), "fixture assumption broken: apply always writes a ledger"
    owners_ledger.unlink()
    assert not owners_ledger.exists()

    # No fixed apply for the sibling first: it is uninstalled directly, which is
    # what a user upgrading a pre-024 host actually does.
    uninstall = _run("--uninstall", *_common(uninstalled, home))
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr

    assert _installed_skill_names(skills_root) == _skill_names(), (
        f"direct {uninstalled} uninstall deleted skills the ledger-less {legacy} still needs"
    )
    still_installed = _run("--is-installed", *_common(legacy, home))
    assert still_installed.returncode == 0, still_installed.stdout + still_installed.stderr

    # Conservative retention must not strand the skills forever: uninstalling
    # the profile that does hold the root still empties it.
    legacy_uninstall = _run("--uninstall", *_common(legacy, home))
    assert legacy_uninstall.returncode == 0, legacy_uninstall.stdout + legacy_uninstall.stderr
    assert _installed_skill_names(skills_root) == []
    assert _run("--is-installed", *_common(legacy, home)).returncode == 1


# --------------------------------------------------------------------------
# AQG-024 host-recovery blocker 3 (2026-09-03 incident report): TRAE CN
# project scope has no verified product evidence that Trae CN reads project
# config from a shared `.trae` directory rather than its own `.trae-cn`. The
# adapter must fail closed for trae-cn / trae-work-cn project scope instead
# of silently writing into the international Trae's project directory.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("client", ("trae-cn", "trae-work-cn"))
def test_trae_cn_project_scope_fails_closed_without_verified_evidence(
    client: str,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"

    apply = _run(
        "--apply",
        *_common(client, home, "--scope", "project", "--project-root", str(project)),
    )

    assert apply.returncode != 0, apply.stdout + apply.stderr
    assert "not verified" in (apply.stdout + apply.stderr).lower() or "fail" in (
        apply.stdout + apply.stderr
    ).lower()
    assert not (project / ".trae").exists()
    assert not (project / ".trae-cn").exists()


@pytest.mark.parametrize(
    ("ide", "work", "root"),
    [("trae", "trae-work", ".trae"), ("trae-cn", "trae-work-cn", ".trae-cn")],
)
def test_work_profile_never_touches_the_ide_hook_surface_it_shares(
    ide: str,
    work: str,
    root: str,
    tmp_path: Path,
) -> None:
    # R24-10: the Work profiles have no verified lifecycle-hook schema. Sharing a
    # skills root with the IDE must not let them add, rewrite, or strip hooks.
    home = tmp_path / "home"
    hooks_path = home / root / "hooks.json"

    assert _run("--apply", *_common(ide, home)).returncode == 0
    ide_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert "SessionStart" in ide_hooks

    work_apply = _run("--apply", *_common(work, home))
    assert work_apply.returncode == 0, work_apply.stdout + work_apply.stderr

    # The Work apply really did use the shared root and created no private one ...
    assert _installed_skill_names(home / root / "skills") == _skill_names()
    assert not list(home.glob(".trae-work*")), sorted(
        path.name for path in home.iterdir()
    )
    # ... and left the IDE's hook surface byte-identical.
    assert json.loads(hooks_path.read_text(encoding="utf-8")) == ide_hooks

    work_uninstall = _run("--uninstall", *_common(work, home))
    assert work_uninstall.returncode == 0, work_uninstall.stdout + work_uninstall.stderr
    assert json.loads(hooks_path.read_text(encoding="utf-8")) == ide_hooks
    assert _run("--verify", *_common(ide, home)).returncode == 0


# --------------------------------------------------------------------------
# AQG-024 host-recovery blocker 4 (2026-09-03 incident report): apply/uninstall
# for all four Trae profiles must merge into and precisely remove only
# AQG-owned hooks.json entries, never touching a hook a third-party tool put
# there first. The incident's own recovery had to hand-diff hooks.json because
# no candidate test proved this with a genuinely non-AQG fixture.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client", "root"),
    [
        ("trae", ".trae"),
        ("trae-work", ".trae"),
        ("trae-cn", ".trae-cn"),
        ("trae-work-cn", ".trae-cn"),
    ],
)
def test_apply_and_uninstall_preserve_a_third_party_hook_exactly(
    client: str,
    root: str,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config_root = home / root
    config_root.mkdir(parents=True)
    hooks_path = config_root / "hooks.json"
    third_party_hooks = {
        "PreToolUse": [
            {
                "matcher": "SomeOtherVendorTool",
                "hooks": [
                    {
                        "type": "command",
                        "command": "/opt/other-vendor/bin/scan.sh",
                    }
                ],
            }
        ],
        "SomeCustomEvent": [{"matcher": "", "hooks": [{"type": "command", "command": "echo hi"}]}],
    }
    hooks_path.write_text(json.dumps(third_party_hooks, indent=2) + "\n", encoding="utf-8")

    apply = _run("--apply", *_common(client, home))
    assert apply.returncode == 0, apply.stdout + apply.stderr

    after_apply = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert after_apply["PreToolUse"][0] == third_party_hooks["PreToolUse"][0], after_apply
    assert after_apply["SomeCustomEvent"] == third_party_hooks["SomeCustomEvent"], after_apply

    uninstall = _run("--uninstall", *_common(client, home))
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr

    after_uninstall = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert after_uninstall["PreToolUse"][0] == third_party_hooks["PreToolUse"][0], after_uninstall
    assert after_uninstall["SomeCustomEvent"] == third_party_hooks["SomeCustomEvent"], after_uninstall


def test_repeated_apply_on_a_shared_root_does_not_disturb_the_other_profile(
    tmp_path: Path,
) -> None:
    # R24-08: idempotence across profiles sharing one root.
    home = tmp_path / "home"
    skills_root = home / ".trae" / "skills"

    for client in ("trae", "trae-work", "trae", "trae-work"):
        apply = _run("--apply", *_common(client, home))
        assert apply.returncode == 0, apply.stdout + apply.stderr

    assert _installed_skill_names(skills_root) == _skill_names()
    for client in ("trae", "trae-work"):
        verify = _run("--verify", *_common(client, home))
        assert verify.returncode == 0, verify.stdout + verify.stderr
