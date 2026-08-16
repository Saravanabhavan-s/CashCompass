import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.append_only import append_only


class VaultEntry(Base):
    """Deterministic blind-index row: the token IS the primary key AND the
    lookup index. Mutable by design — crypto_shred() and rotate_kek() both
    UPDATE this table. That asymmetry with the append-only ledger is
    deliberate: the ledger is immutable, the key material is not."""

    __tablename__ = "vault_entries"

    token_id: Mapped[str] = mapped_column(Text, primary_key=True)
    field_type: Mapped[str] = mapped_column(String(32), nullable=False)

    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # N1, 96-bit
    auth_tag: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # NULL == shredded
    dek_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # N2
    dek_auth_tag: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # key_version is FROZEN at row creation and bound into the data AAD — it
    # never changes. kek_version records which KEK currently wraps the DEK
    # and DOES change on rotation. Splitting these is what lets rotate_kek
    # leave ciphertext/nonce/auth_tag/key_version byte-unchanged (see
    # tests/test_vault_rotation.py::test_rotate_kek_leaves_data_columns_unchanged).
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kek_version: Mapped[int] = mapped_column(Integer, nullable=False)

    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    shredded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_vault_entries_tenant_field", "tenant_id", "field_type"),
        Index(
            "ix_vault_entries_kek_version",
            "kek_version",
            postgresql_where=text("shredded_at IS NULL"),
        ),
        CheckConstraint("octet_length(nonce) = 12", name="ck_vault_nonce_len"),
        CheckConstraint("octet_length(auth_tag) = 16", name="ck_vault_tag_len"),
        CheckConstraint(
            "token_id ~ '^TKN_[A-Z][A-Z0-9_]*_[0-9a-f]{16}$'", name="ck_vault_token_format"
        ),
        CheckConstraint(
            "(wrapped_dek IS NULL) = (shredded_at IS NOT NULL)", name="ck_vault_shred_consistency"
        ),
    )


@append_only
class DetokenizationAudit(Base):
    """The token<->value map is the system's highest-value attack target.
    Every reverse-map call is logged here, no exceptions — including denied
    attempts. No ForeignKey to vault_entries.token_id: the audit trail must
    be able to reference a token whose row may later be hard-deleted, and an
    FK is a lever (a misconfigured ON DELETE CASCADE) an attacker or a
    careless DBA could pull to erase evidence. It references by value, not
    by constraint."""

    __tablename__ = "detokenization_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    token_id: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    caller: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    denial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_detok_audit_token_id", "token_id"),
        Index("ix_detok_audit_requested_at", "requested_at"),
    )
