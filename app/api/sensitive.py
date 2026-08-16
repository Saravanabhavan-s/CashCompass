from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.sensitive import Dependent, SensitiveProfileRead, SensitiveProfileUpsert
from app.services import sensitive as svc

router = APIRouter(prefix="/profile", tags=["profile"])


@router.post(
    "/sensitive",
    response_model=SensitiveProfileRead,
    status_code=201,
    # NOTE: This endpoint is NOT yet vault-protected.
    # Layer 2 will tokenize identity fields and enforce DPDP-compliant consent checks
    # before data in TIER_HEALTH_FAMILY is stored or returned.
)
def upsert_sensitive_profile(body: SensitiveProfileUpsert, db: Session = Depends(get_db)):
    profile = svc.upsert_sensitive_profile(db, body)
    dependents = None
    if profile.dependents:
        dependents = [Dependent(**d) for d in profile.dependents]
    return SensitiveProfileRead(
        id=profile.id,
        user_id=profile.user_id,
        medical_conditions=profile.medical_conditions,
        dependents=dependents,
        family_commitments=profile.family_commitments,
        obligations=profile.obligations,
        vault_protected=False,
    )
