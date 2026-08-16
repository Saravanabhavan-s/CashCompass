"""Vault service layer — module-level functions, db: Session first, commit
internally. Same shape as app/services/ledger.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import VaultFieldType
from app.exceptions import (
    DecryptionError,
    ShreddedError,
    TokenNotFoundError,
    UnauthorizedVaultAccess,
)
from app.models.vault import DetokenizationAudit, VaultEntry
from app.vault import crypto, tokens
from app.vault.keys import get_key_provider


def _write_audit(
    db: Session,
    token_id: str,
    tenant_id: str,
    caller: str,
    purpose: str,
    *,
    granted: bool,
    denial_reason: str | None = None,
) -> None:
    """Written from a SEPARATE short-lived Session — bound to the same
    engine as the caller's `db` (via db.get_bind()), not a fresh Session on
    that same connection — so it commits even if the caller's session is
    rolled back or dirty. Every reverse-map call, and every denial, gets
    exactly one row here, no exceptions."""
    with Session(db.get_bind()) as audit_session:
        audit_session.add(
            DetokenizationAudit(
                token_id=token_id,
                tenant_id=tenant_id,
                caller=caller,
                purpose=purpose,
                granted=granted,
                denial_reason=denial_reason,
            )
        )
        audit_session.commit()


def tokenize_field(
    db: Session,
    field_type: VaultFieldType,
    plaintext: str,
    tenant_id: str | None = None,
    *,
    allow_resurrect: bool = False,
) -> str:
    """Idempotent: if the token already exists, returns it without creating a
    second row. Re-tokenizing a value whose token was previously
    crypto-shredded raises ShreddedError unless allow_resurrect=True —
    erasure is durable; re-submitting the same value does not undo it."""
    tenant_id = tenant_id or settings.default_tenant_id
    provider = get_key_provider()
    hsk = provider.get_hsk()
    token_id = tokens.make_token(hsk, field_type, plaintext, tenant_id)

    existing = db.get(VaultEntry, token_id)
    if existing is not None:
        if existing.shredded_at is not None and not allow_resurrect:
            raise ShreddedError(
                f"token {token_id} was crypto-shredded; refusing to resurrect it. "
                "Pass allow_resurrect=True to override."
            )
        return token_id

    key_version = provider.current_kek_version()
    kek = provider.get_kek(key_version)
    dek = crypto.new_key()

    data_aad = tokens.build_data_aad(token_id, field_type, key_version, tenant_id)
    ciphertext, nonce, tag = crypto.encrypt(dek, plaintext.encode("utf-8"), data_aad)

    wrap_aad = tokens.build_wrap_aad(token_id, tenant_id)
    wrapped_dek, dek_nonce, dek_tag = crypto.wrap_dek(kek, dek, wrap_aad)

    row = VaultEntry(
        token_id=token_id,
        field_type=field_type.value,
        ciphertext=ciphertext,
        nonce=nonce,
        auth_tag=tag,
        wrapped_dek=wrapped_dek,
        dek_nonce=dek_nonce,
        dek_auth_tag=dek_tag,
        key_version=key_version,
        kek_version=key_version,
        tenant_id=tenant_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent insert of the same deterministic token — close the race.
        db.rollback()
        existing = db.get(VaultEntry, token_id)
        if existing is None:
            raise
        return token_id
    return token_id


def detokenize(db: Session, token_id: str, caller: str, purpose: str) -> str:
    """Order: allowlist -> row exists -> not shredded -> unwrap -> decrypt.
    Exactly one audit row is written per call, success or failure."""
    allowed = [c.strip() for c in settings.vault_detokenize_allowed_callers.split(",") if c.strip()]
    row = db.get(VaultEntry, token_id)
    tenant_id_for_audit = row.tenant_id if row is not None else settings.default_tenant_id

    if allowed and caller not in allowed:
        reason = f"caller {caller!r} is not in the detokenize allowlist"
        _write_audit(db, token_id, tenant_id_for_audit, caller, purpose, granted=False, denial_reason=reason)
        raise UnauthorizedVaultAccess(reason)

    if row is None:
        reason = "token not found"
        _write_audit(db, token_id, tenant_id_for_audit, caller, purpose, granted=False, denial_reason=reason)
        raise TokenNotFoundError(f"no vault entry for token {token_id!r}")

    if row.shredded_at is not None:
        reason = "SHREDDED: value has been crypto-shredded and is permanently unrecoverable"
        _write_audit(db, token_id, row.tenant_id, caller, purpose, granted=False, denial_reason=reason)
        raise ShreddedError(reason)

    provider = get_key_provider()
    try:
        kek = provider.get_kek(row.kek_version)
        wrap_aad = tokens.build_wrap_aad(token_id, row.tenant_id)
        dek = crypto.unwrap_dek(kek, row.wrapped_dek, row.dek_nonce, row.dek_auth_tag, wrap_aad)
        data_aad = tokens.build_data_aad(token_id, VaultFieldType(row.field_type), row.key_version, row.tenant_id)
        plaintext = crypto.decrypt(dek, row.ciphertext, row.nonce, row.auth_tag, data_aad)
    except DecryptionError as exc:
        _write_audit(db, token_id, row.tenant_id, caller, purpose, granted=False, denial_reason=f"decryption failed: {exc}")
        raise

    _write_audit(db, token_id, row.tenant_id, caller, purpose, granted=True)
    return plaintext.decode("utf-8")


def tokenize_payload(
    db: Session,
    payload: dict,
    field_type_map: dict[str, VaultFieldType],
    tenant_id: str | None = None,
) -> tuple[dict, dict[str, str]]:
    """Tokenizes every string (or list-of-strings) leaf whose dict key is
    present in field_type_map; everything else passes through unchanged.
    field_type_map is explicit rather than inferred from classify(), because
    classification.py only assigns a concrete VaultFieldType to IDENTITY
    fields — health/family and free-text fields need the caller to say what
    they are. Returns (tokenized_payload, {token_id: original_plaintext})."""
    tenant_id = tenant_id or settings.default_tenant_id
    token_map: dict[str, str] = {}

    def _walk(obj):
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                field_type = field_type_map.get(key)
                if field_type is not None and isinstance(value, str):
                    token_id = tokenize_field(db, field_type, value, tenant_id)
                    token_map[token_id] = value
                    out[key] = token_id
                elif field_type is not None and isinstance(value, list):
                    tokenized_list = []
                    for item in value:
                        if isinstance(item, str):
                            token_id = tokenize_field(db, field_type, item, tenant_id)
                            token_map[token_id] = item
                            tokenized_list.append(token_id)
                        else:
                            tokenized_list.append(_walk(item))
                    out[key] = tokenized_list
                else:
                    out[key] = _walk(value)
            return out
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        return obj

    return _walk(payload), token_map


def detokenize_payload(db: Session, payload: dict, caller: str, purpose: str) -> dict:
    """Rehydrates every token-shaped string found anywhere in payload. A
    payload with N tokens writes N audit rows — one per reverse-map call,
    per the "every reverse-map call is logged, no exceptions" rule."""

    def _walk(obj):
        if isinstance(obj, dict):
            return {key: _walk(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        if isinstance(obj, str) and tokens.is_token(obj):
            return detokenize(db, obj, caller, purpose)
        return obj

    return _walk(payload)


def rotate_kek(db: Session, new_kek: bytes, new_version: int) -> dict:
    """Re-wraps DEKs ONLY — never touches ciphertext, nonce, auth_tag, or
    key_version. That's what makes rotation a cheap re-wrap instead of a
    full re-encryption; see app/vault/crypto.py's module docstring."""
    provider = get_key_provider()
    if hasattr(provider, "add_kek"):
        provider.add_kek(new_version, new_kek, make_current=True)

    rows = list(db.scalars(select(VaultEntry).where(VaultEntry.shredded_at.is_(None))).all())
    rewrapped = 0
    for row in rows:
        if row.kek_version == new_version:
            continue  # idempotent: already on the new key
        old_kek = provider.get_kek(row.kek_version)
        wrap_aad = tokens.build_wrap_aad(row.token_id, row.tenant_id)
        dek = crypto.unwrap_dek(old_kek, row.wrapped_dek, row.dek_nonce, row.dek_auth_tag, wrap_aad)
        wrapped_dek, dek_nonce, dek_tag = crypto.wrap_dek(new_kek, dek, wrap_aad)
        row.wrapped_dek = wrapped_dek
        row.dek_nonce = dek_nonce
        row.dek_auth_tag = dek_tag
        row.kek_version = new_version
        rewrapped += 1
    db.commit()

    remaining_on_old = db.scalar(
        select(func.count())
        .select_from(VaultEntry)
        .where(VaultEntry.kek_version != new_version, VaultEntry.shredded_at.is_(None))
    ) or 0

    return {"to_version": new_version, "rewrapped": rewrapped, "remaining_on_old": remaining_on_old}


def crypto_shred(db: Session, token_id: str, reason: str, *, caller: str = "system") -> None:
    """Destroys the DEK. The ciphertext becomes permanently unrecoverable —
    mathematically, not by policy. The ledger entry, the token row, and the
    audit trail all survive; only the personal data is gone."""
    row = db.get(VaultEntry, token_id)
    if row is None:
        raise TokenNotFoundError(f"no vault entry for token {token_id!r}")
    if row.shredded_at is not None:
        return  # idempotent no-op

    row.wrapped_dek = None
    row.dek_nonce = None
    row.dek_auth_tag = None
    row.shredded_at = datetime.now(timezone.utc)
    db.commit()

    _write_audit(
        db,
        token_id,
        row.tenant_id,
        caller,
        purpose="crypto_shred",
        granted=False,
        denial_reason=f"SHREDDED: {reason}",
    )


def list_audit(
    db: Session, limit: int = 50, offset: int = 0, token_id: str | None = None
) -> tuple[list[DetokenizationAudit], int]:
    count_stmt = select(func.count()).select_from(DetokenizationAudit)
    items_stmt = select(DetokenizationAudit).order_by(DetokenizationAudit.requested_at.desc())
    if token_id:
        count_stmt = count_stmt.where(DetokenizationAudit.token_id == token_id)
        items_stmt = items_stmt.where(DetokenizationAudit.token_id == token_id)

    total = db.scalar(count_stmt) or 0
    items = list(db.scalars(items_stmt.limit(limit).offset(offset)).all())
    return items, total
