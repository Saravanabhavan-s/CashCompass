"""
Append-only ledger invariant tests.

Tests both enforcement layers:
  1. ORM guard (before_flush) — fires when SQLAlchemy attempts to flush a
     modified or deleted LedgerEntry.
  2. Postgres trigger (trg_ledger_entry_append_only) — fires on raw SQL UPDATE
     or DELETE, bypassing the ORM entirely.
"""
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.exceptions import AppendOnlyViolation
from app.models.ledger import LedgerEntry


def _create_entry(session: Session) -> LedgerEntry:
    entry = LedgerEntry(
        effective_date=date.today(),
        type="DECISION",
        source="USER_STATED",
        description="test entry",
        effect=[],
    )
    session.add(entry)
    session.commit()
    return entry


def test_orm_update_raises(db_session):
    """Modifying a committed LedgerEntry and flushing raises AppendOnlyViolation."""
    entry = _create_entry(db_session)
    entry.description = "tampered"
    with pytest.raises(AppendOnlyViolation, match="append-only"):
        db_session.flush()
    db_session.rollback()


def test_orm_delete_raises(db_session):
    """Marking a committed LedgerEntry for deletion and flushing raises AppendOnlyViolation."""
    entry = _create_entry(db_session)
    db_session.delete(entry)
    with pytest.raises(AppendOnlyViolation, match="append-only"):
        db_session.flush()
    db_session.rollback()


def test_db_trigger_update_raises(db_session, db_engine):
    """Raw SQL UPDATE on ledger_entry is blocked by the Postgres trigger."""
    entry = _create_entry(db_session)
    with pytest.raises(Exception):
        with db_engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE ledger_entry SET description = 'hacked' "
                    "WHERE id = :id"
                ),
                {"id": entry.id},
            )
            conn.commit()


def test_db_trigger_delete_raises(db_session, db_engine):
    """Raw SQL DELETE on ledger_entry is blocked by the Postgres trigger."""
    entry = _create_entry(db_session)
    with pytest.raises(Exception):
        with db_engine.connect() as conn:
            conn.execute(
                text("DELETE FROM ledger_entry WHERE id = :id"),
                {"id": entry.id},
            )
            conn.commit()


def test_new_entry_allowed(db_session):
    """Inserting a new LedgerEntry is always allowed (guard only blocks updates/deletes)."""
    e1 = _create_entry(db_session)
    e2 = _create_entry(db_session)
    assert e1.id != e2.id


def test_compensating_entry_pattern(db_session):
    """Corrections via a new compensating entry work; direct mutation does not."""
    # Original decision: set savings to ₹5L
    original = LedgerEntry(
        effective_date=date.today(),
        type="STATE_SNAPSHOT",
        source="USER_STATED",
        description="Initial savings",
        effect=[{"field": "savings", "op": "set", "value": 500000.0}],
    )
    db_session.add(original)
    db_session.commit()

    # Correction: add a compensating entry instead of editing the original
    correction = LedgerEntry(
        effective_date=date.today(),
        type="DECISION",
        source="USER_STATED",
        description="Correction: actual savings are ₹8L",
        effect=[{"field": "savings", "op": "add", "value": 300000.0}],
    )
    db_session.add(correction)
    db_session.commit()  # Must not raise

    assert original.description == "Initial savings"  # original unchanged
