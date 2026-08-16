from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from app.enums import Cadence, PrimitiveType


class OneTimeOutflow(BaseModel):
    type: Literal[PrimitiveType.ONE_TIME_OUTFLOW] = PrimitiveType.ONE_TIME_OUTFLOW
    amount: Decimal
    due_date: date


class RecurringOutflow(BaseModel):
    type: Literal[PrimitiveType.RECURRING_OUTFLOW] = PrimitiveType.RECURRING_OUTFLOW
    amount: Decimal
    cadence: Cadence
    start: date
    end: date | None = None


class OneTimeInflow(BaseModel):
    type: Literal[PrimitiveType.ONE_TIME_INFLOW] = PrimitiveType.ONE_TIME_INFLOW
    amount: Decimal
    date: date


class InflowChange(BaseModel):
    type: Literal[PrimitiveType.INFLOW_CHANGE] = PrimitiveType.INFLOW_CHANGE
    # Positive = raise/new income; negative = job loss or reduction.
    delta: Decimal
    cadence: Cadence
    start: date


class HorizonShift(BaseModel):
    type: Literal[PrimitiveType.HORIZON_SHIFT] = PrimitiveType.HORIZON_SHIFT
    goal_ref: str  # label or id of the goal whose deadline changes
    new_date: date


class RiskConstraint(BaseModel):
    type: Literal[PrimitiveType.RISK_CONSTRAINT] = PrimitiveType.RISK_CONSTRAINT
    min_liquidity: Decimal
    reason: str


class Goal(BaseModel):
    type: Literal[PrimitiveType.GOAL] = PrimitiveType.GOAL
    target_amount: Decimal
    deadline: date
    priority: int = Field(ge=1, le=10)


# Discriminated union on the `type` field.
# Any life event decomposes into a list of these — no event-specific types needed.
Primitive = Annotated[
    Union[
        OneTimeOutflow,
        RecurringOutflow,
        OneTimeInflow,
        InflowChange,
        HorizonShift,
        RiskConstraint,
        Goal,
    ],
    Field(discriminator="type"),
]
