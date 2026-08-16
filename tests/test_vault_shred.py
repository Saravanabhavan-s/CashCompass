import pytest

from app.enums import VaultFieldType
from app.exceptions import ShreddedError
from app.models.ledger import LedgerEntry
from app.models.vault import DetokenizationAudit, VaultEntry
from app.services import vault as vault_svc


def test_shred_nulls_wrapped_dek_and_sets_shredded_at(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    vault_svc.crypto_shred(db_session, token, reason="test erasure")

    db_session.expire_all()
    row = db_session.get(VaultEntry, token)
    assert row.wrapped_dek is None
    assert row.dek_nonce is None
    assert row.dek_auth_tag is None
    assert row.shredded_at is not None


def test_detokenize_after_shred_raises_shredded_error(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    vault_svc.crypto_shred(db_session, token, reason="test erasure")

    with pytest.raises(ShreddedError):
        vault_svc.detokenize(db_session, token, caller="test-caller", purpose="post-shred check")


def test_shredded_value_unrecoverable_with_kek_and_hsk(db_session, vault_keys):
    from app.vault import crypto, tokens
    from app.vault.keys import get_key_provider

    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    vault_svc.crypto_shred(db_session, token, reason="test erasure")

    db_session.expire_all()
    row = db_session.get(VaultEntry, token)

    # Holding BOTH real keys does not help: the DEK itself is gone.
    provider = get_key_provider()
    kek = provider.get_kek(row.kek_version)
    hsk = provider.get_hsk()
    assert kek is not None
    assert hsk is not None
    assert row.wrapped_dek is None  # nothing left to unwrap with the KEK
    # There is no path from (ciphertext, KEK, HSK) to plaintext once
    # wrapped_dek is gone — the DEK was never stored anywhere else.


def test_ledger_entry_referencing_shredded_token_still_exists(db_session, vault_keys):
    from datetime import date

    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    entry = LedgerEntry(
        effective_date=date.today(),
        type="DECISION",
        source="USER_STATED",
        description=f"note referencing {token}",
        effect=[],
    )
    db_session.add(entry)
    db_session.commit()
    entry_id = entry.id

    vault_svc.crypto_shred(db_session, token, reason="test erasure")

    db_session.expire_all()
    still_there = db_session.get(LedgerEntry, entry_id)
    assert still_there is not None
    assert token in still_there.description


def test_shred_writes_audit_row(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    before = db_session.query(DetokenizationAudit).count()
    vault_svc.crypto_shred(db_session, token, reason="test erasure")
    after = db_session.query(DetokenizationAudit).count()
    assert after == before + 1

    row = db_session.query(DetokenizationAudit).filter_by(token_id=token).one()
    assert row.granted is False
    assert "SHREDDED" in row.denial_reason


def test_tokenize_after_shred_raises_rather_than_resurrecting(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    vault_svc.crypto_shred(db_session, token, reason="test erasure")

    with pytest.raises(ShreddedError):
        vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")

    # allow_resurrect=True is the explicit, documented escape hatch.
    resurrected = vault_svc.tokenize_field(
        db_session, VaultFieldType.NAME, "Ramya G", allow_resurrect=True
    )
    assert resurrected == token
