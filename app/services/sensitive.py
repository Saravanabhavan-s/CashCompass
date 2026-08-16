from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import SensitivityTier, VaultFieldType
from app.models.sensitive import SensitiveProfile
from app.schemas.sensitive import SensitiveProfileUpsert
from app.services import vault as vault_svc


def get_sensitive_profile(db: Session, user_id: str) -> SensitiveProfile | None:
    return db.scalar(select(SensitiveProfile).where(SensitiveProfile.user_id == user_id))


def upsert_sensitive_profile(
    db: Session, data: SensitiveProfileUpsert, tenant_id: str | None = None
) -> SensitiveProfile:
    """Tokenizes every identity-bearing field BEFORE building/committing the
    profile row — tokenize_field commits internally (the Layer 1 service
    convention), so tokenizing first keeps this function's own commit atomic
    for the profile row itself. A tokenized-but-unreferenced vault row from a
    retry is harmless: tokenize_field is idempotent."""
    medical_conditions = (
        [vault_svc.tokenize_field(db, VaultFieldType.MEDICAL, c, tenant_id) for c in data.medical_conditions]
        if data.medical_conditions
        else None
    )
    family_commitments = (
        [vault_svc.tokenize_field(db, VaultFieldType.NOTE, c, tenant_id) for c in data.family_commitments]
        if data.family_commitments
        else None
    )
    obligations = (
        [vault_svc.tokenize_field(db, VaultFieldType.NOTE, o, tenant_id) for o in data.obligations]
        if data.obligations
        else None
    )
    dependents = (
        [
            {
                "relation": d.relation,
                "age": d.age,
                "notes_token": (
                    vault_svc.tokenize_field(db, VaultFieldType.NOTE, d.notes, tenant_id) if d.notes else None
                ),
            }
            for d in data.dependents
        ]
        if data.dependents
        else None
    )

    profile = db.scalar(select(SensitiveProfile).where(SensitiveProfile.user_id == data.user_id))
    if profile:
        profile.medical_conditions = medical_conditions
        profile.dependents = dependents
        profile.family_commitments = family_commitments
        profile.obligations = obligations
        profile.vault_protected = True
        profile.sensitivity_tier = SensitivityTier.TIER_HEALTH_FAMILY.value
    else:
        profile = SensitiveProfile(
            user_id=data.user_id,
            medical_conditions=medical_conditions,
            dependents=dependents,
            family_commitments=family_commitments,
            obligations=obligations,
            vault_protected=True,
            sensitivity_tier=SensitivityTier.TIER_HEALTH_FAMILY.value,
        )
        db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile
