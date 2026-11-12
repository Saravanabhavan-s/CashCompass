"""Hard allocation policy. Violating plans are rejected, never recommended."""
from __future__ import annotations

from app.debate.schemas import Instrument, Plan

EQUITY_ZERO_BELOW_MONTHS = 12  # goal under 12 months away -> no equity
EQUITY_CAP_BELOW_MONTHS = 36  # goal under 36 months away -> equity <= cap
EQUITY_CAP_PCT = 20

V_ZERO = "violates_policy:equity_must_be_0_when_goal_under_12_months"
V_CAP = "violates_policy:equity_must_be_at_most_20_when_goal_under_36_months"


def max_equity_pct(months_to_first_goal: int | None) -> int:
    if months_to_first_goal is None:
        return 100
    if months_to_first_goal < EQUITY_ZERO_BELOW_MONTHS:
        return 0
    if months_to_first_goal < EQUITY_CAP_BELOW_MONTHS:
        return EQUITY_CAP_PCT
    return 100


def policy_violations(plan: Plan, months_to_first_goal: int | None) -> list[str]:
    eq = plan.allocation[Instrument.EQUITY_INDEX]
    if months_to_first_goal is None:
        return []
    if months_to_first_goal < EQUITY_ZERO_BELOW_MONTHS and eq > 0:
        return [V_ZERO]
    if EQUITY_ZERO_BELOW_MONTHS <= months_to_first_goal < EQUITY_CAP_BELOW_MONTHS and eq > EQUITY_CAP_PCT:
        return [V_CAP]
    return []
