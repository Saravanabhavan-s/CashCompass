from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_vault_admin
from app.exceptions import ShreddedError, TokenNotFoundError, UnauthorizedVaultAccess
from app.schemas.vault import (
    AuditEntry,
    AuditPage,
    DetokenizeRequest,
    DetokenizeResponse,
    RotateKekRequest,
    RotateKekResponse,
    ShredRequest,
    ShredResponse,
    TokenizeRequest,
    TokenizeResponse,
)
from app.services import vault as vault_svc
from app.vault import crypto

router = APIRouter(prefix="/vault", tags=["vault"])


@router.post("/tokenize", response_model=TokenizeResponse, status_code=201, dependencies=[Depends(require_vault_admin)])
def tokenize(body: TokenizeRequest, db: Session = Depends(get_db)):
    from app.config import settings

    tenant_id = body.tenant_id or settings.default_tenant_id
    token_id = vault_svc.tokenize_field(db, body.field_type, body.value, tenant_id)
    return TokenizeResponse(token_id=token_id, field_type=body.field_type, tenant_id=tenant_id)


@router.post("/detokenize", response_model=DetokenizeResponse)
def detokenize(body: DetokenizeRequest, db: Session = Depends(get_db)):
    try:
        value = vault_svc.detokenize(db, body.token_id, caller=body.caller, purpose=body.purpose)
    except UnauthorizedVaultAccess as exc:
        raise HTTPException(403, str(exc)) from exc
    except TokenNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ShreddedError as exc:
        raise HTTPException(410, str(exc)) from exc
    return DetokenizeResponse(token_id=body.token_id, value=value)


@router.get("/audit", response_model=AuditPage)
def audit(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    token_id: str | None = None,
    db: Session = Depends(get_db),
):
    items, total = vault_svc.list_audit(db, limit=limit, offset=offset, token_id=token_id)
    return AuditPage(
        items=[AuditEntry.model_validate(item) for item in items], total=total, limit=limit, offset=offset
    )


@router.post(
    "/shred/{token_id}",
    response_model=ShredResponse,
    dependencies=[Depends(require_vault_admin)],
)
def shred(token_id: str, body: ShredRequest, db: Session = Depends(get_db)):
    try:
        vault_svc.crypto_shred(db, token_id, reason=body.reason, caller="admin")
    except TokenNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    from app.models.vault import VaultEntry

    row = db.get(VaultEntry, token_id)
    return ShredResponse(token_id=token_id, shredded_at=row.shredded_at, reason=body.reason)


@router.post(
    "/rotate-kek",
    response_model=RotateKekResponse,
    dependencies=[Depends(require_vault_admin)],
)
def rotate_kek(body: RotateKekRequest, db: Session = Depends(get_db)):
    """Never accepts raw key material — only a target version number. A
    fresh KEK is generated server-side and registered under that version; a
    key in a request body would land in access logs, proxy buffers, and
    FastAPI validation error messages. (app/services/vault.py:rotate_kek
    keeps `new_kek: bytes` as a parameter for testability — only this HTTP
    layer is restricted.)"""
    new_kek = crypto.new_key()
    result = vault_svc.rotate_kek(db, new_kek, body.new_version)
    return RotateKekResponse(**result)
