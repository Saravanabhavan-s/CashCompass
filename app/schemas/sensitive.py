from pydantic import BaseModel

from app.enums import SensitivityTier


class Dependent(BaseModel):
    relation: str        # e.g. "child", "parent", "spouse"
    age: int | None = None
    notes: str | None = None


class SensitiveProfileUpsert(BaseModel):
    user_id: str
    medical_conditions: list[str] | None = None
    dependents: list[Dependent] | None = None
    family_commitments: list[str] | None = None
    obligations: list[str] | None = None


# NOT included in any default user/state serializer.
# Only returned by the dedicated /profile/sensitive endpoint.
class SensitiveProfileRead(BaseModel):
    id: str
    user_id: str
    sensitivity_tier: SensitivityTier = SensitivityTier.TIER_HEALTH_FAMILY
    medical_conditions: list[str] | None
    dependents: list[Dependent] | None
    family_commitments: list[str] | None
    obligations: list[str] | None
    # Layer 2 vault will flip this to True after tokenizing identity fields.
    vault_protected: bool = False
