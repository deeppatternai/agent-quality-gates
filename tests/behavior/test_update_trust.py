"""Behavior contracts for verifying a signed release.

docs/UPDATE_ARCHITECTURE.md §9. Once an update applies without a human
confirming it, the signature IS the consent — so every case here is about what
must be refused, not about what a good signature does.

The verifier is stdlib-only on purpose: it ships to every user, and AQG's only
runtime dependency is PyYAML. That means the RSA check is written out by hand,
which is exactly the code where a subtle mistake becomes a forgery. It is
therefore ported from DE's audited implementation rather than re-derived, and
the property that makes it safe is that it reconstructs the WHOLE expected
PKCS#1 v1.5 block and compares it in constant time — it never parses what came
out of the exponentiation.

The signing side of this lives outside the shipped tree and does not appear
here. These tests sign with a throwaway keypair stored as integers in a fixture,
so the suite exercises real RSA maths with no library and no secret.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from scripts.aqg_update import trust as trust_mod

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "release_test_key.json"
_KEY = json.loads(FIXTURE.read_text(encoding="utf-8"))
_N = int(_KEY["modulus_hex"], 16)
_E = _KEY["exponent"]
_D = int(_KEY["private_exponent_hex"], 16)
_KEY_ID = _KEY["key_id"]


def _keyring(**overrides):
    entry = {
        "key_id": _KEY_ID,
        "algorithm": "rsa-pkcs1v15-sha256",
        "modulus_hex": _KEY["modulus_hex"],
        "exponent": _E,
        "revoked": False,
    }
    entry.update(overrides)
    return trust_mod.load_keyring({"schema": 1, "keys": [entry]})


def _manifest(**overrides):
    payload = {
        "schema": 1,
        "channel": "stable",
        "version": "0.16.0",
        "commit": "a1b2c3d4e5f6",
        "release_sequence": 8,
        "key_id": _KEY_ID,
        "python_minimum": "3.9",
        "files": {"VERSION": "a" * 64},
    }
    payload.update(overrides)
    return payload


def _sign(manifest: dict, *, private_exponent: int = _D, modulus: int = _N) -> str:
    """Produce a real PKCS#1 v1.5 signature with the throwaway key."""
    digest = hashlib.sha256(trust_mod.canonical_bytes(manifest)).digest()
    encoded = trust_mod.DIGEST_INFO_SHA256 + digest
    width = (modulus.bit_length() + 7) // 8
    block = b"\x00\x01" + b"\xff" * (width - len(encoded) - 3) + b"\x00" + encoded
    signed = pow(int.from_bytes(block, "big"), private_exponent, modulus)
    return base64.b64encode(signed.to_bytes(width, "big")).decode("ascii")


# --- the good case exists only to make the refusals meaningful ---------------------


def test_a_correctly_signed_manifest_verifies(_ok=None):
    manifest = _manifest()
    verified = trust_mod.verify_manifest(
        manifest, signature=_sign(manifest), keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )
    assert verified.key_id == _KEY_ID
    assert verified.manifest["version"] == "0.16.0"


# --- the signature itself ----------------------------------------------------------


def test_a_manifest_altered_after_signing_is_refused():
    manifest = _manifest()
    signature = _sign(manifest)
    tampered = {**manifest, "version": "9.9.9"}
    with pytest.raises(trust_mod.TrustError, match="verification failed"):
        trust_mod.verify_manifest(
            tampered, signature=signature, keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_signature_from_a_different_key_is_refused():
    """The whole point of a pinned keyring: a valid signature by the wrong key
    is exactly what an attacker produces."""
    other_n = _N - 2  # a different modulus; the maths will simply not agree
    manifest = _manifest()
    signature = _sign(manifest, modulus=other_n)
    with pytest.raises(trust_mod.TrustError):
        trust_mod.verify_manifest(
            manifest, signature=signature, keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_revoked_key_is_refused_even_with_a_valid_signature():
    manifest = _manifest()
    with pytest.raises(trust_mod.TrustError, match="revoked"):
        trust_mod.verify_manifest(
            manifest,
            signature=_sign(manifest),
            keyring=_keyring(revoked=True),
            channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_key_absent_from_the_keyring_is_refused():
    manifest = _manifest(key_id="some-other-key")
    with pytest.raises(trust_mod.TrustError, match="not trusted"):
        trust_mod.verify_manifest(
            manifest, signature=_sign(manifest), keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_signature_whose_key_id_disagrees_with_the_manifest_is_refused():
    """Naming one key and signing with another is how a valid signature gets
    pointed at the wrong manifest."""
    manifest = _manifest()
    with pytest.raises(trust_mod.TrustError, match="key"):
        trust_mod.verify_manifest(
            manifest,
            signature=_sign(manifest),
            keyring=_keyring(),
            channel="stable",
            signature_key_id="a-different-key",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


@pytest.mark.parametrize(
    "bad", ["", "not base64!!", "YWJj", "AAAA" * 100]
)
def test_a_malformed_signature_is_refused_rather_than_crashing(bad):
    with pytest.raises(trust_mod.TrustError):
        trust_mod.verify_manifest(
            _manifest(), signature=bad, keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_non_canonical_base64_signature_is_refused():
    """Accepting a re-encodable variant lets the same signature arrive in more
    than one form, which is a distinct value nobody signed."""
    manifest = _manifest()
    good = _sign(manifest)
    sneaky = good[:-1] + ("A" if good[-1] != "A" else "B")
    with pytest.raises(trust_mod.TrustError):
        trust_mod.verify_manifest(
            manifest, signature=sneaky, keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_signature_numerically_larger_than_the_modulus_is_refused():
    width = (_N.bit_length() + 7) // 8
    oversized = base64.b64encode((_N + 1).to_bytes(width, "big")).decode("ascii")
    with pytest.raises(trust_mod.TrustError):
        trust_mod.verify_manifest(
            _manifest(), signature=oversized, keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


# --- the manifest's own claims -------------------------------------------------------


def test_a_manifest_for_another_channel_is_refused():
    """`edge` is manual-only. A signed edge manifest arriving on the automatic
    path is a correctly signed thing that must still not be applied."""
    manifest = _manifest(channel="edge")
    with pytest.raises(trust_mod.TrustError, match="channel"):
        trust_mod.verify_manifest(
            manifest, signature=_sign(manifest), keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


@pytest.mark.parametrize("missing", ["version", "commit", "release_sequence", "key_id"])
def test_a_manifest_missing_a_required_field_is_refused(missing):
    manifest = _manifest()
    del manifest[missing]
    with pytest.raises(trust_mod.TrustError, match=missing):
        trust_mod.verify_manifest(
            manifest, signature=_sign(manifest), keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_non_integer_release_sequence_is_refused():
    """The sequence is the anti-rollback comparison; a string compares by
    accident or not at all."""
    manifest = _manifest(release_sequence="8")
    with pytest.raises(trust_mod.TrustError, match="release_sequence"):
        trust_mod.verify_manifest(
            manifest, signature=_sign(manifest), keyring=_keyring(), channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


# --- anti-rollback ---------------------------------------------------------------------


def test_a_sequence_below_what_is_installed_is_refused():
    """A correctly signed OLD release is exactly what a rollback attack
    replays."""
    manifest = _manifest(release_sequence=3)
    with pytest.raises(trust_mod.TrustError, match="sequence"):
        trust_mod.verify_manifest(
            manifest,
            signature=_sign(manifest),
            keyring=_keyring(),
            channel="stable",
            installed_sequence=7,
        )


def test_the_same_sequence_is_refused_as_well():
    """Re-applying the installed sequence is not an update; accepting it lets a
    replayed manifest look like progress."""
    manifest = _manifest(release_sequence=7)
    with pytest.raises(trust_mod.TrustError, match="sequence"):
        trust_mod.verify_manifest(
            manifest,
            signature=_sign(manifest),
            keyring=_keyring(),
            channel="stable",
            installed_sequence=7,
        )


def test_a_higher_sequence_is_accepted():
    manifest = _manifest(release_sequence=9)
    verified = trust_mod.verify_manifest(
        manifest,
        signature=_sign(manifest),
        keyring=_keyring(),
        channel="stable",
        installed_sequence=7,
    )
    assert verified.manifest["release_sequence"] == 9


def test_a_first_install_accepts_any_sequence():
    # Contract changed after aud_EqcBMQUXLJFRgS3o: first install is spelled with
    # an explicit sentinel, so a None from a failed state read cannot pass for it.
    manifest = _manifest(release_sequence=1)
    verified = trust_mod.verify_manifest(
        manifest,
        signature=_sign(manifest),
        keyring=_keyring(),
        channel="stable",
        installed_sequence=trust_mod.FIRST_INSTALL,
    )
    assert verified.manifest["release_sequence"] == 1


# --- the keyring itself ------------------------------------------------------------------


def test_a_keyring_with_an_unknown_algorithm_is_refused():
    with pytest.raises(trust_mod.TrustError, match="algorithm"):
        _keyring(algorithm="rsa-pkcs1v15-md5")


def test_a_keyring_with_a_short_modulus_is_refused():
    """A modulus too small for the padding is not a weak key, it is one where
    the check cannot be performed at all."""
    with pytest.raises(trust_mod.TrustError, match="modulus"):
        _keyring(modulus_hex=format(2**512 + 1, "x"))


def test_a_keyring_with_a_duplicate_key_id_is_refused():
    entry = {
        "key_id": _KEY_ID,
        "algorithm": "rsa-pkcs1v15-sha256",
        "modulus_hex": _KEY["modulus_hex"],
        "exponent": _E,
        "revoked": False,
    }
    with pytest.raises(trust_mod.TrustError, match="duplicate"):
        trust_mod.load_keyring({"schema": 1, "keys": [entry, dict(entry)]})


def test_an_empty_keyring_is_refused():
    """An empty keyring would make every signature untrusted, which reads as a
    verification failure rather than as a broken install."""
    with pytest.raises(trust_mod.TrustError, match="no keys"):
        trust_mod.load_keyring({"schema": 1, "keys": []})


def test_a_keyring_of_a_future_schema_is_refused():
    with pytest.raises(trust_mod.TrustError, match="schema"):
        trust_mod.load_keyring({"schema": 99, "keys": []})


# --- canonical bytes -----------------------------------------------------------------------


def test_key_order_does_not_change_what_is_signed():
    """Two spellings of the same manifest must produce one signature, or a
    re-serialization between signing and verifying breaks every release."""
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert trust_mod.canonical_bytes(a) == trust_mod.canonical_bytes(b)


def test_canonical_bytes_reject_a_value_json_cannot_represent():
    with pytest.raises(trust_mod.TrustError):
        trust_mod.canonical_bytes({"x": {1, 2}})


# =============================================================================
# Added after audit aud_EqcBMQUXLJFRgS3o. See .aqg/adjudication/.
# =============================================================================

# --- the block structure itself ------------------------------------------------------
#
# The tests above sign with a helper that builds the SAME PKCS#1 block the
# verifier rebuilds, so a construction bug shared by both — a wrong DigestInfo
# prefix, a misplaced separator — would pass every one of them. These two
# sections are the ones that can detect it: one signs deliberately malformed
# blocks with the fixture private key, and one verifies a signature this
# codebase did not produce.


def _sign_block(block: bytes) -> str:
    """Sign an arbitrary, possibly malformed block. Forgery, on purpose."""
    return base64.b64encode(
        pow(int.from_bytes(block, "big"), _D, _N).to_bytes(
            (_N.bit_length() + 7) // 8, "big"
        )
    ).decode("ascii")


def _width() -> int:
    return (_N.bit_length() + 7) // 8


@pytest.mark.parametrize(
    "name,build",
    [
        # A parsing verifier skips the padding to find the digest and accepts
        # whatever follows. This is the forgery class the module exists to refuse.
        ("trailing garbage after the digest",
         lambda w, enc: b"\x00\x01" + b"\xff" * 8 + b"\x00" + enc
                        + b"\x00" * (w - 11 - len(enc))),
        # PS shorter than the width demands, padded out at the front.
        ("a short 0xff run",
         lambda w, enc: b"\x00" * (w - len(enc) - 11) + b"\x00\x01"
                        + b"\xff" * 8 + b"\x00" + enc),
        # No 0x00 between the padding and the DigestInfo.
        ("no separator byte",
         lambda w, enc: b"\x00\x01" + b"\xff" * (w - len(enc) - 2) + enc),
        # The right digest under the wrong algorithm identifier.
        # SHA-1's DigestInfo carrying a SHA-256 digest: shorter, so the padding
        # has to be computed from its own length or the block is the wrong width.
        ("a DigestInfo for another hash",
         lambda w, enc: (lambda alt: b"\x00\x01" + b"\xff" * (w - len(alt) - 3)
                                     + b"\x00" + alt)(
             b"\x30\x21\x30\x09\x06\x05\x2b\x0e\x03\x02\x1a\x05\x00\x04\x14"
             + enc[19:])),
        # First byte not 0x00: the block does not even start correctly.
        ("a block that does not start with 0x00 0x01",
         lambda w, enc: b"\x00\x02" + b"\xff" * (w - len(enc) - 3) + b"\x00" + enc),
    ],
)
def test_a_malformed_pkcs1_block_is_refused_even_when_correctly_signed(name, build):
    """Signed with the real private key — only the block is wrong.

    Every one of these carries the correct digest of the correct manifest. A
    verifier that parses what came out of the exponentiation accepts them; one
    that rebuilds and compares does not.
    """
    manifest = _manifest()
    digest = hashlib.sha256(trust_mod.canonical_bytes(manifest)).digest()
    encoded = trust_mod.DIGEST_INFO_SHA256 + digest
    block = build(_width(), encoded)
    assert len(block) == _width(), f"fixture {name} built a wrong-width block"
    with pytest.raises(trust_mod.TrustError, match="verification failed"):
        trust_mod.verify_manifest(
            manifest,
            signature=_sign_block(block),
            keyring=_keyring(),
            channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


def test_a_signature_from_an_independent_implementation_verifies():
    """The known-answer test. LibreSSL built this block, not this codebase.

    Everything else in this file is signed by a helper that shares its
    construction with the code under test, so it cannot detect a shared error in
    the DigestInfo constant or the padding layout. This vector can: if the
    prefix bytes were wrong, this signature would not verify.
    """
    vector = json.loads(
        (FIXTURE.parent / "release_kat_openssl.json").read_text(encoding="utf-8")
    )
    keyring = trust_mod.load_keyring(
        {
            "schema": 1,
            "keys": [
                {
                    "key_id": vector["key_id"],
                    "algorithm": vector["algorithm"],
                    "modulus_hex": vector["modulus_hex"],
                    "exponent": vector["exponent"],
                    "revoked": False,
                }
            ],
        }
    )
    verified = trust_mod.verify_manifest(
        vector["manifest"],
        signature=vector["signature"],
        keyring=keyring,
        channel="stable",
        installed_sequence=trust_mod.FIRST_INSTALL,
    )
    assert verified.key_id == vector["key_id"]


def test_the_independent_vector_is_rejected_when_its_manifest_is_altered():
    """Otherwise the vector above would pass for a reason other than the maths."""
    vector = json.loads(
        (FIXTURE.parent / "release_kat_openssl.json").read_text(encoding="utf-8")
    )
    keyring = trust_mod.load_keyring(
        {
            "schema": 1,
            "keys": [
                {
                    "key_id": vector["key_id"],
                    "algorithm": vector["algorithm"],
                    "modulus_hex": vector["modulus_hex"],
                    "exponent": vector["exponent"],
                    "revoked": False,
                }
            ],
        }
    )
    with pytest.raises(trust_mod.TrustError, match="verification failed"):
        trust_mod.verify_manifest(
            {**vector["manifest"], "commit": "0" * 40},
            signature=vector["signature"],
            keyring=keyring,
            channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


# --- what "verified" is allowed to mean ------------------------------------------------


def test_the_verified_manifest_cannot_be_mutated_afterwards():
    """A frozen dataclass stops reassignment, not mutation.

    If a field can move after the signature was checked, then `VerifiedRelease`
    asserts something about bytes that are no longer there.
    """
    manifest = _manifest()
    verified = trust_mod.verify_manifest(
        manifest,
        signature=_sign(manifest),
        keyring=_keyring(),
        channel="stable",
        installed_sequence=trust_mod.FIRST_INSTALL,
    )
    with pytest.raises(TypeError):
        verified.manifest["commit"] = "attacker"  # type: ignore[index]
    assert verified.manifest["commit"] == "a1b2c3d4e5f6"


def test_mutating_the_caller_s_manifest_does_not_reach_the_verified_one():
    """The caller keeps its own object; the verified one is a snapshot."""
    manifest = _manifest()
    verified = trust_mod.verify_manifest(
        manifest,
        signature=_sign(manifest),
        keyring=_keyring(),
        channel="stable",
        installed_sequence=trust_mod.FIRST_INSTALL,
    )
    manifest["commit"] = "attacker"
    assert verified.manifest["commit"] == "a1b2c3d4e5f6"


def test_a_nested_value_in_the_verified_manifest_is_immutable_too():
    """Shallow-copying would leave `files` — the hash roster — writable."""
    manifest = _manifest()
    verified = trust_mod.verify_manifest(
        manifest,
        signature=_sign(manifest),
        keyring=_keyring(),
        channel="stable",
        installed_sequence=trust_mod.FIRST_INSTALL,
    )
    with pytest.raises(TypeError):
        verified.manifest["files"]["VERSION"] = "b" * 64  # type: ignore[index]


# --- the keyring as a trust anchor ------------------------------------------------------


def test_the_same_key_material_under_a_second_id_is_refused():
    """Revoking an id does not revoke a key.

    Two ids over one modulus means revoking A leaves B signing with the
    identical private key — the revocation is cosmetic.
    """
    entry = {
        "algorithm": "rsa-pkcs1v15-sha256",
        "modulus_hex": _KEY["modulus_hex"],
        "exponent": _E,
    }
    with pytest.raises(trust_mod.TrustError, match="material"):
        trust_mod.load_keyring(
            {
                "schema": 1,
                "keys": [
                    {**entry, "key_id": "revoked-one", "revoked": True},
                    {**entry, "key_id": "live-alias", "revoked": False},
                ],
            }
        )


@pytest.mark.parametrize(
    "bad_hex",
    ["-" + format(2**2047 + 1, "x"), "0x" + format(2**2047 + 1, "x"),
     "  " + format(2**2047 + 1, "x") + "  ", "1_" + format(2**2047, "x"),
     format(2**2047 + 0xabcdef, "X")],
)
def test_a_non_canonical_modulus_string_is_refused(bad_hex):
    """`int(s, 16)` accepts a sign, a prefix, underscores and whitespace, and a
    negative modulus survives the bit-length floor because `bit_length()` ignores
    the sign."""
    with pytest.raises(trust_mod.TrustError, match="modulus_hex"):
        _keyring(modulus_hex=bad_hex)


def test_an_even_modulus_is_refused():
    """No RSA modulus is even; accepting one means the entry is not a key."""
    with pytest.raises(trust_mod.TrustError, match="modulus"):
        _keyring(modulus_hex=format(2**2048, "x"))


def test_an_even_exponent_is_refused():
    with pytest.raises(trust_mod.TrustError, match="exponent"):
        _keyring(exponent=65536)


def test_an_exponent_not_below_the_modulus_is_refused():
    with pytest.raises(trust_mod.TrustError, match="exponent"):
        _keyring(exponent=_N + 1)


def test_an_oversized_modulus_is_refused():
    """Unbounded is not the same as large: `pow()` against a megabit modulus
    does not fail, it hangs — and a hung update is a stuck one."""
    with pytest.raises(trust_mod.TrustError, match="modulus"):
        _keyring(modulus_hex=format(2 ** 40000 + 1, "x"))


# --- the manifest's own schema ------------------------------------------------------------


def test_a_manifest_declaring_a_future_schema_is_refused():
    """The keyring's schema is pinned; the manifest's must be too.

    A schema-2 manifest that redefines what `channel` or `release_sequence`
    means would otherwise be processed by schema-1 logic, on a path that applies
    without asking anyone.
    """
    manifest = _manifest(schema=2)
    with pytest.raises(trust_mod.TrustError, match="schema"):
        trust_mod.verify_manifest(
            manifest,
            signature=_sign(manifest),
            keyring=_keyring(),
            channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


# --- anti-rollback must not be able to switch itself off ------------------------------------


def test_omitting_the_installed_sequence_is_a_type_error_not_a_weaker_check():
    """The gate against a replayed old release must not default to off.

    A caller that forgets the argument, or one whose state read failed and
    passed None, would otherwise disable the only defence against a validly
    signed vulnerable release — while every other check still passes.
    """
    manifest = _manifest()
    with pytest.raises(TypeError):
        trust_mod.verify_manifest(
            manifest, signature=_sign(manifest), keyring=_keyring(), channel="stable"
        )


def test_none_is_not_accepted_as_a_first_install():
    """First install is spelled explicitly, so a failed state read cannot
    impersonate one."""
    manifest = _manifest()
    with pytest.raises(trust_mod.TrustError):
        trust_mod.verify_manifest(
            manifest,
            signature=_sign(manifest),
            keyring=_keyring(),
            channel="stable",
            installed_sequence=None,
        )


def test_a_negative_release_sequence_is_refused():
    manifest = _manifest(release_sequence=-1)
    with pytest.raises(trust_mod.TrustError, match="sequence"):
        trust_mod.verify_manifest(
            manifest,
            signature=_sign(manifest),
            keyring=_keyring(),
            channel="stable",
            installed_sequence=trust_mod.FIRST_INSTALL,
        )


# --- canonical bytes must be JSON, and must be injective --------------------------------------


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_float_is_refused(value):
    """`json.dumps` emits the non-JSON tokens NaN/Infinity by default.

    Deterministic inside Python, but not parseable by anything else — so the
    "one byte string per manifest" contract holds only until a second
    implementation touches the pipeline.
    """
    with pytest.raises(trust_mod.TrustError):
        trust_mod.canonical_bytes({"x": value})


def test_a_non_string_key_is_refused_rather_than_coerced():
    """`{1: "x"}` and `{"1": "x"}` serialize identically, so two distinct
    manifests would share one signature."""
    with pytest.raises(trust_mod.TrustError):
        trust_mod.canonical_bytes({1: "x"})


def test_a_tuple_is_refused_rather_than_serialized_as_a_list():
    with pytest.raises(trust_mod.TrustError):
        trust_mod.canonical_bytes({"x": (1, 2)})


def test_a_nested_non_json_value_is_refused():
    """The check has to recurse, or it only guards the top level."""
    with pytest.raises(trust_mod.TrustError):
        trust_mod.canonical_bytes({"a": {"b": [1, {"c": {1, 2}}]}})
