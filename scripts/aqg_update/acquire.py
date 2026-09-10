"""Turn a remote into a version this machine is willing to run.

docs/UPDATE_ARCHITECTURE.md §9. This is the last gate before
``transaction.apply_plan`` makes a version live, and after it there is no human
left to ask. So the shape here is: refuse, in a fixed order, and treat the
ordinary answer — *nothing new* — separately from a failed check.

**The ordering that makes the rest safe.** The release is verified **out of
git's object store**, before any working tree exists:

1. fetch the channel ref, and read the two release documents with ``cat-file``
   — Git objects may be written, but nothing is checked out or activated;
2. verify the signature over the manifest, against the **pinned** keyring;
3. fetch the commit the manifest names, and check it is the commit we have;
4. hash every blob in that commit and compare the roster both ways.

Only after all four does a caller stage a worktree. Two things follow from
verifying the object store rather than a checkout. A rejected release never
creates an executable checkout; fetched Git objects may remain on disk. These
read-size gates are not transport disk quotas. The check never needs an exception for the ``.git`` file
that a ``git worktree`` necessarily contains: the audit of
``internal/release/manifest.py`` rated such an exception critical, because
``.git/hooks/`` is executable code, and the way to not need one is to not look
at a working tree.

**The documents live outside the tree they describe**, on ``refs/aqg-release/
<channel>``. A manifest committed into the release would have to cover itself,
and a roster that excludes its own manifest is a roster with a hole in it.

Not here: applying anything. This module fetches, verifies, and reports. It
writes nothing but git objects, and it never mutates a host's configuration.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

try:  # invoked as a package (tests, `python3 -m`)
    from scripts.aqg_update import trust
except ImportError:  # invoked with scripts/ itself on sys.path
    from aqg_update import trust  # type: ignore[no-redef]

#: The ref a channel's release documents live on, at the remote. Nothing is
#: fetched *into* a local ref of this name: see FETCH_HEAD below.
RELEASE_REF_PREFIX = "refs/aqg-release"

#: Where a release that has passed every check is pinned. One name, reused, so
#: the count is bounded at one — and it exists at all only so that a `git gc`
#: between verifying a commit and staging it cannot reclaim it.
VERIFIED_REF = "refs/aqg-release/verified"

#: A channel name is interpolated into a refname. Restricted to a plain word:
#: a slash reaches into another namespace, and `commits` once collided with one.
_CHANNEL_RE = re.compile(r"\A[a-z][a-z0-9-]{0,30}\Z")

#: git options applied to every invocation. `protocol.ext.allow=never` keeps a
#: remote from steering the transport into an arbitrary helper program;
#: `transfer.fsckobjects=true` makes malformed objects fail on arrival rather
#: than enter the store and be trusted later.
_GIT_HARDENING = (
    "-c", "protocol.ext.allow=never",
    "-c", "transfer.fsckobjects=true",
)

MANIFEST_BLOB = "manifest.json"
SIGNATURE_BLOB = "signature.json"

#: These arrive from the network before anything about them is known. A cap is
#: the only thing between a hostile remote and this process's memory; the
#: manifest for a repository of a few thousand files is well under a megabyte.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

#: The same problem one layer down. A signed manifest names a commit, and that
#: commit's blobs are attacker-chosen if the signing key is; more to the point,
#: this runs unattended at session start, where a process that grows to
#: gigabytes is a machine that stops responding. Sizes are read with
#: `--batch-check`, which returns them WITHOUT the contents, so an oversized
#: tree is refused before a byte of it is held.
MAX_BLOB_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 256 * 1024 * 1024

#: git is given a bounded time. A hung fetch at session start is indistinguishable
#: from a slow one, and neither may hold up a shell.
FETCH_TIMEOUT_SECONDS = 60
LOCAL_TIMEOUT_SECONDS = 120

#: Tree entry modes git may report. Anything else is refused rather than
#: interpreted: 120000 is a symlink, which git will check out pointing anywhere
#: on the machine, and 160000 is a submodule, which is a second repository this
#: manifest says nothing about.
_MODE_FILE = "100644"
_MODE_EXEC = "100755"
_MODE_SYMLINK = "120000"
_MODE_SUBMODULE = "160000"


class AcquireError(RuntimeError):
    """A refusal to accept a release. Always fail-closed."""


class GitReadError(AcquireError):
    """An operational Git failure, eligible for one bounded retry."""


def _git_failure(operation: str, code: int, stderr: bytes) -> GitReadError:
    # Never persist arbitrary stderr: URLs, headers and local paths can carry
    # secrets. Categories are diagnostic hints, not proof of a root cause.
    lowered = stderr.lower()
    category = "git-error"
    for label, patterns in (
        ("dns", (b"could not resolve", b"name resolution")),
        ("timeout", (b"timed out", b"timeout")),
        ("tls", (b"certificate", b"ssl", b"tls")),
        ("auth", (b"authentication", b"could not read username", b"401", b"403")),
        ("missing-ref", (b"couldn't find remote ref", b"not our ref")),
        ("permission", (b"permission denied", b"access is denied")),
        ("lock", (b"cannot lock", b"index.lock")),
        ("connection", (b"connection", b"unable to access")),
        ("object-read", (b"bad object", b"unable to read", b"invalid object")),
    ):
        if any(pattern in lowered for pattern in patterns):
            category = label
            break
    return GitReadError(f"git {operation} failed: exit={code}, category={category}")


def _retry(operation):
    for attempt in range(2):
        try:
            return operation()
        except GitReadError:
            if attempt:
                raise
            time.sleep(1)


def _required_git(repo: Path, *args: str, timeout: int = LOCAL_TIMEOUT_SECONDS):
    def once():
        done = _git(repo, *args, timeout=timeout)
        if done.returncode:
            raise _git_failure(args[0], done.returncode, done.stderr)
        return done
    return _retry(once)


@dataclass(frozen=True)
class AvailableRelease:
    manifest: Mapping[str, Any]
    commit: str
    key_id: str
    release_sequence: int


def _git(
    repo: Path, *args: str, timeout: int = LOCAL_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *_GIT_HARDENING, "-C", str(repo), *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitReadError(f"git {args[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise GitReadError(f"git {args[0]} could not start: category=os-error") from exc


def _require_channel(channel: Any) -> str:
    if not isinstance(channel, str) or not _CHANNEL_RE.match(channel):
        raise AcquireError(
            f"channel {channel!r} is not a plain lowercase word; it is "
            f"interpolated into a refname, where a slash reaches into another "
            f"namespace")
    if channel == 'verified':
        raise AcquireError('channel is reserved for the local verified pin')
    return channel


def _fetch_args(remote: str, refspec: str):
    if not isinstance(remote, str) or not remote or remote.startswith('-'):
        raise AcquireError('remote must be a nonempty repository operand, not a Git option')
    # Ignore configured opportunistic ref mappings; write only FETCH_HEAD.
    return ('fetch', '--quiet', '--no-tags', '--refmap=', '--', remote, refspec)


def _fetch(repo: Path, remote: str, refspec: str) -> bool:
    """Legacy release-cutter helper: False on a completed nonzero Git exit.

    Launch/timeout errors still raise AcquireError, as before. Automatic checks
    must use the required path so unavailable releases cannot look current.
    """
    done = _git(
        repo, *_fetch_args(remote, refspec),
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    return done.returncode == 0


def _read_blob(repo: Path, spec: str, *, required: bool = False) -> Optional[bytes]:
    """Read one blob, size-checked, or ``None`` if it is not there."""
    reader = _required_git if required else _git
    sized = reader(repo, "cat-file", "-s", spec)
    if sized.returncode != 0:
        return None
    try:
        size = int(sized.stdout.decode("ascii", "replace").strip())
    except ValueError as exc:
        raise AcquireError(f"git reported an unreadable size for {spec}") from exc
    if size > MAX_DOCUMENT_BYTES:
        raise AcquireError(
            f"{spec} is too large ({size} bytes, limit {MAX_DOCUMENT_BYTES}); "
            f"refusing to read a document of unbounded size from a remote"
        )
    body = reader(repo, "cat-file", "blob", spec)
    if body.returncode != 0:
        return None
    return body.stdout


def _load_document(repo: Path, spec: str, label: str, *, required: bool = False) -> Optional[Dict[str, Any]]:
    raw = _read_blob(repo, spec, required=required)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AcquireError(f"the release {label} is not readable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AcquireError(f"the release {label} is not a JSON object")
    return payload


def commit_roster(repo: Path, commit: str) -> Dict[str, Dict[str, Any]]:
    """Hash every blob in *commit*, straight out of the object store.

    One ``cat-file --batch`` for the whole tree rather than a process per file:
    a thousand files is one pipe and a fraction of a second, and a thousand
    forks is neither.
    """
    listing = _required_git(repo, "ls-tree", "-r", "-z", commit)

    entries = []
    for record in listing.stdout.split(b"\0"):
        if not record:
            continue
        try:
            meta, raw_path = record.split(b"\t", 1)
            mode, kind, sha = meta.split()
        except ValueError as exc:
            raise AcquireError("git produced an unreadable tree listing") from exc
        path = raw_path.decode("utf-8", "surrogateescape")
        mode_text = mode.decode("ascii", "replace")
        if mode_text == _MODE_SYMLINK:
            raise AcquireError(
                f"{path}: is a symlink in the release commit; git would check it "
                f"out pointing anywhere on this machine")
        if mode_text == _MODE_SUBMODULE:
            raise AcquireError(
                f"{path}: is a submodule; it is a second repository this manifest "
                f"says nothing about")
        if kind != b"blob" or mode_text not in (_MODE_FILE, _MODE_EXEC):
            raise AcquireError(f"{path}: unexpected tree entry {mode_text} {kind!r}")
        entries.append((path, sha.decode("ascii"), mode_text == _MODE_EXEC))

    if not entries:
        raise AcquireError(f"{commit} contains no files")

    # A commit may hold both `a` and `A`; a case-folding filesystem — macOS by
    # default — cannot. The checkout then produces fewer files than the roster
    # names, so verification fails on the user's machine while passing on Linux,
    # and which of the two survives is not something a signature can pin.
    #
    # Checked here, in the function BOTH sides use, so a release carrying such a
    # pair is refused when it is signed rather than when it is installed.
    folded: Dict[str, str] = {}
    for path, _sha, _executable in entries:
        key = path.casefold()
        if key in folded:
            raise AcquireError(
                f"{path}: differs from {folded[key]!r} only by case; on a folding "
                f"filesystem they are one file, so this commit cannot be checked "
                f"out completely and no signature over it is installable there")
        folded[key] = path

    return _hash_blobs(repo, entries)


def _gate_sizes(repo: Path, entries) -> None:
    """Refuse an oversized tree before a byte of its contents is held.

    ``--batch-check`` returns the header line for each object and **not** the
    contents, so the whole tree can be measured for the cost of its object
    names. Measuring after reading would be a check performed on the far side
    of the damage it exists to prevent.
    """
    checked = _batch(repo, ["cat-file", "--batch-check"], entries)
    total = 0
    lines = checked.split(b"\n")
    if len(lines) < len(entries):
        raise AcquireError("git returned fewer sizes than objects asked for")
    for (path, sha, _), line in zip(entries, lines):
        header = line.split()
        if len(header) != 3 or header[1] != b"blob":
            raise AcquireError(f"{path}: git did not report a blob for {sha}")
        try:
            size = int(header[2])
        except ValueError as exc:
            raise AcquireError(f"{path}: git reported an unreadable blob size") from exc
        if size > MAX_BLOB_BYTES:
            raise AcquireError(
                f"{path}: is too large ({size} bytes, limit {MAX_BLOB_BYTES}); "
                f"this runs unattended at session start and may not be made to "
                f"hold a tree of a remote's choosing")
        total += size
        if total > MAX_TREE_BYTES:
            raise AcquireError(
                f"the release tree is too large (over {MAX_TREE_BYTES} bytes)")


def _batch(repo: Path, args, entries) -> bytes:
    return _retry(lambda: _batch_once(repo, args, entries))


def _batch_once(repo: Path, args, entries) -> bytes:
    try:
        child = subprocess.Popen(
            ["git", *_GIT_HARDENING, "-C", str(repo), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise GitReadError("git cat-file could not start: category=os-error") from exc
    try:
        out, err = child.communicate(
            b"\n".join(sha.encode("ascii") for _, sha, _ in entries) + b"\n",
            timeout=LOCAL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        child.kill()
        child.communicate()
        raise GitReadError("git cat-file timed out reading the release tree") from exc
    if child.returncode != 0:
        raise _git_failure("cat-file batch", child.returncode, err)
    return out


def _hash_blobs(repo: Path, entries) -> Dict[str, Dict[str, Any]]:
    """Hash every blob, with the whole tree already known to fit.

    ``--batch`` frames each object as ``<sha> blob <size>\n<contents>\n`` and
    appends that final newline whatever the blob's own bytes end with, which is
    why the stride below is ``size + 1``. Checked against blobs with and
    without a trailing newline, empty, and binary — an auditor read this as
    assuming the blob supplies the newline, and the tests at the time happened
    to use only blobs that did.
    """
    _gate_sizes(repo, entries)
    out = _batch(repo, ["cat-file", "--batch"], entries)

    roster: Dict[str, Dict[str, Any]] = {}
    offset = 0
    for path, sha, executable in entries:
        newline = out.find(b"\n", offset)
        if newline < 0:
            raise AcquireError("git cat-file returned a truncated batch")
        header = out[offset:newline].split()
        if len(header) != 3 or header[1] != b"blob":
            raise AcquireError(f"{path}: git did not return a blob for {sha}")
        try:
            size = int(header[2])
        except ValueError as exc:
            raise AcquireError(f"{path}: git reported an unreadable blob size") from exc
        body = out[newline + 1: newline + 1 + size]
        if len(body) != size:
            raise AcquireError(f"{path}: git returned a short blob")
        roster[path] = {
            "sha256": hashlib.sha256(body).hexdigest(),
            "executable": executable,
        }
        offset = newline + 1 + size + 1
    return roster


def verify_commit_tree(repo: Path, commit: str, manifest: Mapping[str, Any]) -> None:
    """Raise unless *commit* is exactly the tree *manifest* describes.

    Both directions, for the same reason ``verify_tree`` checks both: every
    covered file must match, **and** the commit must hold nothing the roster
    does not name — an added file leaves every covered digest intact while
    putting code nobody signed into the tree about to become the live install.
    """
    expected = manifest.get("files")
    if not isinstance(expected, Mapping) or not expected:
        raise AcquireError("the release manifest carries no file roster")
    actual = commit_roster(repo, commit)

    for path in sorted(set(actual) - set(expected)):
        raise AcquireError(f"{path}: is in the release commit but not in the manifest")
    for path in sorted(set(expected) - set(actual)):
        raise AcquireError(f"{path}: is in the manifest but not in the release commit")
    for path in sorted(expected):
        entry = expected[path]
        if not isinstance(entry, Mapping):
            raise AcquireError(f"{path}: manifest entry is not an object")
        if actual[path]["sha256"] != entry.get("sha256"):
            raise AcquireError(f"{path}: contents do not match the manifest")
        if actual[path]["executable"] != entry.get("executable"):
            raise AcquireError(f"{path}: executable bit does not match the manifest")


def available_release(
    repo: Path,
    *,
    remote: str,
    channel: str,
    keyring: Mapping[str, Any],
    installed_sequence: Union[int, "trust._FirstInstall"],
) -> Optional[AvailableRelease]:
    """Return the release this machine should move to, or ``None``.

    ``None`` means a verified release does not advance the sequence.
    Fetch or document-read failures raise, so the runner records a failed
    check and uses its failure retry interval. A signature
    that does not verify is not "no update available", and reporting it as one
    is how a rejected release becomes a silent one.
    """
    repo = Path(repo)
    channel = _require_channel(channel)
    remote_ref = f"{RELEASE_REF_PREFIX}/{channel}"
    # No destination refspec: the result lands in FETCH_HEAD and no local ref is
    # created. A rejected release therefore cannot leave a poisoned ref behind,
    # which is what the previous `+ref:ref` did before anything was verified.
    _required_git(repo, *_fetch_args(remote, remote_ref),
                  timeout=FETCH_TIMEOUT_SECONDS)
    # Resolve once: both documents must come from the same object even if
    # another Git user overwrites FETCH_HEAD between our reads.
    metadata = _required_git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
    metadata_commit = metadata.stdout.decode("ascii", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", metadata_commit):
        raise AcquireError("git returned an invalid release metadata commit")
    manifest = _load_document(repo, f"{metadata_commit}:{MANIFEST_BLOB}", "manifest", required=True)
    signature = _load_document(repo, f"{metadata_commit}:{SIGNATURE_BLOB}", "signature", required=True)

    signature_text = signature.get("signature")
    if not isinstance(signature_text, str):
        raise AcquireError("the release signature document carries no signature")
    signature_key_id = signature.get("key_id")
    if signature_key_id is not None and not isinstance(signature_key_id, str):
        raise AcquireError("the release signature document names a non-string key")

    try:
        verified = trust.verify_manifest(
            manifest,
            signature=signature_text,
            keyring=keyring,
            channel=channel,
            installed_sequence=installed_sequence,
            signature_key_id=signature_key_id,
        )
    except trust.SequenceNotAdvanced:
        # Already current: the ordinary answer on almost every check. A typed
        # exception rather than a match on the message, so rewording the
        # verifier cannot turn this into a session-start error or swallow a
        # real rejection that happens to read alike.
        return None
    except trust.TrustError as exc:
        raise AcquireError(f"the release on {channel!r} was refused: {exc}") from exc

    commit = verified.manifest["commit"]
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise AcquireError("the release manifest must name a full lowercase commit ID")
    _required_git(repo, *_fetch_args(remote, commit),
                  timeout=FETCH_TIMEOUT_SECONDS)

    resolved = _required_git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if resolved.stdout.decode("ascii", "replace").strip() != commit:
        raise AcquireError(
            f"the manifest names {commit} but git resolved it to something else")

    verify_commit_tree(repo, commit, verified.manifest)

    # Pinned only now, and only here. Until this line the commit is reachable
    # solely from FETCH_HEAD, which the next fetch overwrites; after it, one ref
    # keeps the verified commit alive until a caller has staged it.
    pinned = _git(repo, "update-ref", "--no-deref", VERIFIED_REF, commit)
    if pinned.returncode != 0:
        raise _git_failure("update-ref", pinned.returncode, pinned.stderr)

    return AvailableRelease(
        manifest=verified.manifest,
        commit=commit,
        key_id=verified.key_id,
        release_sequence=verified.manifest["release_sequence"],
    )
