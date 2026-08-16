from app.enums import VaultFieldType
from app.models.vault import VaultEntry
from app.services import vault as vault_svc
from app.vault import crypto


def test_all_values_still_decrypt_after_rotation(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    new_kek = crypto.new_key()
    from app.vault.keys import get_key_provider

    get_key_provider().add_kek(2, new_kek)  # register without making current, mirrors API flow
    vault_svc.rotate_kek(db_session, new_kek, new_version=2)

    value = vault_svc.detokenize(db_session, token, caller="test-caller", purpose="rotation test")
    assert value == "Ramya G"


def test_rotate_kek_leaves_data_columns_unchanged(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    row_before = db_session.get(VaultEntry, token)
    before = {
        "ciphertext": row_before.ciphertext,
        "nonce": row_before.nonce,
        "auth_tag": row_before.auth_tag,
        "key_version": row_before.key_version,
    }
    db_session.expire_all()

    new_kek = crypto.new_key()
    vault_svc.rotate_kek(db_session, new_kek, new_version=2)

    db_session.expire_all()
    row_after = db_session.get(VaultEntry, token)
    assert row_after.ciphertext == before["ciphertext"]
    assert row_after.nonce == before["nonce"]
    assert row_after.auth_tag == before["auth_tag"]
    assert row_after.key_version == before["key_version"]


def test_rotate_kek_updates_wrapped_dek_and_kek_version(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    row_before = db_session.get(VaultEntry, token)
    wrapped_before = row_before.wrapped_dek
    db_session.expire_all()

    new_kek = crypto.new_key()
    vault_svc.rotate_kek(db_session, new_kek, new_version=2)

    db_session.expire_all()
    row_after = db_session.get(VaultEntry, token)
    assert row_after.wrapped_dek != wrapped_before
    assert row_after.kek_version == 2


def test_rotate_kek_is_idempotent_when_already_current(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    new_kek = crypto.new_key()
    result1 = vault_svc.rotate_kek(db_session, new_kek, new_version=2)
    assert result1["rewrapped"] == 1

    result2 = vault_svc.rotate_kek(db_session, new_kek, new_version=2)
    assert result2["rewrapped"] == 0  # already on version 2, nothing to do

    value = vault_svc.detokenize(db_session, token, caller="test-caller", purpose="idempotent rotation")
    assert value == "Ramya G"


def test_rotate_kek_skips_shredded_rows(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    vault_svc.crypto_shred(db_session, token, reason="test")

    new_kek = crypto.new_key()
    result = vault_svc.rotate_kek(db_session, new_kek, new_version=2)
    assert result["rewrapped"] == 0

    row = db_session.get(VaultEntry, token)
    assert row.wrapped_dek is None
    assert row.kek_version == 1  # untouched — shredded rows are skipped


def test_old_kek_still_needed_until_all_rows_rewrapped(db_session, vault_keys):
    vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    new_kek = crypto.new_key()
    result = vault_svc.rotate_kek(db_session, new_kek, new_version=2)
    assert result["remaining_on_old"] == 0  # single row, fully rewrapped
