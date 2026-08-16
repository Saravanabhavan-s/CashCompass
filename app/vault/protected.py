"""VaultProtected — structural guards that make it hard to accidentally leak
cleartext PII, mirroring the ledger's proven append-only defence-in-depth
(app/models/ledger.py) with one more layer added for Pydantic.

Four independent layers, each closing a different bypass of the others:
  (a) Pydantic validation-time  — VaultToken rejects cleartext on construction.
  (b) Pydantic serialization-time — VaultProtectedModel re-checks on dump,
      closing the model_construct()/__dict__ bypass of (a).
  (c) SQLAlchemy flush-time — VaultProtectedMixin + a before_flush listener,
      closing the "somebody built the ORM object directly" bypass of (a)/(b).
  (d) Postgres trigger (see alembic/versions/002_vault_tokenization.py) —
      closes the "raw SQL bypasses the ORM entirely" bypass of (a)/(b)/(c).

LOG HYGIENE RULE, everywhere in this module: errors name the FIELD, never
the VALUE. The offending value is by definition cleartext PII; putting it in
an exception message is how a privacy layer becomes a privacy leak.
"""
from __future__ import annotations

import itertools
from typing import Annotated, Any, ClassVar

from pydantic import AfterValidator, BaseModel, model_serializer
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.exceptions import CleartextLeakError
from app.vault.tokens import TOKEN_RE, is_token


def _must_be_token(v: str) -> str:
    if not isinstance(v, str) or not TOKEN_RE.match(v):
        raise ValueError("vault-protected field must hold a vault token, not cleartext")
    return v


VaultToken = Annotated[str, AfterValidator(_must_be_token)]


def _assert_all_tokens(value: Any, *, field: str, key_suffix: str = "_token") -> None:
    """Recursively verifies every string that should be a token, is one.
    Scalars and lists of scalars are checked directly; for lists of dicts
    (e.g. SensitiveProfile.dependents), only keys ending in `key_suffix` are
    checked — a dependent's `relation`/`age` are intentionally cleartext."""
    if value is None:
        return
    if isinstance(value, str):
        if not is_token(value):
            raise CleartextLeakError(f"field {field!r} must hold a vault token, not cleartext")
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                for k, v in item.items():
                    if k.endswith(key_suffix) and v is not None and not is_token(v):
                        raise CleartextLeakError(f"field {field!r}.{k!r} must hold a vault token, not cleartext")
            elif isinstance(item, str):
                if not is_token(item):
                    raise CleartextLeakError(f"field {field!r} must hold a vault token, not cleartext")
        return
    # Unexpected shape (e.g. a bare dict) — fail closed rather than silently pass.
    raise CleartextLeakError(f"field {field!r} has an unexpected shape for a vault-protected column")


class VaultProtectedModel(BaseModel):
    """Pydantic base for read schemas over vault-protected rows. Subclasses
    declare __vault_protected_fields__; the wrap serializer re-verifies every
    such field is still token-shaped at dump time, closing the
    model_construct()/__dict__-assignment bypass of field validators."""

    __vault_protected_fields__: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _block_cleartext(self, handler):
        data = handler(self)
        for name in self.__vault_protected_fields__:
            if name in data:
                _assert_all_tokens(data[name], field=name)
        return data


class VaultProtectedMixin:
    """SQLAlchemy mixin: declares which JSON columns must contain only vault
    tokens once `vault_protected` is true on the row. Guarded by that flag
    (rather than unconditional) so a not-yet-migrated row is not blocked —
    this is what makes flipping a model like SensitiveProfile incremental."""

    __vault_protected_columns__: ClassVar[tuple[str, ...]] = ()
    __vault_token_key_suffix__: ClassVar[str] = "_token"


@event.listens_for(Session, "before_flush")
def _enforce_vault_protection(session: Session, flush_context, instances):
    for obj in itertools.chain(session.new, session.dirty):
        if isinstance(obj, VaultProtectedMixin) and getattr(obj, "vault_protected", False):
            for col in obj.__vault_protected_columns__:
                _assert_all_tokens(getattr(obj, col), field=col, key_suffix=obj.__vault_token_key_suffix__)
