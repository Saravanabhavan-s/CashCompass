from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_vault_admin
from app.enums import SensitivityTier
from app.exceptions import VaultError
from app.schemas.sensitive import SensitiveProfileRead, SensitiveProfileRevealed, SensitiveProfileUpsert
from app.services import sensitive as svc
from app.services import vault as vault_svc

router = APIRouter(prefix="/profile", tags=["profile"])


@router.post(
    "/sensitive",
    response_model=SensitiveProfileRead,
    status_code=201,
)
def upsert_sensitive_profile(body: SensitiveProfileUpsert, db: Session = Depends(get_db)):
    profile = svc.upsert_sensitive_profile(db, body)
    return SensitiveProfileRead(
        id=profile.id,
        user_id=profile.user_id,
        sensitivity_tier=SensitivityTier(profile.sensitivity_tier),
        medical_conditions=profile.medical_conditions,
        dependents=profile.dependents,
        family_commitments=profile.family_commitments,
        obligations=profile.obligations,
        vault_protected=profile.vault_protected,
    )


class RevealRequest(BaseModel):
    user_id: str
    caller: str = Field(min_length=1)
    purpose: str = Field(min_length=1)


@router.post(
    "/sensitive/reveal",
    response_model=SensitiveProfileRevealed,
    dependencies=[Depends(require_vault_admin)],
)
def reveal_sensitive_profile(body: RevealRequest, db: Session = Depends(get_db)):
    """USER_FACING egress destination for SensitiveProfile — the only route
    that returns plaintext. Every token revealed here writes one audit row
    (app/services/vault.py:detokenize)."""
    profile = svc.get_sensitive_profile(db, body.user_id)
    if profile is None:
        raise HTTPException(404, "no sensitive profile for that user_id")

    payload = {
        "medical_conditions": profile.medical_conditions,
        "dependents": profile.dependents,
        "family_commitments": profile.family_commitments,
        "obligations": profile.obligations,
    }
    try:
        revealed = vault_svc.detokenize_payload(db, payload, caller=body.caller, purpose=body.purpose)
    except VaultError as exc:
        raise HTTPException(410, str(exc)) from exc

    dependents = None
    if revealed["dependents"]:
        dependents = [
            {"relation": d["relation"], "age": d["age"], "notes": d.pop("notes_token", None)}
            for d in revealed["dependents"]
        ]

    return SensitiveProfileRevealed(
        id=profile.id,
        user_id=profile.user_id,
        medical_conditions=revealed["medical_conditions"],
        dependents=dependents,
        family_commitments=revealed["family_commitments"],
        obligations=revealed["obligations"],
    )
