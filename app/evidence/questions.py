"""Primitive-driven evidence questions.

Consistent with Layer 1's "no event enumeration" principle: what gets
researched is derived from which PRIMITIVES are present on the hurdle, never
from a hurdle "type". Questions are derived from primitive TYPES (plus the
sign of INFLOW_CHANGE.delta) only -- never amounts, dates, labels or free-text
reasons -- so no user data can reach an outbound source query.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.enums import PrimitiveType


class EvidenceTopic(str, Enum):
    SAVINGS_INSTRUMENTS = "SAVINGS_INSTRUMENTS"
    EMERGENCY_FUND = "EMERGENCY_FUND"
    COST_OF_LIVING = "COST_OF_LIVING"
    INCOME_SMOOTHING = "INCOME_SMOOTHING"
    INVESTMENT_CAPACITY = "INVESTMENT_CAPACITY"
    OPPORTUNITY_COST_OF_DELAY = "OPPORTUNITY_COST_OF_DELAY"


class EvidenceQuestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    topic: EvidenceTopic
    text: str


_QUESTION_TEXT = {
    EvidenceTopic.SAVINGS_INSTRUMENTS: "Which savings instruments (FD, RD, short-term liquid funds) suit saving toward a dated lump-sum goal?",
    EvidenceTopic.EMERGENCY_FUND: "How large should an emergency fund be, and what are the safety and withdrawal terms of liquid funds?",
    EvidenceTopic.COST_OF_LIVING: "How are living costs and inflation trending for a recurring expense?",
    EvidenceTopic.INCOME_SMOOTHING: "What unemployment support and income-smoothing instruments exist after an income drop?",
    EvidenceTopic.INVESTMENT_CAPACITY: "How should new income be allocated across investment options?",
    EvidenceTopic.OPPORTUNITY_COST_OF_DELAY: "What is the opportunity cost of shifting a goal's deadline?",
}


def _field(p: Any, name: str) -> Any:
    return p.get(name) if isinstance(p, dict) else getattr(p, name, None)


def _type_of(p: Any) -> PrimitiveType | None:
    raw = _field(p, "type")
    try:
        return PrimitiveType(raw)
    except ValueError:
        return None


def _sign(p: Any) -> int:
    try:
        delta = Decimal(str(_field(p, "delta")))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    return (delta > 0) - (delta < 0)


def derive_evidence_questions(primitives: list[Any]) -> list[EvidenceQuestion]:
    """Pure, no I/O. Unrecognised/malformed primitives are skipped, never
    raised on: an unseen combination degrades to an empty or minimal set."""
    types: set[PrimitiveType] = set()
    topics: set[EvidenceTopic] = set()
    for p in primitives or []:
        t = _type_of(p)
        if t is None:
            continue
        types.add(t)
        if t == PrimitiveType.INFLOW_CHANGE:
            s = _sign(p)
            if s < 0:
                topics.add(EvidenceTopic.INCOME_SMOOTHING)
            elif s > 0:
                topics.add(EvidenceTopic.INVESTMENT_CAPACITY)

    if {PrimitiveType.GOAL, PrimitiveType.ONE_TIME_OUTFLOW} <= types:
        topics.add(EvidenceTopic.SAVINGS_INSTRUMENTS)
    if PrimitiveType.RISK_CONSTRAINT in types:
        topics.add(EvidenceTopic.EMERGENCY_FUND)
    if PrimitiveType.RECURRING_OUTFLOW in types:
        topics.add(EvidenceTopic.COST_OF_LIVING)
    if PrimitiveType.HORIZON_SHIFT in types:
        topics.add(EvidenceTopic.OPPORTUNITY_COST_OF_DELAY)

    return [EvidenceQuestion(topic=t, text=_QUESTION_TEXT[t]) for t in sorted(topics, key=lambda t: t.value)]
