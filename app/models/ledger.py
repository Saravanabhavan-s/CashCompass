import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, JSON, String, Text, event, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.exceptions import AppendOnlyViolation


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


# ORM-level append-only guard — first line of defense.
#
# Why two layers?
#   1. This listener fires inside SQLAlchemy's flush pipeline, giving a clean
#      Python error with a meaningful message before any SQL reaches the DB.
#   2. The Postgres BEFORE UPDATE/DELETE trigger (see migration 001) is the
#      second line: it blocks raw-SQL mutations that bypass the ORM entirely
#      (psql shell, migration scripts, direct DB access).
#
# Corrections are always new compensating LedgerEntries, never edits.
@event.listens_for(Session, "before_flush")
def _enforce_ledger_append_only(session: Session, flush_context, instances):
    for obj in session.dirty:
        if isinstance(obj, LedgerEntry):
            raise AppendOnlyViolation(
                "LedgerEntry is append-only. Corrections must be new compensating entries, "
                f"not edits. Attempted UPDATE on entry id={obj.id!r}."
            )
    for obj in session.deleted:
        if isinstance(obj, LedgerEntry):
            raise AppendOnlyViolation(
                "LedgerEntry is append-only. Records may never be deleted. "
                f"Attempted DELETE on entry id={obj.id!r}."
            )
