from pydantic import BaseModel

from app.enums import SensitivityTier
from app.vault.protected import VaultProtectedModel, VaultToken


class Dependent(BaseModel):
    relation: str        # e.g. "child", "parent", "spouse"
    age: int | None = None
    notes: str | None = None


class DependentTokenized(BaseModel):
    # relation/age stay cleartext deliberately — low-cardinality quasi-
    # identifiers whose real protection is that the whole `dependents` list
    # is TIER_HEALTH_FAMILY and therefore never egresses to any agent
    # (see app/vault/egress.py). Tokenizing "child" would not protect it —
    # anyone with the HSK can dictionary-attack a value that low-entropy.
    relation: str
    age: int | None = None
    notes_token: VaultToken | None = None


class SensitiveProfileUpsert(BaseModel):
    # Accepts PLAINTEXT — tokenizing is the server's job
    # (app/services/sensitive.py), and the client is the plaintext's origin.
    # Do not retype this to VaultToken.
    user_id: str
    medical_conditions: list[str] | None = None
    dependents: list[Dependent] | None = None
    family_commitments: list[str] | None = None
    obligations: list[str] | None = None


# NOT included in any default user/state serializer.
# Only returned by the dedicated /profile/sensitive endpoint.
class SensitiveProfileRead(VaultProtectedModel):
    __vault_protected_fields__ = frozenset(
        {"medical_conditions", "family_commitments", "obligations", "dependents"}
    )

    model_config = {"from_attributes": True}

    id: str
    user_id: str
    sensitivity_tier: SensitivityTier
    medical_conditions: list[VaultToken] | None
    dependents: list[DependentTokenized] | None
    family_commitments: list[VaultToken] | None
    obligations: list[VaultToken] | None
    vault_protected: bool


class SensitiveProfileRevealed(BaseModel):
    """Plaintext projection, returned ONLY by POST /profile/sensitive/reveal
    (the USER_FACING egress destination) — always audited, one row per
    detokenized token."""

    id: str
    user_id: str
    medical_conditions: list[str] | None
    dependents: list[Dependent] | None
    family_commitments: list[str] | None
    obligations: list[str] | None
