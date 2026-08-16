from typing import Generator

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.vault.crypto import constant_time_eq


def get_db() -> Generator[Session, None, None]:
    yield from get_session()


def require_vault_admin(x_vault_admin_key: str | None = Header(default=None, alias="X-Vault-Admin-Key")) -> str:
    """PROTOTYPE AUTH — a shared secret, not an identity.

    Layer 1 has no user/auth model, so there is nothing to authenticate
    against. This proves the *shape* of the control (privileged vault ops
    are gated, the gate is centralized, the check is constant-time) without
    claiming to be authentication. Production replaces this with an
    authenticated principal plus per-purpose authorization; `caller` on
    POST /vault/detokenize would then be DERIVED FROM THE TOKEN, not
    self-asserted as it is here.
    """
    configured = settings.vault_admin_key.get_secret_value()
    if not configured:
        raise HTTPException(503, "Vault admin key is not configured; refusing to serve.")
    if not x_vault_admin_key or not constant_time_eq(x_vault_admin_key, configured):
        raise HTTPException(401, "Missing or invalid X-Vault-Admin-Key.")
    return "admin"
