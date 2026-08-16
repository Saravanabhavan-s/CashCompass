import pytest

from app.enums import VaultFieldType
from app.exceptions import NormalizationError
from app.vault.normalize import normalize


def test_name_variants_normalize_identically():
    assert normalize(VaultFieldType.NAME, "Ramya G") == normalize(VaultFieldType.NAME, "ramya  g")
    assert normalize(VaultFieldType.NAME, "Ramya G") == "ramya g"


def test_name_nfkc_width_variants():
    # full-width "Ｇ" vs ascii "G" should normalize to the same codepoint after NFKC
    assert normalize(VaultFieldType.NAME, "Ramya Ｇ") == normalize(VaultFieldType.NAME, "Ramya G")


def test_pan_uppercased_and_stripped():
    assert normalize(VaultFieldType.PAN, " abcde1234f ") == "ABCDE1234F"


def test_invalid_pan_rejected():
    with pytest.raises(NormalizationError):
        normalize(VaultFieldType.PAN, "not-a-pan")


def test_phone_e164_india_variants():
    variants = ["9876543210", "+91-98765-43210", "098765 43210", "919876543210", "+919876543210"]
    normalized = {normalize(VaultFieldType.PHONE, v) for v in variants}
    assert normalized == {"+919876543210"}


def test_phone_invalid_rejected():
    with pytest.raises(NormalizationError):
        normalize(VaultFieldType.PHONE, "123")


def test_email_lowercased():
    assert normalize(VaultFieldType.EMAIL, " Ramya.G@Gmail.COM ") == "ramya.g@gmail.com"


def test_email_dots_and_plus_tags_preserved():
    assert normalize(VaultFieldType.EMAIL, "ramya.g@gmail.com") != normalize(
        VaultFieldType.EMAIL, "ramyag@gmail.com"
    )
    assert normalize(VaultFieldType.EMAIL, "ramya+tag@gmail.com") != normalize(
        VaultFieldType.EMAIL, "ramya@gmail.com"
    )


def test_email_invalid_rejected():
    with pytest.raises(NormalizationError):
        normalize(VaultFieldType.EMAIL, "not-an-email")


def test_account_preserves_leading_zeros():
    assert normalize(VaultFieldType.ACCOUNT, "0001 2345 6789") == "000123456789"


def test_employer_legal_suffixes_canonicalized():
    assert normalize(VaultFieldType.EMPLOYER, "Infosys Pvt. Ltd.") == "infosys private limited"
    assert normalize(VaultFieldType.EMPLOYER, "Acme Ltd") == "acme limited"


def test_address_punctuation_collapsed():
    assert normalize(VaultFieldType.ADDRESS, "#12, MG Rd., Bengaluru") == "12 mg rd bengaluru"


def test_empty_value_rejected():
    for field_type in VaultFieldType:
        with pytest.raises(NormalizationError):
            normalize(field_type, "   ")


def test_type_separation_same_string_different_normalized_form_still_distinct_tokens_upstream():
    # Normalization alone doesn't merge field types — different normalizers can
    # legitimately produce the same string; token separation is guaranteed by
    # tokens.py including field_type in the HMAC input, not by normalize().
    assert normalize(VaultFieldType.MEDICAL, "Chemo") == normalize(VaultFieldType.NOTE, "Chemo")
