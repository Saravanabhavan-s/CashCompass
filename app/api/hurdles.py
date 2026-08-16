from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.hurdle import HurdleCreate, HurdleRead
from app.services import hurdles as svc

router = APIRouter(prefix="/hurdles", tags=["hurdles"])


@router.post("", response_model=HurdleRead, status_code=201)
def create_hurdle(body: HurdleCreate, db: Session = Depends(get_db)):
    return svc.create_hurdle(db, body)


@router.get("/{hurdle_id}", response_model=HurdleRead)
def get_hurdle(hurdle_id: str, db: Session = Depends(get_db)):
    hurdle = svc.get_hurdle(db, hurdle_id)
    if not hurdle:
        raise HTTPException(status_code=404, detail="Hurdle not found")
    return hurdle
