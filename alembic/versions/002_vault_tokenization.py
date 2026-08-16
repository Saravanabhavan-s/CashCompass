"""Layer 2: PII tokenization vault.

Revision ID: 002
Revises: 001
Create Date: 2026-08-16

Adds:
  - vault_entries: the deterministic blind-index store (envelope-encrypted
    ciphertext + wrapped DEK per row). Mutable by design — crypto_shred()
    and rotate_kek() both UPDATE it, unlike the append-only ledger.
  - detokenization_audit: append-only log of every reverse-map call,
    granted or denied. Same discipline as ledger_entry: a generic trigger
    function (not the ledger's — its message hardcodes 'ledger_entry') plus
    the ORM-level guard registered in app/models/append_only.py.
  - sensitive_profile.sensitivity_tier / .vault_protected: real columns
    backing what were previously hardcoded schema defaults
    (app/schemas/sensitive.py), plus a Postgres trigger that rejects
    cleartext in its JSON columns once vault_protected is true — the DB
    layer of VaultProtected (app/vault/protected.py).

Note on timestamps: Layer 1 used naive DateTime (ledger_entry.created_at,
hurdle.created_at). These new tables use TIMESTAMPTZ, because an audit trail
with an ambiguous timezone is not an audit trail. Layer 1's columns are
deliberately NOT retrofitted here — that's a separate, riskier change with
no Layer 2 payoff. TODO(003): consider migrating ledger_entry/hurdle to
TIMESTAMPTZ. Until then, never compare an aware detokenization_audit
timestamp to a naive ledger_entry one without normalizing first.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vault_entries",
        sa.Column("token_id", sa.Text, primary_key=True),
        sa.Column("field_type", sa.String(32), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("auth_tag", sa.LargeBinary, nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary, nullable=True),
        sa.Column("dek_nonce", sa.LargeBinary, nullable=True),
        sa.Column("dek_auth_tag", sa.LargeBinary, nullable=True),
        sa.Column("key_version", sa.Integer, nullable=False),
        sa.Column("kek_version", sa.Integer, nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("shredded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("octet_length(nonce) = 12", name="ck_vault_nonce_len"),
        sa.CheckConstraint("octet_length(auth_tag) = 16", name="ck_vault_tag_len"),
        sa.CheckConstraint(
            "token_id ~ '^TKN_[A-Z][A-Z0-9_]*_[0-9a-f]{16}$'", name="ck_vault_token_format"
        ),
        sa.CheckConstraint(
            "(wrapped_dek IS NULL) = (shredded_at IS NOT NULL)", name="ck_vault_shred_consistency"
        ),
    )
    op.create_index("ix_vault_entries_tenant_field", "vault_entries", ["tenant_id", "field_type"])
    op.create_index(
        "ix_vault_entries_kek_version",
        "vault_entries",
        ["kek_version"],
        postgresql_where=sa.text("shredded_at IS NULL"),
    )

    op.create_table(
        "detokenization_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("caller", sa.String(255), nullable=False),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("granted", sa.Boolean, nullable=False),
        sa.Column("denial_reason", sa.Text, nullable=True),
    )
    op.create_index("ix_detok_audit_token_id", "detokenization_audit", ["token_id"])
    op.create_index("ix_detok_audit_requested_at", "detokenization_audit", ["requested_at"])

    # Generic append-only trigger function — NOT a reuse of
    # enforce_ledger_append_only() (migration 001), which hardcodes the
    # string 'ledger_entry' in its RAISE and would be a confusing lie here.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_append_only()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not permitted.', TG_TABLE_NAME, TG_OP;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_detokenization_audit_append_only
        BEFORE UPDATE OR DELETE ON detokenization_audit
        FOR EACH ROW
        EXECUTE FUNCTION enforce_append_only();
        """
    )

    # sensitive_profile: flip from "flagged, not yet protected" to
    # vault-backed. Table is empty today; server_defaults make the NOT NULL
    # adds safe regardless and keep raw INSERTs working.
    op.add_column(
        "sensitive_profile",
        sa.Column(
            "sensitivity_tier",
            sa.String(50),
            nullable=False,
            server_default="TIER_HEALTH_FAMILY",
        ),
    )
    op.add_column(
        "sensitive_profile",
        sa.Column("vault_protected", sa.Boolean, nullable=False, server_default=sa.false()),
    )

    # DB layer of VaultProtected: once vault_protected is true, every element
    # of medical_conditions/family_commitments/obligations must be a vault
    # token, and every dependents[].*_token key must be one too. The
    # RAISE never echoes the offending value — only the column name — so a
    # cleartext leak never lands in the Postgres logs either.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_vault_tokens_only()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            col_name text;
            col_value jsonb;
            el jsonb;
            dep jsonb;
            k text;
            v jsonb;
        BEGIN
            IF NOT NEW.vault_protected THEN
                RETURN NEW;
            END IF;

            FOR col_name, col_value IN
                SELECT * FROM (VALUES
                    ('medical_conditions', NEW.medical_conditions::jsonb),
                    ('family_commitments', NEW.family_commitments::jsonb),
                    ('obligations', NEW.obligations::jsonb)
                ) AS cols(name, value)
            LOOP
                IF col_value IS NOT NULL AND col_value <> 'null'::jsonb THEN
                    FOR el IN SELECT jsonb_array_elements(col_value) LOOP
                        IF jsonb_typeof(el) <> 'string'
                           OR (el #>> '{}') !~ '^TKN_[A-Z][A-Z0-9_]*_[0-9a-f]{16}$' THEN
                            RAISE EXCEPTION
                                'sensitive_profile is vault-protected: cleartext not permitted in column %',
                                col_name;
                        END IF;
                    END LOOP;
                END IF;
            END LOOP;

            IF NEW.dependents IS NOT NULL AND NEW.dependents::jsonb <> 'null'::jsonb THEN
                FOR dep IN SELECT jsonb_array_elements(NEW.dependents::jsonb) LOOP
                    FOR k, v IN SELECT * FROM jsonb_each(dep) LOOP
                        IF k ~ '_token$' AND
                           (jsonb_typeof(v) <> 'string'
                            OR (v #>> '{}') !~ '^TKN_[A-Z][A-Z0-9_]*_[0-9a-f]{16}$') THEN
                            RAISE EXCEPTION
                                'sensitive_profile.dependents: key % must hold a vault token', k;
                        END IF;
                    END LOOP;
                END LOOP;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_sensitive_profile_tokens_only
        BEFORE INSERT OR UPDATE ON sensitive_profile
        FOR EACH ROW
        EXECUTE FUNCTION enforce_vault_tokens_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_sensitive_profile_tokens_only ON sensitive_profile;")
    op.execute("DROP FUNCTION IF EXISTS enforce_vault_tokens_only();")
    op.drop_column("sensitive_profile", "vault_protected")
    op.drop_column("sensitive_profile", "sensitivity_tier")

    op.execute("DROP TRIGGER IF EXISTS trg_detokenization_audit_append_only ON detokenization_audit;")
    op.execute("DROP FUNCTION IF EXISTS enforce_append_only();")

    op.drop_index("ix_detok_audit_requested_at", table_name="detokenization_audit")
    op.drop_index("ix_detok_audit_token_id", table_name="detokenization_audit")
    op.drop_table("detokenization_audit")

    op.drop_index("ix_vault_entries_kek_version", table_name="vault_entries")
    op.drop_index("ix_vault_entries_tenant_field", table_name="vault_entries")
    op.drop_table("vault_entries")
