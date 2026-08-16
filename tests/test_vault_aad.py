import pytest
from sqlalchemy import text

from app.enums import VaultFieldType
from app.exceptions import DecryptionError
from app.services import vault as vault_svc


def test_swapping_two_rows_ciphertext_breaks_both(db_session, vault_keys):
    token_a = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    token_b = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Priya K")

    row_a = db_session.execute(
        text("SELECT ciphertext, nonce, auth_tag FROM vault_entries WHERE token_id = :t"), {"t": token_a}
    ).first()
    row_b = db_session.execute(
        text("SELECT ciphertext, nonce, auth_tag FROM vault_entries WHERE token_id = :t"), {"t": token_b}
    ).first()

    # Raw-SQL row-swap attack: copy A's ciphertext/nonce/auth_tag into B's row
    # and vice versa. Both rows still decrypt with the correct DEK/KEK — only
    # the AAD (which is bound to token_id) can catch this.
    db_session.execute(
        text("UPDATE vault_entries SET ciphertext = :ct, nonce = :n, auth_tag = :tag WHERE token_id = :t"),
        {"ct": row_b.ciphertext, "n": row_b.nonce, "tag": row_b.auth_tag, "t": token_a},
    )
    db_session.execute(
        text("UPDATE vault_entries SET ciphertext = :ct, nonce = :n, auth_tag = :tag WHERE token_id = :t"),
        {"ct": row_a.ciphertext, "n": row_a.nonce, "tag": row_a.auth_tag, "t": token_b},
    )
    db_session.commit()

    with pytest.raises(DecryptionError):
        vault_svc.detokenize(db_session, token_a, caller="test-caller", purpose="aad test")
    with pytest.raises(DecryptionError):
        vault_svc.detokenize(db_session, token_b, caller="test-caller", purpose="aad test")


def test_tenant_mismatch_in_aad_fails_decryption(db_session, vault_keys):
    from app.vault import crypto, tokens
    from app.vault.keys import get_key_provider

    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G", tenant_id="default")
    provider = get_key_provider()
    from app.models.vault import VaultEntry

    row = db_session.get(VaultEntry, token)
    kek = provider.get_kek(row.kek_version)
    wrap_aad = tokens.build_wrap_aad(token, row.tenant_id)
    dek = crypto.unwrap_dek(kek, row.wrapped_dek, row.dek_nonce, row.dek_auth_tag, wrap_aad)

    wrong_aad = tokens.build_data_aad(token, VaultFieldType.NAME, row.key_version, "other-tenant")
    with pytest.raises(DecryptionError):
        crypto.decrypt(dek, row.ciphertext, row.nonce, row.auth_tag, wrong_aad)


def test_field_type_mismatch_in_aad_fails_decryption(db_session, vault_keys):
    from app.vault import crypto, tokens
    from app.vault.keys import get_key_provider
    from app.models.vault import VaultEntry

    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    provider = get_key_provider()
    row = db_session.get(VaultEntry, token)
    kek = provider.get_kek(row.kek_version)
    wrap_aad = tokens.build_wrap_aad(token, row.tenant_id)
    dek = crypto.unwrap_dek(kek, row.wrapped_dek, row.dek_nonce, row.dek_auth_tag, wrap_aad)

    wrong_aad = tokens.build_data_aad(token, VaultFieldType.EMPLOYER, row.key_version, row.tenant_id)
    with pytest.raises(DecryptionError):
        crypto.decrypt(dek, row.ciphertext, row.nonce, row.auth_tag, wrong_aad)
