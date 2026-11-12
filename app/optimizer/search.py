"""Plan evaluation, ranking, repair search and real feasibility assessment.

Ranking is lexicographic: scenarios_feasible desc, goal_shortfall asc,
deadline_shift_months asc, support desc, terminal_pool desc, Plan.key().
Policy-violating plans are never ranked or recommended.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import product
from typing import Any, Iterable, Sequence

from pydantic import BaseModel

from app.debate.aggregate import plan_distance
from app.debate.schemas import INSTRUMENT_ORDER, Instrument, Plan
from app.optimizer.assumptions import Assumptions, Scenario
from app.optimizer.policy import max_equity_pct, policy_violations
from app.optimizer.projection import (
    MAX_HORIZON,
    Schedule,
    ScenarioResult,
    build_schedule,
    month_index,
    parse_primitives,
    simulate,
)
from app.enums import PrimitiveType
from app.schemas.state import FinancialState

ZERO = Decimal(0)
_CASH, _LIQ, _FD, _EQ = INSTRUMENT_ORDER


def _alloc(cash=0, liq=0, fd=0, eq=0) -> dict[Instrument, int]:
    return {_CASH: cash, _LIQ: liq, _FD: fd, _EQ: eq}


class PlanEvaluation(BaseModel):
    plan: Plan
    feasible_base: bool
    scenario_results: dict[str, ScenarioResult]
    scenarios_feasible: int  # 0-3
    goal_shortfall: Decimal  # BASE scenario
    min_liquidity_slack: Decimal  # BASE scenario
    terminal_pool: Decimal  # BASE scenario
    policy_violations: list[str]
    assumptions_used: list[str]


class Gap(BaseModel):
    extra_monthly_saving: Decimal | None  # whole rupees/month needed at no delay; None if no amount works
    months_of_delay: int | None  # deadline delay needed at 100% saving; None if none works
    best_case_shortfall: Decimal


class FeasibilityAssessment(BaseModel):
    status: str  # FEASIBLE | INFEASIBLE
    best_case_plan: Plan
    evaluation: PlanEvaluation
    gap: Gap | None = None


class RankedCandidate(BaseModel):
    plan: Plan
    support: float
    evaluation: PlanEvaluation


class RejectedCandidate(BaseModel):
    plan: Plan
    violations: list[str]


class OptimizerResult(BaseModel):
    status: str  # FEASIBLE | REPAIRED | INFEASIBLE
    chosen: Plan | None
    chosen_eval: PlanEvaluation | None
    ranked: list[RankedCandidate]
    rejected_policy: list[RejectedCandidate]
    repaired_from: Plan | None = None
    gap: Gap | None = None


def _assumption_notes(
    assumptions: Assumptions, living: Decimal | None, sched: Schedule, has_hs: bool
) -> list[str]:
    notes = [
        assumptions.describe(),
        "opening pool = savings + liquid_cash (falls back to cash); stocks and other holdings are excluded",
        "a negative monthly surplus is paid from the pool (CASH, LIQUID, FD with exit penalty, EQUITY)",
    ]
    if living is None:
        notes.append("living expense not provided -> surplus is an upper bound")
    if has_hs:
        notes.append("HORIZON_SHIFT applies to all goals on the hurdle (goals carry no ids)")
    if sched.unreachable_goal_count:
        notes.append("a goal falls beyond the 120-month horizon cap and is counted as unmet")
    return notes


def _has_hs(primitives) -> bool:
    return any(p.type == PrimitiveType.HORIZON_SHIFT for p in parse_primitives(primitives))


def evaluate_plan(
    state: FinancialState,
    primitives: Iterable[Any],
    plan: Plan,
    assumptions: Assumptions | None = None,
    monthly_living_expense: Decimal | None = None,
    as_of: date | None = None,
) -> PlanEvaluation:
    assumptions = assumptions or Assumptions()
    as_of = as_of or date.today()
    prims = parse_primitives(primitives)
    sched = build_schedule(state, prims, plan, as_of, monthly_living_expense)
    results = {s.value: simulate(sched, plan, assumptions, s) for s in Scenario}
    base = results[Scenario.BASE.value]
    return PlanEvaluation(
        plan=plan,
        feasible_base=base.feasible,
        scenario_results=results,
        scenarios_feasible=sum(r.feasible for r in results.values()),
        goal_shortfall=base.goal_shortfall,
        min_liquidity_slack=base.min_liquidity_slack,
        terminal_pool=base.terminal_pool,
        policy_violations=policy_violations(plan, sched.first_goal_month),
        assumptions_used=_assumption_notes(assumptions, monthly_living_expense, sched, _has_hs(prims)),
    )


def rank_key(ev: PlanEvaluation, support: float) -> tuple:
    return (
        -ev.scenarios_feasible,
        ev.goal_shortfall,
        ev.plan.deadline_shift_months,
        -Decimal(str(support)),
        -ev.terminal_pool,
        ev.plan.key(),
    )


def rank_candidates(evals: Sequence[tuple[PlanEvaluation, float]]) -> list[tuple[PlanEvaluation, float]]:
    """Policy-clean candidates only, best first."""
    clean = [(e, s) for e, s in evals if not e.policy_violations]
    return sorted(clean, key=lambda es: rank_key(*es))


# ---- feasibility -----------------------------------------------------------
def _best_case_plans(shift: int = 12) -> list[Plan]:
    return [
        Plan(allocation=a, monthly_saving_rate=100, deadline_shift_months=shift, emergency_buffer_months=0)
        for a in (_alloc(liq=100), _alloc(cash=100), _alloc(cash=50, liq=50))
    ]


def _base_feasible(state, prims, plan, assumptions, living, as_of, extra=ZERO) -> bool:
    sched = build_schedule(state, prims, plan, as_of, living)
    return simulate(sched, plan, assumptions, Scenario.BASE, extra).feasible


def _compute_gap(state, prims, assumptions, living, as_of, best_shortfall: Decimal) -> Gap:
    plan0 = Plan(allocation=_alloc(liq=100), monthly_saving_rate=100, deadline_shift_months=0, emergency_buffer_months=0)
    sched0 = build_schedule(state, prims, plan0, as_of, living)

    # extra monthly saving, whole rupees, at no delay (binary search; feasibility is monotone in it)
    hi = int(
        sum((t for _, t in sched0.goals), ZERO)
        + sched0.unreachable_goal_total
        + sum(sched0.outflows, ZERO)
        + sched0.min_liquidity
        + sum(sched0.fixed, ZERO)
        + 1
    )
    extra: Decimal | None = None
    if sched0.unreachable_goal_count == 0 and _base_feasible(state, prims, plan0, assumptions, living, as_of, Decimal(hi)):
        lo = 0
        while lo < hi:
            mid = (lo + hi) // 2
            if _base_feasible(state, prims, plan0, assumptions, living, as_of, Decimal(mid)):
                hi = mid
            else:
                lo = mid + 1
        extra = Decimal(lo)

    delay: int | None = None
    for s in range(0, 13):  # the Plan lever itself is capped at 12 months
        if _base_feasible(state, prims, plan0.model_copy(update={"deadline_shift_months": s}), assumptions, living, as_of):
            delay = s
            break
    if delay is None:
        delay = _scan_long_delay(state, prims, assumptions, living, as_of)
    return Gap(extra_monthly_saving=extra, months_of_delay=delay, best_case_shortfall=best_shortfall)


def _scan_long_delay(state, prims, assumptions, living, as_of) -> int | None:
    """Delays beyond the Plan lever cap (12): shift the goal dates themselves
    by whole months, which is exactly what a longer deadline means."""
    from app.schemas.primitives import Goal, OneTimeOutflow

    plan0 = Plan(allocation=_alloc(liq=100), monthly_saving_rate=100, deadline_shift_months=0, emergency_buffer_months=0)
    goals = [p for p in prims if p.type == PrimitiveType.GOAL]
    if not goals:
        return None
    for s in range(13, MAX_HORIZON + 1):
        shifted = []
        goal_dates = {g.deadline for g in goals}
        for p in prims:
            if p.type == PrimitiveType.GOAL:
                shifted.append(Goal(target_amount=p.target_amount, deadline=_add_months(p.deadline, s), priority=p.priority))
            elif p.type == PrimitiveType.ONE_TIME_OUTFLOW and p.due_date in goal_dates:
                shifted.append(OneTimeOutflow(amount=p.amount, due_date=_add_months(p.due_date, s)))
            else:
                shifted.append(p)
        if any(month_index(as_of, g.deadline) >= MAX_HORIZON for g in shifted if g.type == PrimitiveType.GOAL):
            return None
        if _base_feasible(state, shifted, plan0, assumptions, living, as_of):
            return s
    return None


def _add_months(d: date, n: int) -> date:
    from calendar import monthrange

    total = d.month - 1 + n
    y, m = d.year + total // 12, total % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def assess_feasibility(
    state: FinancialState,
    primitives: Iterable[Any],
    assumptions: Assumptions | None = None,
    monthly_living_expense: Decimal | None = None,
    as_of: date | None = None,
) -> FeasibilityAssessment:
    """Best case = 100% saving rate, policy-clean allocation, max allowed
    deadline shift (12 months), no emergency buffer requirement beyond the
    hurdle's own RISK_CONSTRAINTs. If even that fails the hurdle is INFEASIBLE,
    with a quantified gap."""
    assumptions = assumptions or Assumptions()
    as_of = as_of or date.today()
    prims = parse_primitives(primitives)
    best_eval: PlanEvaluation | None = None
    for plan in _best_case_plans():
        ev = evaluate_plan(state, prims, plan, assumptions, monthly_living_expense, as_of)
        if ev.policy_violations:
            continue
        if ev.feasible_base:
            return FeasibilityAssessment(status="FEASIBLE", best_case_plan=plan, evaluation=ev)
        if best_eval is None or ev.goal_shortfall < best_eval.goal_shortfall:
            best_eval = ev
    assert best_eval is not None  # all-liquid/cash plans are never policy violations
    return FeasibilityAssessment(
        status="INFEASIBLE",
        best_case_plan=best_eval.plan,
        evaluation=best_eval,
        gap=_compute_gap(state, prims, assumptions, monthly_living_expense, as_of, best_eval.goal_shortfall),
    )


# ---- repair + optimize -----------------------------------------------------
def _clip_equity(alloc: dict[Instrument, int], cap: int) -> dict[Instrument, int]:
    excess = max(alloc[_EQ] - cap, 0)
    out = dict(alloc)
    out[_EQ] -= excess
    out[_LIQ] += excess
    return out


def _repair(state, prims, origin: Plan, assumptions, living, as_of) -> Plan | None:
    """Least-change feasible plan: smallest plan_distance from `origin`."""
    cands: dict[tuple, Plan] = {}
    buffers = sorted({origin.emergency_buffer_months, 0, 3, 6})
    for shift in range(0, 13):
        months = build_schedule(state, prims, origin.model_copy(update={"deadline_shift_months": shift}), as_of, living).first_goal_month
        cap = max_equity_pct(months)
        allocs = [
            _clip_equity(dict(origin.allocation), cap),
            _alloc(liq=100),
            _alloc(cash=50, liq=50),
            _alloc(cash=100),
            _alloc(liq=100 - cap, eq=cap),
        ]
        for a, rate, buf in product(allocs, range(0, 101, 5), buffers):
            p = Plan(allocation=a, monthly_saving_rate=rate, deadline_shift_months=shift, emergency_buffer_months=buf)
            if not policy_violations(p, months):
                cands[p.key()] = p
    for p in sorted(cands.values(), key=lambda c: (plan_distance(origin, c), c.key())):
        if _base_feasible(state, prims, p, assumptions, living, as_of):
            return p
    return None


def optimize(
    state: FinancialState,
    primitives: Iterable[Any],
    candidates: Sequence[tuple[Plan, float]],
    assumptions: Assumptions | None = None,
    monthly_living_expense: Decimal | None = None,
    as_of: date | None = None,
) -> OptimizerResult:
    assumptions = assumptions or Assumptions()
    as_of = as_of or date.today()
    prims = parse_primitives(primitives)

    # dedupe by key, keep the highest support
    best_support: dict[tuple, tuple[Plan, float]] = {}
    for p, s in candidates:
        cur = best_support.get(p.key())
        if cur is None or s > cur[1]:
            best_support[p.key()] = (p, s)

    evals = [(evaluate_plan(state, prims, p, assumptions, monthly_living_expense, as_of), s) for p, s in best_support.values()]
    rejected = [RejectedCandidate(plan=e.plan, violations=e.policy_violations) for e, _ in evals if e.policy_violations]
    ranked = rank_candidates(evals)
    ranked_out = [RankedCandidate(plan=e.plan, support=s, evaluation=e) for e, s in ranked]

    for e, s in ranked:
        if e.feasible_base:
            return OptimizerResult(status="FEASIBLE", chosen=e.plan, chosen_eval=e, ranked=ranked_out, rejected_policy=rejected)

    if ranked:
        origin = ranked[0][0].plan
    elif evals:
        origin = max(evals, key=lambda es: es[1])[0].plan
    else:
        origin = Plan(allocation=_alloc(liq=100), monthly_saving_rate=20, deadline_shift_months=0, emergency_buffer_months=3)

    repaired = _repair(state, prims, origin, assumptions, monthly_living_expense, as_of)
    if repaired is not None:
        ev = evaluate_plan(state, prims, repaired, assumptions, monthly_living_expense, as_of)
        return OptimizerResult(
            status="REPAIRED", chosen=repaired, chosen_eval=ev, ranked=ranked_out, rejected_policy=rejected, repaired_from=origin
        )

    assess = assess_feasibility(state, prims, assumptions, monthly_living_expense, as_of)
    gap = assess.gap or _compute_gap(state, prims, assumptions, monthly_living_expense, as_of, assess.evaluation.goal_shortfall)
    return OptimizerResult(status="INFEASIBLE", chosen=None, chosen_eval=None, ranked=ranked_out, rejected_policy=rejected, gap=gap)
