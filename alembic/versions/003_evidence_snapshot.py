"""Layer 3a: evidence snapshot service.

Revision ID: 003
Revises: 002
Create Date: 2026-10-21

Adds evidence_snapshot and evidence_item, both append-only. Reuses the
generic enforce_append_only() trigger function created in migration 002 --
the same two-layer discipline (ORM before_flush guard + DB trigger) as
ledger_entry and detokenization_audit. TIMESTAMPTZ, per Layer 2's convention.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_snapshot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hurdle_id", sa.String(36), sa.ForeignKey("hurdle.id"), nullable=False),
        sa.Column("snapshot_hash", sa.Text, nullable=False),
        sa.Column("question_count", sa.Integer, nullable=False),
        sa.Column("item_count", sa.Integer, nullable=False),
        sa.Column("source_versions", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
    )
    op.create_index("ix_evidence_snapshot_hurdle_created", "evidence_snapshot", ["hurdle_id", "created_at"])

    op.create_table(
        "evidence_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("evidence_snapshot.id"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("query", sa.Text, nullable=False),
        sa.Column("lens_tags", JSONB, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("citation", sa.Text, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_item_snapshot", "evidence_item", ["snapshot_id"])

    for table in ("evidence_snapshot", "evidence_item"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION enforce_append_only();
            """
        )


def downgrade() -> None:
    for table in ("evidence_item", "evidence_snapshot"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
    op.drop_index("ix_evidence_item_snapshot", table_name="evidence_item")
    op.drop_table("evidence_item")
    op.drop_index("ix_evidence_snapshot_hurdle_created", table_name="evidence_snapshot")
    op.drop_table("evidence_snapshot")
