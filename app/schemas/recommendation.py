from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class RecommendationRequest(BaseModel):
    monthly_living_expense: Decimal | None = Field(default=None, ge=0)
    as_of: date | None = None
    user_id: str | None = None
    force_refresh_evidence: bool = False
    # optional overrides of the illustrative annual rates / stress parameters
    assumptions: dict[str, Decimal] | None = None


class ConfirmRequest(BaseModel):
    confirmed_by: str = Field(min_length=1, max_length=128)
    acknowledge_warnings: bool = False


class RejectRequest(BaseModel):
    rejected_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1)


class DecisionRead(BaseModel):
    decision: str
    decided_by: str
    reason: str | None
    acknowledge_warnings: bool
    ledger_entry_id: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RecommendationRead(BaseModel):
    id: str
    hurdle_id: str
    outcome: str  # RECOMMENDED | INFEASIBLE | BLOCKED
    status: str  # PENDING | CONFIRMED | REJECTED (derived from decision rows)
    snapshot_id: str
    snapshot_hash: str
    debate_run_id: str | None
    chosen_plan: dict | None
    plan_summary: str | None
    evaluation: dict
    citations: list[dict]
    confidence: Decimal | None
    confidence_band: str
    confidence_components: dict
    guardrails: dict
    user_figures: dict
    disclaimer: str
    created_at: datetime
    decision: DecisionRead | None = None
