"""Deterministic plan aggregation. Pure Decimal arithmetic, no LLM."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from app.debate.schemas import INSTRUMENT_ORDER, Plan

_STEP = 5


def _weights(plans: Sequence[Plan], weights: Sequence[float] | None) -> list[Decimal]:
    if weights is None:
        return [Decimal(1)] * len(plans)
    if len(weights) != len(plans):
        raise ValueError("weights length must match plans")
    w = [Decimal(str(x)) for x in weights]
    if any(x < 0 for x in w):
        raise ValueError("weights must be non-negative")
    if sum(w) == 0:
        return [Decimal(1)] * len(plans)  # all-zero evidence score -> uniform
    return w


def _weighted_median(values: Sequence[int], w: Sequence[Decimal]) -> int:
    """Lower weighted median: smallest value whose cumulative weight >= half."""
    pairs = sorted(zip(values, w), key=lambda p: p[0])
    total = sum(x for _, x in pairs)
    acc = Decimal(0)
    for v, x in pairs:
        acc += x
        if acc * 2 >= total:
            return v
    return pairs[-1][0]


def _round5(x: int) -> int:
    return int((Decimal(x) / _STEP).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * _STEP


def aggregate_plans(plans: Sequence[Plan], weights: Sequence[float] | None = None) -> Plan:
    if not plans:
        raise ValueError("aggregate_plans needs at least one plan")
    w = _weights(plans, weights)
    total = sum(w)

    # allocation: weighted mean in units of 5, largest-remainder -> exactly 20 units
    units = 100 // _STEP
    exact = {
        inst: sum(Decimal(p.allocation[inst]) * wi for p, wi in zip(plans, w)) / total / _STEP
        for inst in INSTRUMENT_ORDER
    }
    floors = {i: int(exact[i]) for i in INSTRUMENT_ORDER}
    remaining = units - sum(floors.values())
    order = sorted(INSTRUMENT_ORDER, key=lambda i: (-(exact[i] - floors[i]), INSTRUMENT_ORDER.index(i)))
    for inst in order[:remaining]:
        floors[inst] += 1
    allocation = {i: floors[i] * _STEP for i in INSTRUMENT_ORDER}

    return Plan(
        allocation=allocation,
        monthly_saving_rate=min(100, _round5(_weighted_median([p.monthly_saving_rate for p in plans], w))),
        deadline_shift_months=_weighted_median([p.deadline_shift_months for p in plans], w),
        emergency_buffer_months=_weighted_median([p.emergency_buffer_months for p in plans], w),
    )


def plan_distance(a: Plan, b: Plan) -> float:
    """0 = identical, 1 = maximally different. Mean of four normalised parts."""
    alloc = Decimal(sum(abs(a.allocation[i] - b.allocation[i]) for i in INSTRUMENT_ORDER)) / 200
    rate = Decimal(abs(a.monthly_saving_rate - b.monthly_saving_rate)) / 100
    shift = Decimal(abs(a.deadline_shift_months - b.deadline_shift_months)) / 12
    buf = Decimal(abs(a.emergency_buffer_months - b.emergency_buffer_months)) / 12
    return float(((alloc + rate + shift + buf) / 4).quantize(Decimal("0.000001")))
