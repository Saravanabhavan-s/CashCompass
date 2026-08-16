"""Per-field_type canonicalization. Runs before every tokenize() call.

Without normalization, "Ramya G" and "ramya  g" would produce different
tokens and the same person would look like two different people to any
cross-entry reasoning downstream. Two limitations are deliberate, not
oversights — both documented per field below:

  - ADDRESS is under-normalized on purpose: "MG Rd" and "M.G. Road" produce
    different tokens (a false negative — one person looks like two).
    Aggressive canonicalization would instead produce FALSE POSITIVES — two
    different people merged into one identity — which is worse in a privacy
    vault. Under-matching is the safe failure direction.
  - NAME does not strip honorifics ("Mr Ramya G" != "Ramya G"), same
    rationale.
  - EMAIL does not strip Gmail dots/+tags: they are the same inbox but not
    the same string identity, and a Gmail-specific rule would silently merge
    distinct legal identities on non-Gmail domains.
"""
from __future__ import annotations

import re
import unicodedata

from app.enums import VaultFieldType
from app.exceptions import NormalizationError

_WS_RE = re.compile(r"\s+")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_ACCOUNT_RE = re.compile(r"^[0-9A-Z]{6,34}$")
_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]")
_ADDRESS_PUNCT_RE = re.compile(r"[.,#]")

_EMPLOYER_SUFFIXES = [
    (re.compile(r"\bpvt\.?\s*ltd\.?$", re.IGNORECASE), "private limited"),
    (re.compile(r"\bprivate\s+ltd\.?$", re.IGNORECASE), "private limited"),
    (re.compile(r"\bltd\.?$", re.IGNORECASE), "limited"),
    (re.compile(r"\bco\.?$", re.IGNORECASE), "company"),
    (re.compile(r"\binc\.?$", re.IGNORECASE), "inc"),
    (re.compile(r"\bllp\.?$", re.IGNORECASE), "llp"),
]


def _collapse(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def _base(s: str) -> str:
    return _collapse(unicodedata.normalize("NFKC", s))


def _require_nonempty(s: str, field_type: VaultFieldType) -> str:
    if s is None or not str(s).strip():
        raise NormalizationError(f"refusing to tokenize an empty {field_type.value} value")
    return str(s)


def _norm_name(value: str) -> str:
    return _base(value).casefold()


def _norm_pan(value: str) -> str:
    compact = _NON_ALNUM_RE.sub("", unicodedata.normalize("NFKC", value)).upper()
    if not _PAN_RE.match(compact):
        raise NormalizationError(f"invalid PAN format: {compact!r} does not match AAAAA9999A")
    return compact


def _norm_phone(value: str) -> str:
    kept = re.sub(r"[^\d+]", "", value)
    has_plus = kept.startswith("+")
    digits = kept.lstrip("+")

    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    if len(digits) == 11 and digits[0] == "0":
        return f"+91{digits[1:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if has_plus and 8 <= len(digits) <= 15:
        return f"+{digits}"
    raise NormalizationError(f"unrecognized phone number format: {value!r}")


def _norm_email(value: str) -> str:
    email = _base(value).casefold()
    if email.count("@") != 1:
        raise NormalizationError(f"invalid email format: {value!r}")
    local, domain = email.split("@")
    if not local or "." not in domain:
        raise NormalizationError(f"invalid email format: {value!r}")
    return f"{local}@{domain}"


def _norm_account(value: str) -> str:
    compact = _NON_ALNUM_RE.sub("", unicodedata.normalize("NFKC", value)).upper()
    if not _ACCOUNT_RE.match(compact):
        raise NormalizationError(f"invalid account number format: {compact!r}")
    return compact


def _norm_employer(value: str) -> str:
    name = _base(value).casefold()
    name = name.rstrip(".,")
    for pattern, replacement in _EMPLOYER_SUFFIXES:
        name = pattern.sub(replacement, name)
    return _collapse(name)


def _norm_address(value: str) -> str:
    name = _base(value).casefold()
    name = _ADDRESS_PUNCT_RE.sub("", name)
    return _collapse(name)


def _norm_freeform(value: str) -> str:
    return _base(value).casefold()


_NORMALIZERS = {
    VaultFieldType.NAME: _norm_name,
    VaultFieldType.PAN: _norm_pan,
    VaultFieldType.PHONE: _norm_phone,
    VaultFieldType.EMAIL: _norm_email,
    VaultFieldType.ACCOUNT: _norm_account,
    VaultFieldType.EMPLOYER: _norm_employer,
    VaultFieldType.ADDRESS: _norm_address,
    VaultFieldType.MEDICAL: _norm_freeform,
    VaultFieldType.RELATION: _norm_freeform,
    VaultFieldType.NOTE: _norm_freeform,
}


def normalize(field_type: VaultFieldType, value: str) -> str:
    raw = _require_nonempty(value, field_type)
    return _NORMALIZERS[field_type](raw)
