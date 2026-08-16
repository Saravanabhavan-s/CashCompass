import os

import pytest
from cryptography.exceptions import InvalidTag

from app.exceptions import DecryptionError, KeyConfigurationError
from app.vault import crypto


def test_round_trip_returns_exact_plaintext():
    key = crypto.new_key()
    aad = b"aad-context"
    ciphertext, nonce, tag = crypto.encrypt(key, b"hello vault", aad)
    assert crypto.decrypt(key, ciphertext, nonce, tag, aad) == b"hello vault"


def test_flipping_one_ciphertext_bit_raises_decryption_error():
    key = crypto.new_key()
    aad = b"aad"
    ciphertext, nonce, tag = crypto.encrypt(key, b"secret value", aad)
    tampered = bytearray(ciphertext)
    tampered[0] ^= 0x01
    with pytest.raises(DecryptionError):
        crypto.decrypt(key, bytes(tampered), nonce, tag, aad)


def test_flipping_one_ciphertext_bit_raises_invalid_tag_as_cause():
    key = crypto.new_key()
    aad = b"aad"
    ciphertext, nonce, tag = crypto.encrypt(key, b"secret value", aad)
    tampered = bytearray(ciphertext)
    tampered[0] ^= 0x01
    with pytest.raises(DecryptionError) as exc_info:
        crypto.decrypt(key, bytes(tampered), nonce, tag, aad)
    assert isinstance(exc_info.value.__cause__, InvalidTag)


def test_flipping_one_auth_tag_bit_raises():
    key = crypto.new_key()
    aad = b"aad"
    ciphertext, nonce, tag = crypto.encrypt(key, b"secret value", aad)
    tampered_tag = bytearray(tag)
    tampered_tag[0] ^= 0x01
    with pytest.raises(DecryptionError):
        crypto.decrypt(key, ciphertext, nonce, bytes(tampered_tag), aad)


def test_aad_mismatch_fails_decryption():
    key = crypto.new_key()
    ciphertext, nonce, tag = crypto.encrypt(key, b"secret value", b"aad-a")
    with pytest.raises(DecryptionError):
        crypto.decrypt(key, ciphertext, nonce, tag, b"aad-b")


def test_nonce_unique_across_10000_encryptions():
    key = crypto.new_key()
    nonces = set()
    for _ in range(10_000):
        _, nonce, _ = crypto.encrypt(key, b"x", b"")
        nonces.add(nonce)
    assert len(nonces) == 10_000


def test_encrypt_returns_ciphertext_nonce_16_byte_tag_split_out():
    key = crypto.new_key()
    ciphertext, nonce, tag = crypto.encrypt(key, b"payload", b"")
    assert len(nonce) == crypto.NONCE_LEN
    assert len(tag) == crypto.TAG_LEN
    assert isinstance(ciphertext, bytes)


def test_wrap_unwrap_dek_round_trip():
    kek = crypto.new_key()
    dek = crypto.new_key()
    aad = b"wrap-aad"
    wrapped, nonce, tag = crypto.wrap_dek(kek, dek, aad)
    assert crypto.unwrap_dek(kek, wrapped, nonce, tag, aad) == dek


def test_wrong_key_length_raises_key_configuration_error():
    with pytest.raises(KeyConfigurationError):
        crypto.encrypt(b"too-short", b"data", b"")


def test_constant_time_eq():
    assert crypto.constant_time_eq("abc", "abc")
    assert not crypto.constant_time_eq("abc", "abd")
