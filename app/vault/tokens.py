"""Deterministic tokenization (blind index) — the ledger cross-reference
mechanism.

Random per-call IDs are wrong here: the same person appearing in two ledger
entries must produce the SAME token, or an LLM reading both treats them as
two different people and all cross-entry reasoning breaks. So tokenize() is
HMAC-SHA256(HSK, "field_type:normalized_value") truncated to 64 bits, not a
randomly generated id.

Type-prefixed: the LLM sees TKN_PERSON_8f3a... never a bare TKN_8f3a... —
models reason measurably better over typed placeholders.

field_type is baked INSIDE the HMAC message (not just the string prefix) so
that the same underlying string under two different field types is
cryptographically distinct, not just cosmetically distinct — "acme corp" as
a NAME and "acme corp" as an EMPLOYER must not collide.

64-bit truncation: birthday collision at ~2^32 tokens per field type per
tenant. Acceptable at prototype scale; widen to the full digest before this
faces a real population.

Per-tenant HSK derivation (HKDF): the raw formula in the spec,
HMAC(HSK, f"{field_type}:{n}"), does not depend on tenant_id, yet token_id is
both the primary key AND part of the AAD alongside tenant_id. Two tenants
tokenizing the same PAN would derive the identical token_id, collide on the
primary key, and the loser's AAD would never match the stored row's — a
silent, permanent decryption failure. Deriving a per-tenant HSK via HKDF
keeps the spec's HMAC formula byte-for-byte unchanged while making tokens
tenant-unique. With a single tenant (the prototype default) this degenerates
to exactly the spec's formula.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.enums import VaultFieldType
from app.vault.normalize import normalize

DIGEST_HEX_CHARS = 16  # 64 bits of the HMAC-SHA256 output

TOKEN_RE = re.compile(r"^TKN_(?P<field_type>[A-Z][A-Z0-9_]*)_(?P<digest>[0-9a-f]{16})$")

_HKDF_INFO = b"cashcompass-blind-index"


def tenant_hsk(hsk: bytes, tenant_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=tenant_id.encode("utf-8"),
        info=_HKDF_INFO,
    ).derive(hsk)


def blind_index(hsk: bytes, field_type: VaultFieldType, normalized: str) -> str:
    """HMAC-SHA256(hsk, "field_type:normalized") -> TKN_<FIELD_TYPE>_<16 hex>.
    `hsk` here is expected to already be the tenant-scoped key (tenant_hsk())."""
    msg = f"{field_type.value}:{normalized}".encode("utf-8")
    digest = hmac.new(hsk, msg, hashlib.sha256).hexdigest()
    return f"TKN_{field_type.value}_{digest[:DIGEST_HEX_CHARS]}"


def make_token(hsk: bytes, field_type: VaultFieldType, plaintext: str, tenant_id: str) -> str:
    normalized = normalize(field_type, plaintext)
    return blind_index(tenant_hsk(hsk, tenant_id), field_type, normalized)


def parse_token(token_id: str) -> tuple[VaultFieldType, str]:
    match = TOKEN_RE.match(token_id)
    if not match:
        raise ValueError(f"not a well-formed vault token: {token_id!r}")
    return VaultFieldType(match.group("field_type")), match.group("digest")


def is_token(value: object) -> bool:
    return isinstance(value, str) and bool(TOKEN_RE.match(value))


def build_data_aad(token_id: str, field_type: VaultFieldType, key_version: int, tenant_id: str) -> bytes:
    """AAD for the DATA encryption (§3). Binds ciphertext to its identity so a
    row-swap attack (copying row A's ciphertext into row B) fails decryption
    on both rows."""
    return f"{token_id}|{field_type.value}|{key_version}|{tenant_id}".encode("utf-8")


def build_wrap_aad(token_id: str, tenant_id: str) -> bytes:
    """AAD for the DEK wrap. Deliberately excludes kek_version (which changes
    on rotation) and field_type/key_version (irrelevant to which DEK this is)
    so it stays stable across rotation, while still preventing a
    wrapped_dek/dek_nonce/dek_auth_tag row-swap between tokens or tenants."""
    return f"{token_id}|{tenant_id}|dek-wrap".encode("utf-8")
