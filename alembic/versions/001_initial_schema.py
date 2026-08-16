"""Initial schema: hurdle, ledger_entry, sensitive_profile + append-only trigger.

Revision ID: 001
Revises:
Create Date: 2026-06-27

The ledger_entry table is append-only by design. Mutations are blocked at two layers:
  1. SQLAlchemy ORM guard (before_flush) — Python-level, see app/models/ledger.py.
  2. Postgres BEFORE UPDATE OR DELETE trigger defined here — blocks raw-SQL mutations
     that bypass the ORM (psql shell, direct DB access, migration scripts).

Corrections must be new compensating LedgerEntries. The ledger is the source of truth
and must remain immutable once written.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hurdle",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("raw_description", sa.Text, nullable=False),
        sa.Column("primitives", sa.JSON, nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "ledger_entry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("effect", sa.JSON, nullable=False, server_default="[]"),
        sa.Column(
            "linked_hurdle_id",
            sa.String(36),
            sa.ForeignKey("hurdle.id"),
            nullable=True,
        ),
        sa.Column("evidence", sa.JSON, nullable=True),
    )

    op.create_table(
        "sensitive_profile",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("medical_conditions", sa.JSON, nullable=True),
        sa.Column("dependents", sa.JSON, nullable=True),
        sa.Column("family_commitments", sa.JSON, nullable=True),
        sa.Column("obligations", sa.JSON, nullable=True),
        sa.UniqueConstraint("user_id", name="uq_sensitive_profile_user"),
    )

    # Append-only enforcement at the Postgres layer.
    # This trigger fires BEFORE any UPDATE or DELETE on ledger_entry and raises
    # an exception, making it impossible to mutate rows even via raw SQL.
    # TRUNCATE is intentionally excluded: test teardown uses TRUNCATE to clean
    # tables (row-level triggers do not fire for TRUNCATE in Postgres).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_ledger_append_only()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'ledger_entry is append-only: % is not permitted. '
                'All corrections must be new compensating entries.',
                TG_OP;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_entry_append_only
        BEFORE UPDATE OR DELETE ON ledger_entry
        FOR EACH ROW
        EXECUTE FUNCTION enforce_ledger_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entry_append_only ON ledger_entry;")
    op.execute("DROP FUNCTION IF EXISTS enforce_ledger_append_only();")
    op.drop_table("sensitive_profile")
    op.drop_table("ledger_entry")
    op.drop_table("hurdle")
