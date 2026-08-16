"""
SensitiveProfile stores non-financial personal context that affects financial planning.

Governed by India's Digital Personal Data Protection (DPDP) Act, 2023, which classifies
health data and family information as sensitive personal data requiring explicit consent,
purpose limitation, and stricter access controls than ordinary financial PII.

This model is assigned SENSITIVITY_TIER = TIER_HEALTH_FAMILY. It is:
  - Stored in a separate table, never joined into default user/state queries.
  - Excluded from all default serializers; only the dedicated /profile/sensitive
    endpoint may expose it via an explicit schema.
  - Marked for Layer 2 vault integration: identity fields (dependent names, medical
    identifiers) will be tokenized and access will be gated behind explicit consent
    checks. Until then, the tier flag and structural separation are enforced here.
"""
import uuid

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# SENSITIVITY_TIER: TIER_HEALTH_FAMILY
# Stricter than TIER_FINANCIAL_PII. Not exposed in any default serializer.


class SensitiveProfile(Base):
    __tablename__ = "sensitive_profile"
    __table_args__ = (UniqueConstraint("user_id", name="uq_sensitive_profile_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    medical_conditions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    dependents: Mapped[list | None] = mapped_column(JSON, nullable=True)
    family_commitments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    obligations: Mapped[list | None] = mapped_column(JSON, nullable=True)
