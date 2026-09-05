"""Verify that a release manifest was signed by a key AQG already trusts.

docs/UPDATE_ARCHITECTURE.md §9. Once an update applies without a human
confirming it, **the signature is the consent**. Everything here is therefore
about refusing: a manifest altered after signing, a signature by an untrusted or
revoked key, a correctly signed release for the wrong channel, and a correctly
signed *older* release — which is what a rollback attack replays.

**Stdlib only, on purpose.** This code ships to every user, and AQG's single
runtime dependency is PyYAML; adding a crypto library to the install would be a
real cost paid by everyone. The consequence is that the RSA check is written
out by hand, which is precisely the code where a subtle mistake becomes a
forgery — so it is **ported from DE's `release_contract.py` rather than
re-derived**, including the property that makes it safe:

    It reconstructs the WHOLE expected PKCS#1 v1.5 block and compares it in
    constant time. It never parses what came out of the exponentiation.

That distinction is the difference between a correct verifier and a
Bleichenbacher-forgeable one. A verifier that *parses* the recovered block —
skipping padding to find the digest — accepts blocks with trailing garbage,
which is forgeable for small exponents. Rebuilding and comparing admits exactly
one byte string.

**A limitation, stated rather than implied.** The keyring is pinned into the
product, so a key it does not contain can never sign a release. The converse is
not true: a key it *does* contain and that is later compromised can only be
revoked by shipping a new keyring — through the very channel the compromised key
controls. Local `revoked` flags protect an install that already has the
revocation, and nothing here protects one that does not. Closing that needs a
second root or a threshold scheme; it is an open decision in
docs/UPDATE_ARCHITECTURE.md §14, not something this module can decide.

Not here: producing a signature. The signing side lives outside the shipped
tree, in `internal/release/`, and this module has no code path that touches a
private key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Union

#: ASN.1 DigestInfo prefix for SHA-256, per RFC 8017 §9.2 notes.
DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")

SUPPORTED_ALGORITHM = "rsa-pkcs1v15-sha256"
KEYRING_SCHEMA = 1

#: The manifest schema this verifier implements. Pinned for the same reason the
#: keyring's is: a signed manifest of a later schema may redefine what `channel`
#: or `release_sequence` mean, and this code would apply the old meanings to it
#: without anyone being asked.
MANIFEST_SCHEMA = 1

#: A *strength* floor, not a capacity one. The encoded block needs 51 bytes of
#: DigestInfo-plus-digest, three framing bytes and at least eight of padding —
#: 62 bytes, about 496 bits — so blocks form far below this. Capacity is checked
#: separately by the `padding_len < 8` guard in `_verify_rsa`, which no key that
#: survives `load_keyring` can reach. 2048 is where we stop accepting the key.
MINIMUM_MODULUS_BITS = 2048

#: Unbounded is not the same as large. `pow()` against a megabit modulus does
#: not fail, it runs — and an update that hangs is one that never lands.
MAXIMUM_MODULUS_BITS = 8192

#: Real RSA public exponents are 3, 17 or 65537. Anything needing more than this
#: is not a key, it is a cost.
MAXIMUM_EXPONENT_BITS = 64

#: A manifest nested deeper than this is not a manifest.
MAXIMUM_NESTING_DEPTH = 32

_CANONICAL_HEX = re.compile(r"\A[0-9a-f]+\Z")

_REQUIRED_MANIFEST_FIELDS = (
    "schema",
    "channel",
    "version",
    "commit",
    "release_sequence",
    "key_id",
)


class _FirstInstall:
    """The explicit spelling of "there is no installed sequence yet".

    A sentinel rather than ``None`` so that a caller whose state read failed, and
    which therefore holds ``None``, cannot silently disable the anti-rollback
    check by handing it over as if it meant "first install".
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "FIRST_INSTALL"


#: Pass this, deliberately, when nothing is installed yet.
FIRST_INSTALL = _FirstInstall()


class TrustError(RuntimeError):
    """A refusal to trust a release. Always fail-closed."""


class SequenceNotAdvanced(TrustError):
    """The release is correctly signed and is not newer than what is installed.

    A distinct type because a caller has to tell this apart from every other
    refusal: *this* one is the ordinary answer on almost every check, and the
    rest are rejections. Distinguishing them by matching the message text made
    a rewording here able to turn a routine no-op into an error at session
    start, and able to swallow a real rejection that happened to contain the
    same phrase.
    """


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    modulus: int
    exponent: int
    revoked: bool


@dataclass(frozen=True)
class VerifiedRelease:
    manifest: Mapping[str, Any]
    key_id: str


def _json_only(value: Any, *, path: str = "manifest", depth: int = 0) -> Any:
    """Deep-copy *value*, refusing anything outside the JSON data model.

    Sorting keys is not canonicalization. Python's encoder will happily coerce a
    non-string key — ``{1: "x"}`` and ``{"1": "x"}`` produce identical bytes —
    serialize a tuple as a list, and emit the non-JSON tokens ``NaN`` and
    ``Infinity``. Each of those lets two distinct payloads share one signature,
    or produces bytes a second implementation cannot reproduce. The type set has
    to be narrowed before serializing, not after.
    """
    if depth > MAXIMUM_NESTING_DEPTH:
        raise TrustError(f"{path}: nested deeper than {MAXIMUM_NESTING_DEPTH} levels")
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TrustError(
                f"{path}: {value!r} is not representable in JSON; it would "
                f"serialize to a token no other implementation can parse")
        return value
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TrustError(
                    f"{path}: object keys must be strings, not {type(key).__name__}; "
                    f"a coerced key makes two payloads share one signature")
            out[key] = _json_only(item, path=f"{path}.{key}", depth=depth + 1)
        return out
    if isinstance(value, list):
        return [
            _json_only(item, path=f"{path}[{i}]", depth=depth + 1)
            for i, item in enumerate(value)
        ]
    raise TrustError(
        f"{path}: {type(value).__name__} is not a JSON type; only objects, "
        f"arrays, strings, finite numbers, booleans and null may be signed")


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes that get signed and verified.

    Sorted keys and fixed separators, so two spellings of the same manifest
    produce one signature — otherwise a re-serialization anywhere between
    signing and verifying breaks every release.
    """
    plain = _json_only(payload)
    try:
        return json.dumps(
            plain,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise TrustError(f"manifest cannot be canonicalized: {exc}") from exc


def _freeze(value: Any) -> Any:
    """Make a verified snapshot unwritable, all the way down.

    A frozen dataclass stops its fields being reassigned; it does nothing to the
    dict a field points at. Without this, `VerifiedRelease` asserts that some
    bytes were signed while the object it hands back can be edited into
    different ones.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def load_keyring(document: Mapping[str, Any]) -> Dict[str, TrustedKey]:
    """Parse the pinned public key set, or raise.

    The keyring ships with the product and is never fetched: a key set an
    attacker can supply is not a trust anchor.
    """
    if not isinstance(document, Mapping):
        raise TrustError("keyring must be a JSON object")
    schema = document.get("schema")
    if schema != KEYRING_SCHEMA:
        raise TrustError(
            f"keyring schema {schema!r} is not the one this AQG understands "
            f"({KEYRING_SCHEMA})"
        )
    entries = document.get("keys")
    if not isinstance(entries, list) or not entries:
        raise TrustError("keyring contains no keys; nothing could ever be trusted")

    keys: Dict[str, TrustedKey] = {}
    seen_material: Dict[Any, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TrustError("keyring entries must be JSON objects")
        key_id = entry.get("key_id")
        if not isinstance(key_id, str) or not key_id.strip():
            raise TrustError("keyring entry has no key_id")
        if key_id in keys:
            raise TrustError(f"keyring contains a duplicate key_id: {key_id}")
        algorithm = entry.get("algorithm")
        if algorithm != SUPPORTED_ALGORITHM:
            raise TrustError(
                f"{key_id}: unsupported algorithm {algorithm!r}; this verifier "
                f"implements only {SUPPORTED_ALGORITHM}"
            )
        modulus = _require_hex_int(entry.get("modulus_hex"), key_id, "modulus_hex")
        if modulus % 2 == 0:
            raise TrustError(
                f"{key_id}: modulus is even, so it is not an RSA modulus")
        if modulus.bit_length() < MINIMUM_MODULUS_BITS:
            raise TrustError(
                f"{key_id}: modulus is {modulus.bit_length()} bits; the minimum "
                f"strength this AQG accepts is {MINIMUM_MODULUS_BITS}"
            )
        if modulus.bit_length() > MAXIMUM_MODULUS_BITS:
            raise TrustError(
                f"{key_id}: modulus is {modulus.bit_length()} bits, above the "
                f"{MAXIMUM_MODULUS_BITS} ceiling; verifying against it would run "
                f"rather than fail"
            )
        exponent = entry.get("exponent")
        if not isinstance(exponent, int) or isinstance(exponent, bool):
            raise TrustError(f"{key_id}: exponent must be an integer")
        if exponent < 3 or exponent % 2 == 0:
            raise TrustError(
                f"{key_id}: exponent must be an odd integer of at least 3")
        if exponent >= modulus:
            raise TrustError(
                f"{key_id}: exponent must be smaller than the modulus")
        if exponent.bit_length() > MAXIMUM_EXPONENT_BITS:
            raise TrustError(
                f"{key_id}: exponent is {exponent.bit_length()} bits, above the "
                f"{MAXIMUM_EXPONENT_BITS} ceiling")
        revoked = entry.get("revoked", False)
        if not isinstance(revoked, bool):
            raise TrustError(f"{key_id}: 'revoked' must be a boolean")
        # Revoking an id does not revoke a key. Two ids over one modulus means a
        # revocation can be walked around by naming the live alias, while the
        # private key behind both is the same compromised one.
        material = (modulus, exponent)
        if material in seen_material:
            raise TrustError(
                f"{key_id}: the same key material is already in the keyring under "
                f"{seen_material[material]!r}; an alias makes revoking either one "
                f"cosmetic"
            )
        seen_material[material] = key_id
        keys[key_id] = TrustedKey(
            key_id=key_id, modulus=modulus, exponent=exponent, revoked=revoked
        )
    return keys


def default_keyring_path() -> Path:
    """Where the pinned keyring ships: beside this module, inside the checkout.

    Beside the verifier on purpose. A keyring that lived outside the tree the
    release signature covers would be a trust anchor the signature does not
    protect, which is the opposite of what pinning is for.
    """
    return Path(__file__).resolve().parent / "release-trust.json"


def load_trusted_keys(path: Optional[Path] = None) -> Dict[str, TrustedKey]:
    """Load the keyring this build ships with, or refuse.

    An absent keyring is not an empty one. A build with no pinned keys can
    verify nothing, so it must refuse to update rather than fall back to
    trusting whatever a remote hands it — and it must say which of the two it
    is, because "no keyring" and "bad signature" call for different actions.
    """
    target = Path(path) if path is not None else default_keyring_path()
    try:
        raw = target.read_bytes()
    except FileNotFoundError as exc:
        raise TrustError(
            f"this AQG build has no pinned release keyring at {target}; managed "
            f"updates are disabled until one ships") from exc
    except OSError as exc:
        raise TrustError(f"cannot read the release keyring at {target}: {exc}") from exc
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TrustError(f"the release keyring at {target} is unreadable: {exc}") from exc
    return load_keyring(document)


def _require_hex_int(value: Any, key_id: str, field: str) -> int:
    """Parse one spelling of a hex integer and refuse the rest.

    ``int(s, 16)`` accepts a sign, an ``0x`` prefix, underscores and surrounding
    whitespace. The sign is the one that matters: a negative modulus clears the
    bit-length floor, because ``int.bit_length()`` ignores the sign, and then
    makes every signature fail — a trust anchor that bricks verification instead
    of refusing to load.
    """
    if not isinstance(value, str) or not value:
        raise TrustError(f"{key_id}: {field} must be a hex string")
    if not _CANONICAL_HEX.match(value):
        raise TrustError(
            f"{key_id}: {field} must be lowercase hex digits only, with no sign, "
            f"prefix, underscore or whitespace")
    return int(value, 16)


def _validated_manifest(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a validated deep copy — never the caller's own object.

    Verifying the caller's dict and handing it back means the bytes that were
    checked and the bytes the caller holds are the same mutable object, so
    "verified" stops being true the moment anyone writes to it.
    """
    if not isinstance(manifest, Mapping):
        raise TrustError("release manifest must be a JSON object")
    snapshot = _json_only(manifest)
    for field in _REQUIRED_MANIFEST_FIELDS:
        if field not in snapshot:
            raise TrustError(f"release manifest is missing {field}")
    if snapshot["schema"] != MANIFEST_SCHEMA:
        raise TrustError(
            f"release manifest declares schema {snapshot['schema']!r}, not the "
            f"one this AQG implements ({MANIFEST_SCHEMA}); its fields may not "
            f"mean what this code would take them to mean"
        )
    sequence = snapshot["release_sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise TrustError(
            "release manifest 'release_sequence' must be an integer; it is the "
            "anti-rollback comparison and a string does not compare"
        )
    if sequence < 0:
        raise TrustError("release manifest 'release_sequence' must not be negative")
    if not isinstance(snapshot["key_id"], str):
        raise TrustError("release manifest 'key_id' must be a string")
    return snapshot


def verify_manifest(
    manifest: Mapping[str, Any],
    *,
    signature: str,
    keyring: Mapping[str, TrustedKey],
    channel: str,
    installed_sequence: Union[int, _FirstInstall],
    signature_key_id: Optional[str] = None,
) -> VerifiedRelease:
    """Raise unless *signature* authenticates *manifest* under a trusted key.

    ``installed_sequence`` is the sequence already installed; a manifest at or
    below it is refused, because a correctly signed *older* release is what a
    rollback attack replays. It is **required**, and a first install is spelled
    ``FIRST_INSTALL``: this is the only gate against replaying a validly signed
    vulnerable release, so a caller that forgot the argument, or one holding
    ``None`` because its state read failed, must not be able to switch it off by
    accident.
    """
    manifest = _validated_manifest(manifest)

    if manifest["channel"] != channel:
        raise TrustError(
            f"release manifest is for channel {manifest['channel']!r}, not "
            f"{channel!r}; a correctly signed release for another channel is "
            f"still not one this install may take"
        )

    claimed = signature_key_id if signature_key_id is not None else manifest["key_id"]
    if claimed != manifest["key_id"]:
        raise TrustError(
            f"the signature names key {claimed!r} while the manifest names "
            f"{manifest['key_id']!r}"
        )
    key = keyring.get(claimed) if isinstance(keyring, Mapping) else None
    if key is None:
        raise TrustError(f"signing key {claimed!r} is not trusted by this install")
    if key.revoked:
        raise TrustError(f"signing key {claimed!r} is revoked")

    _verify_rsa(manifest, signature, key)

    if installed_sequence is not FIRST_INSTALL:
        if not isinstance(installed_sequence, int) or isinstance(
            installed_sequence, bool
        ):
            raise TrustError(
                "installed release sequence must be an integer, or FIRST_INSTALL "
                "when nothing is installed; anything else — None from a failed "
                "state read included — would disable the rollback check"
            )
        if installed_sequence < 0:
            raise TrustError("installed release sequence must not be negative")
        if manifest["release_sequence"] <= installed_sequence:
            raise SequenceNotAdvanced(
                f"release sequence {manifest['release_sequence']} is not ahead of "
                f"the installed {installed_sequence}; refusing a replayed or "
                f"rolled-back release"
            )

    return VerifiedRelease(manifest=_freeze(manifest), key_id=claimed)


def _verify_rsa(
    manifest: Mapping[str, Any], signature: str, key: TrustedKey
) -> None:
    """Strict PKCS#1 v1.5 verification. Ported, not re-derived."""
    if not isinstance(signature, str) or not signature:
        raise TrustError("release signature is missing")
    try:
        raw = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TrustError("release signature is not canonical base64") from exc
    if base64.b64encode(raw).decode("ascii") != signature:
        # RSA authenticates the digest, not this encoding of the integer. The
        # check is here so a signature has exactly one wire representation:
        # otherwise the same signature arrives in several spellings that log,
        # compare and deduplicate as different values.
        raise TrustError("release signature is not canonical base64")

    width = (key.modulus.bit_length() + 7) // 8
    if len(raw) != width:
        raise TrustError("release signature verification failed")
    signature_int = int.from_bytes(raw, "big")
    if signature_int >= key.modulus:
        raise TrustError("release signature verification failed")

    recovered = pow(signature_int, key.exponent, key.modulus).to_bytes(width, "big")
    digest = hashlib.sha256(canonical_bytes(manifest)).digest()
    encoded = DIGEST_INFO_SHA256 + digest
    padding_len = width - len(encoded) - 3
    if padding_len < 8:
        raise TrustError("trusted RSA modulus is too small to verify against")
    expected = b"\x00\x01" + (b"\xff" * padding_len) + b"\x00" + encoded

    # Rebuilt and compared whole. Parsing the recovered block instead — skipping
    # padding to find the digest — accepts trailing garbage, which is forgeable
    # for small exponents.
    if not hmac.compare_digest(recovered, expected):
        raise TrustError("release signature verification failed")
