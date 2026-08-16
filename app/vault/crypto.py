"""AES-256-GCM envelope primitives. No DB, no settings, no logging of key
material — this module is pure and fully unit-testable offline.

NONCE DISCIPLINE (non-negotiable):
  A fresh 96-bit CSPRNG nonce is generated for EVERY encryption operation.
  Nonce reuse under the same GCM key leaks the GHASH authentication subkey
  and permits forgery of arbitrary ciphertexts — catastrophic, not degraded.
  Nonces are NEVER derived from counters, timestamps, row ids, or hashes of
  the plaintext. `encrypt()` generates its own nonce and does not accept one
  as an argument — the discipline is enforced structurally, not by comment.
  A nonce is stored in cleartext beside its ciphertext; it is not a secret,
  it only has to be unique.

KEK MUST NEVER ENCRYPT DATA DIRECTLY:
  The KEK is passed only to wrap_dek()/unwrap_dek(), and neither accepts a
  plaintext payload of arbitrary data — only a 32-byte DEK. If the KEK ever
  touched data directly, rotating it would force re-encrypting every row;
  wrapping only DEKs makes rotation a cheap re-wrap job instead. See
  app/services/vault.py:rotate_kek and tests/test_vault_rotation.py, which
  proves ciphertexts are byte-unchanged after rotation.
"""
from __future__ import annotations

import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.exceptions import DecryptionError, KeyConfigurationError

KEY_LEN = 32  # AES-256
NONCE_LEN = 12  # 96-bit, the GCM-native size
TAG_LEN = 16  # GCM authentication tag


def _require_len(b: bytes, n: int, name: str) -> None:
    if len(b) != n:
        raise KeyConfigurationError(f"{name} must be {n} bytes, got {len(b)}.")


def new_key() -> bytes:
    """A fresh 256-bit key. Used to mint a per-record DEK."""
    return os.urandom(KEY_LEN)


def new_nonce() -> bytes:
    return os.urandom(NONCE_LEN)


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes, bytes]:
    """Returns (ciphertext, nonce, auth_tag).

    AESGCM.encrypt() returns ciphertext||tag concatenated; the schema stores
    them in separate columns, so the 16-byte tag is split off explicitly here
    and rejoined in decrypt().
    """
    _require_len(key, KEY_LEN, "key")
    nonce = new_nonce()
    blob = AESGCM(key).encrypt(nonce, plaintext, aad)  # blob == ciphertext || tag
    ciphertext, tag = blob[:-TAG_LEN], blob[-TAG_LEN:]
    assert len(tag) == TAG_LEN
    return ciphertext, nonce, tag


def decrypt(key: bytes, ciphertext: bytes, nonce: bytes, tag: bytes, aad: bytes) -> bytes:
    _require_len(key, KEY_LEN, "key")
    _require_len(nonce, NONCE_LEN, "nonce")
    _require_len(tag, TAG_LEN, "tag")
    try:
        return AESGCM(key).decrypt(nonce, ciphertext + tag, aad)
    except InvalidTag as exc:
        raise DecryptionError(
            "GCM authentication failed: ciphertext, tag, nonce, or AAD does "
            "not match. This means tampering, a swapped row, or a wrong key "
            "— not a decode error."
        ) from exc


def wrap_dek(kek: bytes, dek: bytes, aad: bytes) -> tuple[bytes, bytes, bytes]:
    """Wraps a DEK under a KEK. Returns (wrapped_dek, dek_nonce, dek_auth_tag)."""
    return encrypt(kek, dek, aad)


def unwrap_dek(kek: bytes, wrapped: bytes, nonce: bytes, tag: bytes, aad: bytes) -> bytes:
    return decrypt(kek, wrapped, nonce, tag, aad)


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
