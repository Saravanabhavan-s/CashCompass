"""Layer 3b: debate_run and debate_turn.

Revision ID: 004
Revises: 003
Create Date: 2026-11-12

Both tables are append-only: ORM before_flush guard + the generic
enforce_append_only() trigger function created in migration 002.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "debate_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hurdle_id", sa.String(36), sa.ForeignKey("hurdle.id"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("evidence_snapshot.id"), nullable=False),
        sa.Column("snapshot_hash", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("llm_name", sa.String(128), nullable=False),
        sa.Column("final_leader_group", sa.String(64), nullable=False),
        sa.Column("final_consensus", JSONB, nullable=False),
        sa.Column("leaders", JSONB, nullable=False),
        sa.Column("degraded", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
    )
    op.create_index("ix_debate_run_hurdle_created", "debate_run", ["hurdle_id", "created_at"])

    op.create_table(
        "debate_turn",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("debate_run.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("group_name", sa.String(64), nullable=False),
        sa.Column("persona", sa.String(32), nullable=False),
        sa.Column("round_no", sa.Integer, nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("raw", sa.Text, nullable=False, server_default=""),
        sa.Column("output", JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("repaired", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_debate_turn_run", "debate_turn", ["run_id", "seq"])

    for table in ("debate_run", "debate_turn"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION enforce_append_only();
            """
        )


def downgrade() -> None:
    for table in ("debate_turn", "debate_run"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
    op.drop_index("ix_debate_turn_run", table_name="debate_turn")
    op.drop_table("debate_turn")
    op.drop_index("ix_debate_run_hurdle_created", table_name="debate_run")
    op.drop_table("debate_run")
