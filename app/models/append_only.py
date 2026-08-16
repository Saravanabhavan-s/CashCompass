"""Registry-based append-only guard, shared by every append-only table
(LedgerEntry, DetokenizationAudit).

Why two layers, on every append-only table?
  1. This listener fires inside SQLAlchemy's flush pipeline, giving a clean
     Python error with a meaningful message before any SQL reaches the DB.
  2. A Postgres BEFORE UPDATE/DELETE trigger (see the migrations) is the
     second line: it blocks raw-SQL mutations that bypass the ORM entirely
     (psql shell, migration scripts, direct DB access).

Corrections are always new compensating rows, never edits.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.exceptions import AppendOnlyViolation

APPEND_ONLY_MODELS: set[type] = set()


def append_only(cls: type) -> type:
    APPEND_ONLY_MODELS.add(cls)
    return cls


@event.listens_for(Session, "before_flush")
def _enforce_append_only(session: Session, flush_context, instances):
    for obj in session.dirty:
        if type(obj) in APPEND_ONLY_MODELS:
            raise AppendOnlyViolation(
                f"{obj.__tablename__} is append-only. Corrections must be new compensating "
                f"rows, not edits. Attempted UPDATE on id={getattr(obj, 'id', None)!r}."
            )
    for obj in session.deleted:
        if type(obj) in APPEND_ONLY_MODELS:
            raise AppendOnlyViolation(
                f"{obj.__tablename__} is append-only. Rows may never be deleted. "
                f"Attempted DELETE on id={getattr(obj, 'id', None)!r}."
            )
