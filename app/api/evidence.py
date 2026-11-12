"""Evidence snapshot routes (debug/inspection; the debate engine calls the
service directly).

Contrast with Layer 2's vault routes (app/api/vault.py), which are gated
behind an admin key: nothing here is gated, because this is public
market/regulatory data. It never touches EgressPolicy because it was never
the user's data to begin with. The user's own financial state is generalized
separately via Layer 2's generalize_for_debate().
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.evidence import EvidenceSnapshotRead
from app.services import evidence as svc

router = APIRouter(tags=["evidence"])


@router.post("/hurdles/{hurdle_id}/evidence-snapshot", response_model=EvidenceSnapshotRead)
def build_snapshot(
    hurdle_id: str,
    response: Response,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
):
    before = svc.get_latest_snapshot(db, hurdle_id)
    snap = svc.run_evidence_pipeline(db, hurdle_id, force_refresh=force_refresh)
    response.status_code = 200 if before is not None and before.id == snap.id else 201
    return snap


@router.get("/hurdles/{hurdle_id}/evidence-snapshot", response_model=EvidenceSnapshotRead)
def latest_snapshot(hurdle_id: str, db: Session = Depends(get_db)):
    snap = svc.get_latest_snapshot(db, hurdle_id)
    if snap is None:
        raise HTTPException(404, "No evidence snapshot for this hurdle.")
    return snap


@router.get("/evidence-snapshot/{snapshot_id}", response_model=EvidenceSnapshotRead)
def snapshot_by_id(snapshot_id: str, db: Session = Depends(get_db)):
    snap = svc.get_snapshot(db, snapshot_id)
    if snap is None:
        raise HTTPException(404, "Evidence snapshot not found.")
    return snap
