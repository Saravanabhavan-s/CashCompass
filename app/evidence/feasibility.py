"""Feasibility pre-check (real, since Layer 3d).

Whether a hurdle's GOAL is numerically reachable is decided by the
deterministic optimizer's best-case plan (100% saving rate, policy-clean
allocation, maximum allowed deadline shift). This wrapper keeps the original
signature and returns a bool; callers that need the quantified gap use
app.optimizer.search.assess_feasibility directly.

run_evidence_pipeline still ignores the bool on purpose: blocking an
infeasible hurdle is the Layer 3e orchestrator's job.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any


def quick_feasibility_check(
    state: Any,
    primitives: list[Any],
    *,
    as_of: date | None = None,
    assumptions: Any = None,
    monthly_living_expense: Decimal | None = None,
) -> bool:
    from app.optimizer.search import assess_feasibility  # lazy: optimizer imports debate/evidence modules

    return (
        assess_feasibility(
            state,
            primitives,
            assumptions=assumptions,
            monthly_living_expense=monthly_living_expense,
            as_of=as_of,
        ).status
        == "FEASIBLE"
    )
