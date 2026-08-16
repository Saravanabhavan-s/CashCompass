from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import VaultFieldType
from app.models.ledger import LedgerEntry
from app.schemas.ledger import LedgerEntryCreate


def append_entry(db: Session, data: LedgerEntryCreate) -> LedgerEntry:
    entry = LedgerEntry(
        effective_date=data.effective_date,
        type=data.type.value,
        source=data.source.value,
        description=data.description,
        effect=[e.model_dump() for e in data.effect],
        linked_hurdle_id=data.linked_hurdle_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def append_entry_tokenized(db: Session, data: LedgerEntryCreate, tenant_id: str | None = None) -> LedgerEntry:
    """Additive Layer 2 variant of append_entry(): tokenizes the free-text
    `description` field before it enters the immutable ledger.

    append_entry() itself is left untouched — existing callers keep writing
    description in cleartext, and description is DEFAULT_DENY classified
    (app/vault/classification.py), so even an untokenized description is
    dropped before reaching any agent destination. Tokenizing at write time
    closes the residual risk that a raw DB dump (not just an LLM prompt)
    exposes it.

    Note this can only be applied going forward: the ledger's append-only
    invariant means a description written via append_entry() before this
    existed cannot be retroactively tokenized or shredded — see the
    README's "Crypto-shredding" section.
    """
    from app.services import vault as vault_svc

    description = data.description
    if description.strip():
        description = vault_svc.tokenize_field(db, VaultFieldType.NOTE, description, tenant_id)
    tokenized_data = data.model_copy(update={"description": description})
    return append_entry(db, tokenized_data)


def list_entries(
    db: Session, limit: int = 50, offset: int = 0
) -> tuple[list[LedgerEntry], int]:
    total = db.scalar(select(func.count()).select_from(LedgerEntry)) or 0
    entries = list(
        db.scalars(
            select(LedgerEntry)
            .order_by(LedgerEntry.effective_date, LedgerEntry.created_at)
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return entries, total
