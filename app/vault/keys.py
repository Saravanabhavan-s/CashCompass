"""KeyProvider — custody boundary for the KEK(s) and HSK.

Two implementations:
  LocalKeyProvider — prototype. Keys live in process memory, sourced from an
    env-injected secret (or, optionally, the OS keyring). This is a documented
    prototype limitation, not a production design — see KmsKeyProvider's
    docstring for the production path.
  KmsKeyProvider    — stubbed. Documents where a real deployment would put
    key custody instead.

Resolution is LAZY (get_key_provider() builds on first use, not at import),
because `settings` is a module-level singleton built at import time
(app/config.py), and env vars set by a test after that import would otherwise
be invisible.
"""
from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod

from app.config import settings
from app.exceptions import KeyConfigurationError

KEY_LEN = 32


def _decode_key(raw: str, *, what: str) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise KeyConfigurationError(f"{what} is not valid base64.") from exc
    if len(key) != KEY_LEN:
        raise KeyConfigurationError(
            f"{what} must decode to exactly {KEY_LEN} bytes, got {len(key)}."
        )
    return key


class KeyProvider(ABC):
    @abstractmethod
    def current_kek_version(self) -> int: ...

    @abstractmethod
    def get_kek(self, version: int) -> bytes: ...

    @abstractmethod
    def get_hsk(self) -> bytes: ...

    @abstractmethod
    def known_kek_versions(self) -> list[int]: ...


class LocalKeyProvider(KeyProvider):
    """Prototype key custody. KEK(s) and HSK live in process memory for the
    lifetime of the process, sourced from env-injected base64 secrets (or the
    OS keyring, if configured). This is explicitly NOT production-grade — see
    KmsKeyProvider for the production path. Documented in the README threat
    model as a deliberate prototype limitation.
    """

    def __init__(self, keks: dict[int, bytes], current: int, hsk: bytes):
        if current not in keks:
            raise KeyConfigurationError(
                f"current KEK version {current} has no corresponding key."
            )
        self._keks = dict(keks)
        self._current = current
        self._hsk = hsk

    def current_kek_version(self) -> int:
        return self._current

    def get_kek(self, version: int) -> bytes:
        try:
            return self._keks[version]
        except KeyError:
            raise KeyConfigurationError(f"unknown KEK version: {version}") from None

    def get_hsk(self) -> bytes:
        return self._hsk

    def known_kek_versions(self) -> list[int]:
        return sorted(self._keks)

    def add_kek(self, version: int, key: bytes, *, make_current: bool = False) -> None:
        """Register a new KEK version (used by rotate_kek). Retaining
        superseded versions is required — rotation unwraps old rows with the
        old version and re-wraps with the new one."""
        if len(key) != KEY_LEN:
            raise KeyConfigurationError(f"KEK must be {KEY_LEN} bytes, got {len(key)}.")
        self._keks[version] = key
        if make_current:
            self._current = version


class KmsKeyProvider(KeyProvider):
    """Stubbed production path. NOT implemented — this class documents where
    real key custody would live, it does not provide it.

    Under a KMS/HSM the KEK never leaves the boundary: get_kek() would not
    exist at all. wrap_dek/unwrap_dek become kms.Encrypt(KeyId, Plaintext=DEK)
    / kms.Decrypt(CiphertextBlob=wrapped_dek) calls against a key id, and
    rotation becomes kms.UpdateAlias plus a re-wrap sweep — the app process
    never holds KEK bytes, only a briefly-live DEK in memory per operation.

    The HSK cannot live in a KMS the same way: HMAC needs the key in-process
    for every blind-index lookup, not just for a wrap/unwrap event. A real
    deployment would use kms.GenerateDataKey with a cached, memory-locked
    plaintext copy and a short TTL, or a dedicated HMAC key type (e.g. AWS KMS
    HMAC_256), never a bare env var.

    The KeyProvider interface exists so that swap is a one-class change —
    which is the only reason the prototype's LocalKeyProvider is defensible.
    """

    def current_kek_version(self) -> int:
        raise NotImplementedError("KmsKeyProvider is a documented stub. See class docstring.")

    def get_kek(self, version: int) -> bytes:
        raise NotImplementedError("KmsKeyProvider is a documented stub. See class docstring.")

    def get_hsk(self) -> bytes:
        raise NotImplementedError("KmsKeyProvider is a documented stub. See class docstring.")

    def known_kek_versions(self) -> list[int]:
        raise NotImplementedError("KmsKeyProvider is a documented stub. See class docstring.")


def _build_local_provider() -> LocalKeyProvider:
    if settings.vault_key_source == "keyring":
        return _build_keyring_provider()

    keks_raw = settings.vault_keks.get_secret_value()
    hsk_raw = settings.vault_hsk.get_secret_value()
    if not keks_raw or not hsk_raw:
        raise KeyConfigurationError(
            "VAULT_KEKS and VAULT_HSK must be configured. "
            "Run scripts/gen_vault_keys.py to generate them."
        )
    try:
        keks_json: dict = json.loads(keks_raw)
    except json.JSONDecodeError as exc:
        raise KeyConfigurationError("VAULT_KEKS must be a JSON object.") from exc

    keks = {int(version): _decode_key(b64, what=f"VAULT_KEKS[{version}]") for version, b64 in keks_json.items()}
    hsk = _decode_key(hsk_raw, what="VAULT_HSK")
    return LocalKeyProvider(keks=keks, current=settings.vault_kek_version, hsk=hsk)


def _build_keyring_provider() -> LocalKeyProvider:
    """Optional prototype source: OS keyring instead of env vars. `keyring`
    is an optional extra (`pip install -e ".[keyring]"`), never a hard dep —
    imported lazily so the default env-based path never requires it."""
    try:
        import keyring
    except ImportError as exc:
        raise KeyConfigurationError(
            "VAULT_KEY_SOURCE=keyring requires the 'keyring' extra: "
            'pip install -e ".[keyring]"'
        ) from exc

    current = settings.vault_kek_version
    keks: dict[int, bytes] = {}
    for version in range(1, current + 1):
        raw = keyring.get_password("cashcompass", f"kek_v{version}")
        if raw:
            keks[version] = _decode_key(raw, what=f"keyring kek_v{version}")
    hsk_raw = keyring.get_password("cashcompass", "hsk")
    if current not in keks or not hsk_raw:
        raise KeyConfigurationError(
            f"OS keyring is missing 'cashcompass/kek_v{current}' or 'cashcompass/hsk'."
        )
    return LocalKeyProvider(keks=keks, current=current, hsk=_decode_key(hsk_raw, what="keyring hsk"))


_provider: KeyProvider | None = None


def get_key_provider() -> KeyProvider:
    global _provider
    if _provider is None:
        if settings.vault_key_source == "kms":
            _provider = KmsKeyProvider()
        elif settings.vault_key_source in ("env", "keyring"):
            _provider = _build_local_provider()
        else:  # pragma: no cover — guarded by Settings' Literal type
            raise KeyConfigurationError(f"unknown vault_key_source: {settings.vault_key_source!r}")
    return _provider


def reset_key_provider() -> None:
    """Test-only escape hatch: forces the next get_key_provider() call to
    rebuild from current settings/env."""
    global _provider
    _provider = None
