import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.append_only import append_only


@append_only
class Recommendation(Base):
    """Immutable result of one recommendation run. A new run is a new row; the
    user's decision lives in recommendation_decision, never as an edit here."""

    __tablename__ = "recommendation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    hurdle_id: Mapped[str] = mapped_column(String(36), ForeignKey("hurdle.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # RECOMMENDED | INFEASIBLE | BLOCKED
    snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("evidence_snapshot.id"), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(Text, nullable=False)
    debate_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("debate_run.id"), nullable=True)
    chosen_plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    evaluation: Mapped[dict] = mapped_column(JSONB, nullable=False)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=True), nullable=True)
    confidence_components: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence_band: Mapped[str] = mapped_column(String(8), nullable=False)
    guardrails: Mapped[dict] = mapped_column(JSONB, nullable=False)
    user_figures: Mapped[dict] = mapped_column(JSONB, nullable=False)  # exact figures, user's own view only
    disclaimer: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), nullable=False
    )

    __table_args__ = (Index("ix_recommendation_hurdle_created", "hurdle_id", "created_at"),)


@append_only
class RecommendationDecision(Base):
    __tablename__ = "recommendation_decision"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recommendation_id: Mapped[str] = mapped_column(String(36), ForeignKey("recommendation.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # CONFIRMED | REJECTED
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledge_warnings: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ledger_entry_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ledger_entry.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), nullable=False
    )

    __table_args__ = (UniqueConstraint("recommendation_id", name="uq_recommendation_decision_one_per_rec"),)
