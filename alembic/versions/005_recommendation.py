"""Layer 3e: recommendation and recommendation_decision.

Revision ID: 005
Revises: 004
Create Date: 2026-11-12

Both tables are append-only (ORM guard + enforce_append_only() trigger from
migration 002). The decision table has a UNIQUE recommendation_id: one
decision per recommendation, enforced by the database as well as the service.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recommendation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hurdle_id", sa.String(36), sa.ForeignKey("hurdle.id"), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("evidence_snapshot.id"), nullable=False),
        sa.Column("snapshot_hash", sa.Text, nullable=False),
        sa.Column("debate_run_id", sa.String(36), sa.ForeignKey("debate_run.id"), nullable=True),
        sa.Column("chosen_plan", JSONB, nullable=True),
        sa.Column("evaluation", JSONB, nullable=False),
        sa.Column("citations", JSONB, nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("confidence_components", JSONB, nullable=False),
        sa.Column("confidence_band", sa.String(8), nullable=False),
        sa.Column("guardrails", JSONB, nullable=False),
        sa.Column("user_figures", JSONB, nullable=False),
        sa.Column("disclaimer", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
    )
    op.create_index("ix_recommendation_hurdle_created", "recommendation", ["hurdle_id", "created_at"])

    op.create_table(
        "recommendation_decision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recommendation_id", sa.String(36), sa.ForeignKey("recommendation.id"), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("acknowledge_warnings", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("ledger_entry_id", sa.String(36), sa.ForeignKey("ledger_entry.id"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.UniqueConstraint("recommendation_id", name="uq_recommendation_decision_one_per_rec"),
    )

    for table in ("recommendation", "recommendation_decision"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION enforce_append_only();
            """
        )


def downgrade() -> None:
    for table in ("recommendation_decision", "recommendation"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
    op.drop_table("recommendation_decision")
    op.drop_index("ix_recommendation_hurdle_created", table_name="recommendation")
    op.drop_table("recommendation")
