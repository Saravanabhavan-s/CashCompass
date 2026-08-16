from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.exceptions import AppendOnlyViolation
from app.schemas.ledger import LedgerEntryCreate, LedgerEntryRead, LedgerPage
from app.services import ledger as svc

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.post("", response_model=LedgerEntryRead, status_code=201)
def append_entry(body: LedgerEntryCreate, db: Session = Depends(get_db)):
    # AppendOnlyViolation can't be triggered by a POST (new entry), but
    # surface it as 409 if somehow raised (defensive).
    try:
        return svc.append_entry(db, body)
    except AppendOnlyViolation as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("", response_model=LedgerPage)
def list_entries(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    items, total = svc.list_entries(db, limit=limit, offset=offset)
    return LedgerPage(items=items, total=total, limit=limit, offset=offset)
