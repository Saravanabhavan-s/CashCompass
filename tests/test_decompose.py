"""
Hurdle + primitive decomposition tests.

Proves the core design claim: a life event (wedding) decomposes into universal
primitives WITHOUT a dedicated "wedding" primitive type. The same three primitives
handle any scale of wedding — or any other one-time high-cost life event.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import TypeAdapter

from app.enums import PrimitiveType
from app.schemas.hurdle import HurdleCreate
from app.schemas.primitives import (
    Goal,
    OneTimeOutflow,
    Primitive,
    RiskConstraint,
)
from app.services.hurdles import decompose


def test_wedding_uses_no_wedding_type():
    """No WEDDING primitive type exists — the enum only has universal primitives."""
    primitive_values = {pt.value for pt in PrimitiveType}
    assert "WEDDING" not in primitive_values
    assert "NEW_BABY" not in primitive_values
    assert "JOB_LOSS" not in primitive_values
    assert "SURGERY" not in primitive_values


def test_wedding_decomposes_to_three_primitives():
    """Wedding = ONE_TIME_OUTFLOW + GOAL + RISK_CONSTRAINT (no wedding type)."""
    wedding_date = date.today() + timedelta(days=240)
    hurdle = HurdleCreate(
        label="Wedding",
        raw_description="Wedding in ~8 months, ₹5L",
        primitives=[
            OneTimeOutflow(amount=Decimal("500000"), due_date=wedding_date),
            Goal(target_amount=Decimal("500000"), deadline=wedding_date, priority=1),
            RiskConstraint(min_liquidity=Decimal("100000"), reason="wedding buffer"),
        ],
    )
    types = {p.type for p in hurdle.primitives}
    assert PrimitiveType.ONE_TIME_OUTFLOW in types
    assert PrimitiveType.GOAL in types
    assert PrimitiveType.RISK_CONSTRAINT in types
    assert len(hurdle.primitives) == 3


def test_primitive_json_round_trip():
    """Primitives survive JSON serialization (DB storage) and deserialization."""
    wedding_date = date.today() + timedelta(days=240)
    original = [
        OneTimeOutflow(amount=Decimal("500000"), due_date=wedding_date),
        Goal(target_amount=Decimal("500000"), deadline=wedding_date, priority=1),
        RiskConstraint(min_liquidity=Decimal("100000"), reason="buffer"),
    ]

    # Simulate DB write: serialize to JSON-compatible dicts
    serialized = [p.model_dump(mode="json") for p in original]
    assert serialized[0]["type"] == "ONE_TIME_OUTFLOW"
    assert serialized[1]["type"] == "GOAL"
    assert serialized[2]["type"] == "RISK_CONSTRAINT"

    # Simulate DB read: deserialize via discriminated union
    ta = TypeAdapter(list[Primitive])
    parsed = ta.validate_python(serialized)
    assert parsed[0].type == PrimitiveType.ONE_TIME_OUTFLOW
    assert parsed[1].type == PrimitiveType.GOAL
    assert parsed[2].type == PrimitiveType.RISK_CONSTRAINT
    assert abs(float(parsed[0].amount) - 500000) < 1  # precision survived round-trip


def test_discriminated_union_rejects_unknown_type():
    """Unknown primitive type fails validation at the Pydantic layer."""
    ta = TypeAdapter(list[Primitive])
    with pytest.raises(Exception):
        ta.validate_python([{"type": "WEDDING", "amount": 500000}])


def test_decompose_stub_raises_not_implemented():
    """decompose() stub raises NotImplementedError — LLM layer fills this in Layer 2."""
    with pytest.raises(NotImplementedError):
        decompose("wedding in 8 months, budget ₹5L")


def test_goal_priority_validation():
    """Goal priority must be 1–10."""
    with pytest.raises(Exception):
        Goal(target_amount=Decimal("100000"), deadline=date.today(), priority=0)
    with pytest.raises(Exception):
        Goal(target_amount=Decimal("100000"), deadline=date.today(), priority=11)
    # boundary values pass
    g_low = Goal(target_amount=Decimal("100000"), deadline=date.today(), priority=1)
    g_high = Goal(target_amount=Decimal("100000"), deadline=date.today(), priority=10)
    assert g_low.priority == 1
    assert g_high.priority == 10
