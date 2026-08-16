from app.enums import VaultFieldType
from app.vault import crypto
from app.vault.tokens import (
    TOKEN_RE,
    build_data_aad,
    build_wrap_aad,
    is_token,
    make_token,
    parse_token,
    tenant_hsk,
)


def _hsk():
    return crypto.new_key()


def test_make_token_is_deterministic():
    hsk = _hsk()
    t1 = make_token(hsk, VaultFieldType.NAME, "Ramya G", "default")
    t2 = make_token(hsk, VaultFieldType.NAME, "Ramya G", "default")
    assert t1 == t2


def test_make_token_normalization_equivalence():
    hsk = _hsk()
    assert make_token(hsk, VaultFieldType.NAME, "Ramya G", "default") == make_token(
        hsk, VaultFieldType.NAME, "ramya  g", "default"
    )


def test_make_token_type_separation():
    hsk = _hsk()
    name_token = make_token(hsk, VaultFieldType.NAME, "acme corp", "default")
    employer_token = make_token(hsk, VaultFieldType.EMPLOYER, "acme corp", "default")
    assert name_token != employer_token


def test_make_token_is_type_prefixed_with_16_hex_digest():
    hsk = _hsk()
    token = make_token(hsk, VaultFieldType.NAME, "Ramya G", "default")
    match = TOKEN_RE.match(token)
    assert match is not None
    assert match.group("field_type") == "NAME"
    assert len(match.group("digest")) == 16


def test_make_token_differs_across_tenants():
    hsk = _hsk()
    t_a = make_token(hsk, VaultFieldType.PAN, "ABCDE1234F", "tenant-a")
    t_b = make_token(hsk, VaultFieldType.PAN, "ABCDE1234F", "tenant-b")
    assert t_a != t_b


def test_tenant_hsk_is_deterministic_per_tenant():
    hsk = _hsk()
    assert tenant_hsk(hsk, "default") == tenant_hsk(hsk, "default")
    assert tenant_hsk(hsk, "default") != tenant_hsk(hsk, "other")


def test_parse_token_round_trip():
    hsk = _hsk()
    token = make_token(hsk, VaultFieldType.EMAIL, "a@b.com", "default")
    field_type, digest = parse_token(token)
    assert field_type == VaultFieldType.EMAIL
    assert len(digest) == 16


def test_is_token():
    hsk = _hsk()
    token = make_token(hsk, VaultFieldType.NAME, "x", "default")
    assert is_token(token)
    assert not is_token("plain text value")
    assert not is_token(12345)


def test_build_data_aad_deterministic():
    aad1 = build_data_aad("TKN_NAME_abc", VaultFieldType.NAME, 1, "default")
    aad2 = build_data_aad("TKN_NAME_abc", VaultFieldType.NAME, 1, "default")
    assert aad1 == aad2
    assert aad1 != build_data_aad("TKN_NAME_abc", VaultFieldType.NAME, 2, "default")


def test_build_wrap_aad_excludes_kek_version():
    # wrap AAD is stable across rotation by construction — it takes no kek_version arg.
    aad1 = build_wrap_aad("TKN_NAME_abc", "default")
    aad2 = build_wrap_aad("TKN_NAME_abc", "default")
    assert aad1 == aad2
