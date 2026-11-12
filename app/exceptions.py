class AppendOnlyViolation(RuntimeError):
    """Raised when code attempts to mutate or delete an append-only row
    (LedgerEntry, DetokenizationAudit)."""


class HurdleNotFound(LookupError):
    """No hurdle exists for the given id."""


class VaultError(RuntimeError):
    """Base class for all Layer 2 vault errors."""


class KeyConfigurationError(VaultError):
    """A key is missing, the wrong length, or otherwise unusable. Fail closed."""


class NormalizationError(VaultError):
    """A value could not be normalized (empty, or fails a field_type-specific
    format check such as PAN/phone)."""


class TokenNotFoundError(VaultError):
    """No vault_entries row exists for the given token_id."""


class ShreddedError(VaultError):
    """The token's DEK has been crypto-shredded; the plaintext is permanently
    unrecoverable."""


class DecryptionError(VaultError):
    """GCM authentication failed: tampering, a swapped row, or a wrong key —
    not a decode error. Wraps cryptography.exceptions.InvalidTag."""


class CleartextLeakError(VaultError):
    """A VaultProtected field held (or would have emitted) cleartext."""


class EgressPolicyViolation(VaultError):
    """The egress scanner found a leak in an outgoing payload."""


class UnauthorizedVaultAccess(VaultError):
    """A detokenize call was denied by the caller allowlist. Distinct from a
    missing/wrong admin key (that's a FastAPI-level 401, checked before any
    service code runs) — this is a business-logic denial that still gets an
    audit row."""


class EvidenceSourceError(RuntimeError):
    """A live evidence source (Alpha Vantage, FRED) returned an error, a
    rate-limit notice, or nothing usable. Raised instead of returning an empty
    result so a failed call can never be mistaken for "no evidence"."""


class LLMError(RuntimeError):
    """An LLM provider call failed (transport, HTTP status, malformed body).
    Messages never contain credentials."""


class LLMNotConfigured(LLMError):
    """No usable LLM provider is configured. Fail closed: never fall back to
    a fake answer."""


class DebateFailed(RuntimeError):
    """A debate round produced zero valid agent outputs."""


class DebateInputLeak(EgressPolicyViolation):
    """The leak scanner found user data in the finished debate case. Nothing
    was sent to any model."""
