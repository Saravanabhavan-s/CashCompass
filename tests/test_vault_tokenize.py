from app.enums import VaultFieldType
from app.models.vault import VaultEntry
from app.services import vault as vault_svc


def test_tokenize_twice_returns_identical_token(db_session, vault_keys):
    t1 = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    t2 = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    assert t1 == t2


def test_tokenize_is_idempotent_creates_one_row(db_session, vault_keys):
    vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    count = db_session.query(VaultEntry).count()
    assert count == 1


def test_normalization_variants_produce_same_token(db_session, vault_keys):
    t1 = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    t2 = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "ramya  g")
    assert t1 == t2
    assert db_session.query(VaultEntry).count() == 1


def test_same_string_different_field_type_yields_different_tokens(db_session, vault_keys):
    t_name = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "acme corp")
    t_employer = vault_svc.tokenize_field(db_session, VaultFieldType.EMPLOYER, "acme corp")
    assert t_name != t_employer


def test_token_is_type_prefixed_with_16_hex_chars(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    assert token.startswith("TKN_NAME_")
    assert len(token.split("_")[-1]) == 16


def test_tokenize_detokenize_returns_exact_original(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.PAN, "ABCDE1234F")
    value = vault_svc.detokenize(db_session, token, caller="test-caller", purpose="unit test")
    assert value == "ABCDE1234F"


def test_vault_row_stores_no_plaintext(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    row = db_session.get(VaultEntry, token)
    assert b"Ramya" not in row.ciphertext
    assert b"ramya" not in row.ciphertext.lower()
