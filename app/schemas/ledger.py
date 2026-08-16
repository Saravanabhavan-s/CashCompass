from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from app.enums import LedgerEntryType, LedgerSource


class EffectDelta(BaseModel):
    """One atomic change to a field in FinancialState."""
    field: str       # e.g. "savings", "recurring_income", "stocks"
    op: str          # "set" | "add" | "append"
    value: Any
    account: str | None = None


class LedgerEntryCreate(BaseModel):
    effective_date: date
    type: LedgerEntryType
    source: LedgerSource
    description: str
    effect: list[EffectDelta]
    linked_hurdle_id: str | None = None


class LedgerEntryRead(BaseModel):
    id: str
    created_at: datetime
    effective_date: date
    type: LedgerEntryType
    source: LedgerSource
    description: str
    effect: list[EffectDelta]
    linked_hurdle_id: str | None
    evidence: dict | None

    model_config = {"from_attributes": True}


class LedgerPage(BaseModel):
    items: list[LedgerEntryRead]
    total: int
    limit: int
    offset: int
