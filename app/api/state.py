from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.state import FinancialState
from app.services import state as svc

router = APIRouter(prefix="/state", tags=["state"])


@router.get("", response_model=FinancialState)
def get_state(db: Session = Depends(get_db)):
    return svc.rebuild_state_from_ledger(db)
