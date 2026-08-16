"""
SensitiveProfile stores non-financial personal context that affects financial
planning.

Governed by India's Digital Personal Data Protection (DPDP) Act, 2023, which
classifies health data and family information as sensitive personal data
requiring explicit consent, purpose limitation, and stricter access controls
than ordinary financial PII.

This model is assigned SENSITIVITY_TIER = TIER_HEALTH_FAMILY and is fully
vault-backed (Layer 2):
  - Stored in a separate table, never joined into default user/state queries.
  - Excluded from all default serializers; only the dedicated /profile/sensitive
    endpoint may expose it via an explicit schema.
  - medical_conditions/family_commitments/obligations hold vault tokens, not
    plaintext. dependents[].notes_token is likewise a token; relation/age stay
    cleartext (see app/services/sensitive.py for why that's safe).
  - Enforced at three layers: Pydantic validation (VaultToken), SQLAlchemy
    flush-time (VaultProtectedMixin, guarded by vault_protected), and a
    Postgres trigger (enforce_vault_tokens_only, migration 002) that rejects
    cleartext even from raw SQL.
"""
import uuid

from sqlalchemy import Boolean, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import SensitivityTier
from app.vault.protected import VaultProtectedMixin


class SensitiveProfile(VaultProtectedMixin, Base):
    __tablename__ = "sensitive_profile"
    __table_args__ = (UniqueConstraint("user_id", name="uq_sensitive_profile_user"),)

    __vault_protected_columns__ = ("medical_conditions", "family_commitments", "obligations", "dependents")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    medical_conditions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    dependents: Mapped[list | None] = mapped_column(JSON, nullable=True)
    family_commitments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    obligations: Mapped[list | None] = mapped_column(JSON, nullable=True)

    sensitivity_tier: Mapped[str] = mapped_column(
        String(50), nullable=False, default=SensitivityTier.TIER_HEALTH_FAMILY.value
    )
    vault_protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
