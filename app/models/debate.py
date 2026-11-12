import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.append_only import append_only


@append_only
class DebateRun(Base):
    """One completed debate over one frozen evidence snapshot. Append-only:
    a re-debate is a NEW run, never an edit."""

    __tablename__ = "debate_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    hurdle_id: Mapped[str] = mapped_column(String(36), ForeignKey("hurdle.id"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("evidence_snapshot.id"), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    llm_name: Mapped[str] = mapped_column(String(128), nullable=False)
    final_leader_group: Mapped[str] = mapped_column(String(64), nullable=False)
    final_consensus: Mapped[dict] = mapped_column(JSONB, nullable=False)
    leaders: Mapped[list] = mapped_column(JSONB, nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), nullable=False
    )

    turns: Mapped[list["DebateTurn"]] = relationship(back_populates="run", order_by="DebateTurn.seq")

    __table_args__ = (Index("ix_debate_run_hurdle_created", "hurdle_id", "created_at"),)


@append_only
class DebateTurn(Base):
    __tablename__ = "debate_turn"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("debate_run.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # order within the run
    group_name: Mapped[str] = mapped_column(String(64), nullable=False)
    persona: Mapped[str] = mapped_column(String(32), nullable=False)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    repaired: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    run: Mapped[DebateRun] = relationship(back_populates="turns")

    __table_args__ = (Index("ix_debate_turn_run", "run_id", "seq"),)
