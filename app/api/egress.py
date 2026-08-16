from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_vault_admin
from app.enums import EgressDestination
from app.schemas.egress import EgressPreviewResponse
from app.services.egress import preview_egress

router = APIRouter(prefix="/egress", tags=["egress"])


@router.get("/preview", response_model=EgressPreviewResponse)
def egress_preview(
    destination: EgressDestination = Query(...),
    user_id: str | None = None,
    caller: str | None = None,
    purpose: str | None = None,
    x_vault_admin_key: str | None = Header(default=None, alias="X-Vault-Admin-Key"),
    db: Session = Depends(get_db),
):
    """DEBATE_AGENT and OPTIMIZER need no admin key — they emit nothing
    sensitive, which is the whole point of this layer, and gating them would
    undercut the demo. USER_FACING requires the admin key plus caller and
    purpose, and is audited (it detokenizes)."""
    if destination == EgressDestination.USER_FACING:
        require_vault_admin(x_vault_admin_key)
        if not caller or not purpose:
            raise HTTPException(422, "USER_FACING preview requires both caller and purpose query params.")

    result = preview_egress(db, destination, user_id=user_id, caller=caller, purpose=purpose)
    return EgressPreviewResponse(**result)
