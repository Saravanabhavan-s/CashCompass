from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
