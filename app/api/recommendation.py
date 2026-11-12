"""Recommendation routes. The LLM client comes from the `get_llm` dependency
(503 when none is configured; tests override it with ScriptedLLM). Confirm and
reject are the human-in-the-loop step: nothing reaches the ledger until the
user confirms."""
from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_llm
from app.debate.llm import LLMClient
from app.models.recommendation import Recommendation
from app.optimizer.assumptions import Assumptions
from app.schemas.recommendation import (
    ConfirmRequest,
    DecisionRead,
    RecommendationRead,
    RecommendationRequest,
    RejectRequest,
)
from app.services import recommendation as svc

router = APIRouter(tags=["recommendation"])


def _read(db: Session, rec: Recommendation) -> RecommendationRead:
    decision = svc.get_decision(db, rec.id)
    return RecommendationRead(
        id=rec.id,
        hurdle_id=rec.hurdle_id,
        outcome=rec.outcome,
        status=decision.decision if decision else "PENDING",
        snapshot_id=rec.snapshot_id,
        snapshot_hash=rec.snapshot_hash,
        debate_run_id=rec.debate_run_id,
        chosen_plan=rec.chosen_plan,
        plan_summary=svc.summarize_plan(rec.chosen_plan) if rec.chosen_plan else None,
        evaluation=rec.evaluation,
        citations=rec.citations,
        confidence=rec.confidence,
        confidence_band=rec.confidence_band,
        confidence_components=rec.confidence_components,
        guardrails=rec.guardrails,
        user_figures=rec.user_figures,
        disclaimer=rec.disclaimer,
        created_at=rec.created_at,
        decision=DecisionRead.model_validate(decision) if decision else None,
    )


@router.post("/hurdles/{hurdle_id}/recommendation", response_model=RecommendationRead, status_code=201)
def create_recommendation(
    hurdle_id: str,
    body: RecommendationRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    llm: LLMClient = Depends(get_llm),
):
    body = body or RecommendationRequest()
    assumptions = Assumptions().with_overrides(**body.assumptions) if body.assumptions else None
    rec = svc.generate_recommendation(
        db,
        hurdle_id,
        llm,
        assumptions=assumptions,
        monthly_living_expense=body.monthly_living_expense,
        as_of=body.as_of,
        user_id=body.user_id,
        force_refresh_evidence=body.force_refresh_evidence,
    )
    return _read(db, rec)


@router.get("/recommendations/{recommendation_id}", response_model=RecommendationRead)
def get_recommendation(recommendation_id: str, db: Session = Depends(get_db)):
    return _read(db, svc.get_recommendation(db, recommendation_id))


@router.post("/recommendations/{recommendation_id}/confirm", response_model=RecommendationRead)
def confirm(recommendation_id: str, body: ConfirmRequest, db: Session = Depends(get_db)):
    rec, _ = svc.confirm_recommendation(db, recommendation_id, body.confirmed_by, body.acknowledge_warnings)
    return _read(db, rec)


@router.post("/recommendations/{recommendation_id}/reject", response_model=RecommendationRead)
def reject(recommendation_id: str, body: RejectRequest, db: Session = Depends(get_db)):
    rec, _ = svc.reject_recommendation(db, recommendation_id, body.rejected_by, body.reason)
    return _read(db, rec)
