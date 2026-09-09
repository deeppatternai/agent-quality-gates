"""One contract suite, run against every registered host adapter.

A contract with a single implementation is not a contract — it is one class with
a docstring. These cases are parameterized over every adapter in the lookup
table, so the guarantees `verify` claims are tested for each host rather than
for the first one written.

`_HOSTS` is the sandboxing seam: each adapter needs different temp-directory
arguments and patches a different installer module, which is precisely the
host-shaped knowledge that must NOT appear in `adapter_for`.
`test_every_registered_adapter_has_a_contract_fixture` pins the two lists
together, so adding an adapter without adding it here fails the suite instead of
silently exempting it.

One claim in this file cannot be tested by running adapters at all: that the
layers ABOVE them stay ignorant of host detail. A negative architectural claim
needs a check over the source, which `test_no_host_detail_leaks_above_the_adapter_layer`
performs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Dict, Optional

import pytest

from scripts.aqg_update import hosts
from scripts.aqg_update.hosts import base, claude_code, codex, generic
from scripts.aqg_update.hosts import managed

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_PKG = REPO_ROOT / "scripts" / "aqg_update"


@dataclass(frozen=True)
class HostFixture:
    """What the suite needs to drive one adapter inside a sandbox."""

    build: Callable[[Path], base.HostAdapter]
    #: The installer module whose root resolution this adapter uses, or None for
    #: a host with no hook surface (there is nothing to resolve a root for).
    installer: Optional[ModuleType]
    #: This host's config filename, or None when it has no hook surface.
    config_name: Optional[str]
    #: A syntactically valid config carrying no AQG hooks, or None as above.
    empty_config: Optional[str]
    #: What `verify` reports before anything is installed.
    unconfigured_status: str = "missing"

    @property
    def has_hook_surface(self) -> bool:
        return self.config_name is not None


_HOSTS: Dict[str, HostFixture] = {
    "claude-code": HostFixture(
        build=lambda tmp: claude_code.ClaudeCodeAdapter(
            settings_path=tmp / "settings.json", aqg_root=REPO_ROOT
        ),
        installer=claude_code.install_aqg_hooks,
        config_name="settings.json",
        empty_config=json.dumps({"hooks": {}}),
    ),
    "codex": HostFixture(
        build=lambda tmp: codex.CodexAdapter(
            hooks_path=tmp / "hooks.json", aqg_root=REPO_ROOT
        ),
        installer=codex.install_aqg_codex_hooks,
        config_name="hooks.json",
        empty_config=json.dumps({"hooks": {}}),
    ),
}

for _generic_id in generic.HOSTS_WITHOUT_HOOKS:
    _HOSTS[_generic_id] = HostFixture(
        build=(lambda cid: lambda tmp: generic.GenericAdapter(client_id=cid))(
            _generic_id
        ),
        installer=None,
        config_name=None,
        empty_config=None,
        unconfigured_status="not-applicable",
    )

# These share the neutral contract here; their JSON/TOML/extension-specific
# malformed/foreign/drift fixtures live in test_cross_agent_update.py. The old
# _HOOKED_IDS cases below explicitly use Claude/Codex constructor signatures.
for _managed_id in managed.FAMILIES:
    _HOSTS[_managed_id] = HostFixture(
        build=(lambda cid: lambda tmp: managed.ManagedAdapter(cid, home=tmp, aqg_root=REPO_ROOT))(_managed_id),
        installer=None, config_name=None, empty_config=None,
    )

_IDS = sorted(_HOSTS)
_HOOKED_IDS = sorted(cid for cid, f in _HOSTS.items() if f.has_hook_surface)


def _adapter(client_id: str, tmp_path: Path) -> base.HostAdapter:
    return _HOSTS[client_id].build(tmp_path)


def _tree_digest(root: Path) -> Dict[str, str]:
    """Path → content hash for every file under *root*, recursively."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- the suite covers every adapter ------------------------------------------


def test_every_registered_adapter_has_a_contract_fixture():
    """Without this, a new adapter would quietly skip the whole suite."""
    assert set(hosts.available_clients()) == set(_HOSTS)


@pytest.mark.parametrize("client_id", _IDS)
def test_lookup_returns_the_adapter_that_claims_the_id(client_id):
    assert hosts.adapter_for(client_id).client_id == client_id


# --- what verify reports ------------------------------------------------------


@pytest.mark.parametrize("client_id", _IDS)
def test_an_unconfigured_host_reports_its_delivery_baseline(client_id, tmp_path):
    """A host with a hook surface reports `missing`; one without reports
    `not-applicable`. Collapsing the two would tell a planner to install hooks
    on a host that has nowhere to put them."""
    evidence = _adapter(client_id, tmp_path).verify()
    assert evidence.client_id == client_id
    assert evidence.hooks_status == _HOSTS[client_id].unconfigured_status


@pytest.mark.parametrize("client_id", _HOOKED_IDS)
def test_a_config_that_is_not_json_reports_invalid(client_id, tmp_path):
    fixture = _HOSTS[client_id]
    (tmp_path / fixture.config_name).write_text("{not json", encoding="utf-8")
    evidence = _adapter(client_id, tmp_path).verify()
    assert evidence.hooks_status == "invalid"
    assert evidence.hooks_detail


@pytest.mark.parametrize("client_id", _HOOKED_IDS)
def test_valid_json_of_the_wrong_shape_reports_invalid(client_id, tmp_path):
    """A hand-edited config can parse and still be nonsense. Reporting that as
    `missing` would make the planner overwrite it without noticing."""
    fixture = _HOSTS[client_id]
    (tmp_path / fixture.config_name).write_text(
        json.dumps({"hooks": 42}), encoding="utf-8"
    )
    assert _adapter(client_id, tmp_path).verify().hooks_status == "invalid"


# --- what verify must never do ------------------------------------------------


@pytest.mark.parametrize("client_id", _HOOKED_IDS)
def test_verify_changes_no_file_content(client_id, tmp_path):
    """`verify` is the read-only verb: a dispatcher runs it to decide whether to
    act at all, so it must be safe on a host it will then leave alone.

    Hashed recursively, over a pre-populated config, because a name listing over
    an empty directory would miss an in-place rewrite — the mutation that
    actually matters here.
    """
    fixture = _HOSTS[client_id]
    (tmp_path / fixture.config_name).write_text(fixture.empty_config, encoding="utf-8")
    before = _tree_digest(tmp_path)
    _adapter(client_id, tmp_path).verify()
    assert _tree_digest(tmp_path) == before


# --- how verify reads install state -------------------------------------------


@pytest.mark.parametrize("client_id", _IDS)
def test_state_that_is_not_a_mapping_fails_closed(client_id, tmp_path):
    with pytest.raises(base.AdapterError, match="mapping"):
        _adapter(client_id, tmp_path).verify(state=["not", "a", "mapping"])


@pytest.mark.parametrize("client_id", _IDS)
def test_a_malformed_record_for_this_host_fails_closed(client_id, tmp_path):
    state = {"hosts": {client_id: {"last_applied_version": 14}}}
    with pytest.raises(base.AdapterError, match="last_applied_version"):
        _adapter(client_id, tmp_path).verify(state=state)


@pytest.mark.parametrize("client_id", _IDS)
def test_a_record_for_another_host_is_not_read_as_this_ones(client_id, tmp_path):
    """A mix-up would report one host's version for another and make the planner
    skip a host that is behind."""
    other = next(cid for cid in _IDS if cid != client_id)
    state = {"hosts": {other: {"last_applied_version": "0.14.0"}}}
    assert _adapter(client_id, tmp_path).verify(state=state).recorded_version is None


@pytest.mark.parametrize("client_id", _IDS)
def test_recorded_version_is_read_from_this_hosts_record(client_id, tmp_path):
    state = {"hosts": {client_id: {"last_applied_version": "0.14.0"}}}
    assert _adapter(client_id, tmp_path).verify(state=state).recorded_version == "0.14.0"


# --- AQG-side faults are never blamed on the host ------------------------------


@pytest.mark.parametrize("client_id", _HOOKED_IDS)
def test_an_unresolvable_aqg_root_is_the_adapters_fault(client_id, tmp_path, monkeypatch):
    monkeypatch.delenv("AQG_ROOT", raising=False)
    monkeypatch.setattr(
        _HOSTS[client_id].installer, "_resolve_aqg_root", lambda _=None: None
    )
    adapter = hosts.adapter_for(client_id)
    with pytest.raises(base.AdapterError, match="AQG root"):
        adapter.verify()


@pytest.mark.parametrize("client_id", _HOOKED_IDS)
def test_a_root_that_is_not_an_aqg_checkout_is_the_adapters_fault(client_id, tmp_path):
    """Guarding only `root is None` covered one representative of "the root is
    unusable", not the condition: a stale path that still exists would be
    reported as the HOST's config being invalid."""
    not_a_checkout = tmp_path / "somewhere-else"
    not_a_checkout.mkdir()
    fixture = _HOSTS[client_id]
    adapter = type(fixture.build(tmp_path))(
        **{
            "aqg_root": not_a_checkout,
            **(
                {"settings_path": tmp_path / fixture.config_name}
                if client_id == "claude-code"
                else {"hooks_path": tmp_path / fixture.config_name}
            ),
        }
    )
    with pytest.raises(base.AdapterError, match="AQG"):
        adapter.verify()


@pytest.mark.parametrize("client_id", _IDS)
def test_an_unmodelled_status_from_the_host_helper_fails_closed(
    client_id, tmp_path, monkeypatch
):
    """`_inspect` is the documented seam every adapter's `verify` routes
    through, so patching it is a contract-level operation, not a peek at a
    private detail."""
    adapter = _adapter(client_id, tmp_path)
    monkeypatch.setattr(
        adapter, "_inspect",
        lambda root=None: ("brand-new-status", "from a future helper"),
    )
    with pytest.raises(base.AdapterError, match="brand-new-status"):
        adapter.verify()


# --- the architectural claim, checked over the source --------------------------


def test_no_host_detail_leaks_above_the_adapter_layer():
    """The boundary's whole claim is about what the layers above do NOT know.

    No amount of running the adapters can show that, so this reads the source:
    outside `hosts/`, the update package must not import a host installer or
    name a host's configuration file or environment variable.
    """
    forbidden = (
        "install_aqg_hooks",
        "install_aqg_codex_hooks",
        "install_cursor_support",
        "settings.json",
        "hooks.json",
        "CODEX_HOME",
    )
    offenders = []
    for path in sorted(UPDATE_PKG.rglob("*.py")):
        if path.parent.name == "hosts":
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {token}")
    assert offenders == [], (
        "host-specific detail leaked above the adapter layer: " + "; ".join(offenders)
    )


# --- Codex-specific: the second format proves the boundary is real -------------


def _apply_codex_hooks(target: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "install_aqg_codex_hooks.py"),
            "--apply",
            "--target",
            str(target),
            "--aqg-root",
            str(REPO_ROOT),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        env={**os.environ, "AQG_BACKUP_DIR": str(target.parent / ".aqg-central")},
    )
    assert proc.returncode == 0, proc.stderr


def test_codex_reports_complete_after_a_real_install(tmp_path):
    _apply_codex_hooks(tmp_path / "hooks.json")
    assert _adapter("codex", tmp_path).verify().hooks_status == "complete"


def test_codex_reports_stale_when_a_hook_interpreter_is_gone(tmp_path):
    """Codex's own notion of drift, which Claude Code has no equivalent of —
    the reason the adapter wraps that helper instead of reimplementing it."""
    target = tmp_path / "hooks.json"
    _apply_codex_hooks(target)
    config = json.loads(target.read_text(encoding="utf-8"))
    event = next(iter(config["hooks"]))
    entry = config["hooks"][event][0]
    hook = entry["hooks"][0] if "hooks" in entry else entry
    hook["command"] = [str(tmp_path / "no-such-python"), "-c", "pass"]
    target.write_text(json.dumps(config), encoding="utf-8")
    assert _adapter("codex", tmp_path).verify().hooks_status == "stale"


def test_codex_reads_the_hooks_file_under_codex_home(tmp_path, monkeypatch):
    """The default location is Codex-shaped knowledge, and it belongs inside the
    adapter rather than in the lookup that constructs it.

    Asserting a NON-missing status: `missing` is also what an unpatched run
    would report on a machine with no Codex install, so it would not
    discriminate between reading the patched home and reading nothing.
    """
    codex_home = tmp_path / "codexhome"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert codex.CodexAdapter(aqg_root=REPO_ROOT).verify().hooks_status == "invalid"


# --- hosts with no hook surface ------------------------------------------------


def test_every_registry_client_declares_a_hook_delivery():
    """The registry is the source for which hosts have a hook surface; a client
    added without saying must fail at import, not default into one answer."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import aqg_client_registry as reg

    for client_id, spec in reg.CLIENT_REGISTRY.items():
        assert spec.hook_delivery in reg.HOOK_DELIVERIES, client_id


def test_an_invalid_hook_delivery_is_refused_by_the_registry_validator():
    import sys
    from dataclasses import replace

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import aqg_client_registry as reg

    bad = replace(reg.CLIENT_REGISTRY["codex"], hook_delivery="whatever")
    with pytest.raises(ValueError, match="hook_delivery"):
        reg.validate_registry([bad])


def test_the_generic_adapter_serves_exactly_the_registry_hosts_without_hooks():
    """Drift here would either silently drop a host from updates or claim a host
    has no hooks when it does."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import aqg_client_registry as reg

    from_registry = {
        cid
        for cid, spec in reg.CLIENT_REGISTRY.items()
        if spec.hook_delivery == "none"
    }
    assert set(generic.HOSTS_WITHOUT_HOOKS) == from_registry


def test_a_generic_adapter_refuses_a_host_that_does_have_hooks():
    """Constructing one for a hooked host would report `not-applicable` and make
    the planner skip real hook work."""
    with pytest.raises(base.AdapterError, match="claude-code"):
        generic.GenericAdapter(client_id="claude-code")


def test_a_generic_adapter_refuses_an_unknown_client():
    with pytest.raises(base.AdapterError, match="no-such-host"):
        generic.GenericAdapter(client_id="no-such-host")


def test_evidence_refuses_an_empty_client_id():
    """The per-instance client id of a multi-host adapter still has to be real."""
    with pytest.raises(base.AdapterError, match="client_id"):
        base.Evidence(
            client_id="", hooks_status="missing", hooks_detail="", recorded_version=None
        )


# --- fixes from audit aud_rwf1dg_r5czWNAAO -----------------------------------


def test_a_generic_adapter_reassigned_to_a_hooked_host_still_refuses():
    """The construction-time check protects the constructor, not the invariant:
    `client_id` is a public attribute, and a reassigned instance would report
    `not-applicable` for a host that does take hooks."""
    adapter = generic.GenericAdapter(client_id=generic.HOSTS_WITHOUT_HOOKS[0])
    adapter.client_id = "claude-code"
    with pytest.raises(base.AdapterError, match="claude-code"):
        adapter.verify()


def test_hook_delivery_agrees_with_the_prose_on_the_same_spec():
    """A transcription check, not a correctness oracle.

    The twenty values were derived by hand from each spec's `hooks_surface`
    prose, so the realistic failure is a mistyped value, not a misjudged host.
    This catches the former. It cannot catch prose that is itself wrong, and
    `installer_command` was tested as a stronger oracle and rejected: eleven of
    the thirteen managed-merge hosts install hooks through a generic script
    whose command string never mentions hooks, so it cannot tell them from the
    six that have none.
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import aqg_client_registry as reg

    for client_id, spec in reg.CLIENT_REGISTRY.items():
        prose_says_none = reg.hooks_surface_denies_a_surface(spec.hooks_surface)
        assert prose_says_none == (spec.hook_delivery == "none"), client_id


def test_the_registry_validator_rejects_a_value_its_prose_contradicts():
    import sys
    from dataclasses import replace

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import aqg_client_registry as reg

    bad = replace(reg.CLIENT_REGISTRY["zed"], hook_delivery="managed-merge")
    with pytest.raises(ValueError, match="hooks_surface"):
        reg.validate_registry([bad])


def test_the_adapter_table_refuses_a_duplicate_registration():
    """Last-key-wins would silently replace a dedicated adapter with the generic
    one, and the coverage test could not see it."""
    with pytest.raises(hosts.AdapterError, match="claude-code"):
        hosts.build_adapter_table(
            explicit={"claude-code": claude_code.ClaudeCodeAdapter},
            generic_ids=("claude-code",),
        )
