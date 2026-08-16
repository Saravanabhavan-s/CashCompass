from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sensitive import SensitiveProfile
from app.schemas.sensitive import SensitiveProfileUpsert


def upsert_sensitive_profile(db: Session, data: SensitiveProfileUpsert) -> SensitiveProfile:
    profile = db.scalar(
        select(SensitiveProfile).where(SensitiveProfile.user_id == data.user_id)
    )
    if profile:
        profile.medical_conditions = data.medical_conditions
        profile.dependents = (
            [d.model_dump() for d in data.dependents] if data.dependents else None
        )
        profile.family_commitments = data.family_commitments
        profile.obligations = data.obligations
    else:
        profile = SensitiveProfile(
            user_id=data.user_id,
            medical_conditions=data.medical_conditions,
            dependents=(
                [d.model_dump() for d in data.dependents] if data.dependents else None
            ),
            family_commitments=data.family_commitments,
            obligations=data.obligations,
        )
        db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile
