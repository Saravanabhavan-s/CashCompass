import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.append_only import append_only


@append_only
class LedgerEntry(Base):
    __tablename__ = "ledger_entry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Stored as VARCHAR; Pydantic enforces LedgerEntryType / LedgerSource enum values.
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # List of EffectDelta dicts: [{field, op, value, account?}]
    effect: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    linked_hurdle_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("hurdle.id"), nullable=True
    )
    # Reserved for agent layer (citations, source documents). Unused in Layer 1.
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# Append-only enforcement (ORM layer + Postgres trigger) lives in
# app/models/append_only.py, shared with DetokenizationAudit (Layer 2).
