import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.append_only import append_only


@append_only
class EvidenceSnapshot(Base):
    """Frozen, hashed evidence set for one hurdle. Append-only like the ledger:
    a re-debate on updated evidence is a NEW snapshot, never a mutation of an
    old one, so any earlier debate can be replayed against exactly what it saw."""

    __tablename__ = "evidence_snapshot"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    hurdle_id: Mapped[str] = mapped_column(String(36), ForeignKey("hurdle.id"), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(Text, nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_versions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # clock_timestamp(), not now(): now() is constant within a transaction, which
    # would make "latest snapshot" ordering ambiguous.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), nullable=False
    )

    items: Mapped[list["EvidenceItem"]] = relationship(
        back_populates="snapshot", order_by="EvidenceItem.source, EvidenceItem.query, EvidenceItem.id"
    )

    __table_args__ = (Index("ix_evidence_snapshot_hurdle_created", "hurdle_id", "created_at"),)


@append_only
class EvidenceItem(Base):
    __tablename__ = "evidence_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("evidence_snapshot.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # ALPHA_VANTAGE | FRED | LOCAL_REGULATORY
    query: Mapped[str] = mapped_column(Text, nullable=False)
    lens_tags: Mapped[list] = mapped_column(JSONB, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citation: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    snapshot: Mapped[EvidenceSnapshot] = relationship(back_populates="items")

    __table_args__ = (Index("ix_evidence_item_snapshot", "snapshot_id"),)
