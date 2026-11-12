"""Monthly cash-flow projection. Pure, Decimal-only, deterministic.

MODEL (monthly steps from `as_of`, capped at 120 months)
- Opening pool = savings + liquid_cash, falling back to cash when liquid_cash
  is None. (Stocks and every other holding are NOT in the pool.) The pool is
  split across instruments by plan.allocation.
- Income/month = recurring_income + INFLOW_CHANGE deltas (monthly-equivalent
  by cadence) from their start month.
- Fixed outflow/month = EMIs while month < remaining_months, RECURRING_OUTFLOW
  by cadence between start and end, insurance premium / 12, plus the optional
  monthly_living_expense. surplus = income - fixed.
- Saved/month = max(surplus, 0) x monthly_saving_rate / 100, added in the plan's
  proportions. A NEGATIVE surplus drains the pool via the withdrawal order
  (documented decision: a deficit is paid from savings, not ignored).
- ONE_TIME_OUTFLOW / ONE_TIME_INFLOW land in the month of their date.
- A GOAL is met if the pool BEFORE that month's one-time outflows is >= target
  in the goal's deadline month. A goal whose deadline is beyond the 120-month
  cap is unmet.
- HORIZON_SHIFT sets a goal's deadline to new_date; plan.deadline_shift_months
  is then added. Goals carry no ids, so every HORIZON_SHIFT on the hurdle applies
  to ALL goals (later shifts override earlier ones). One-time outflows that share a
  goal's ORIGINAL date move with it.
- Order inside a month: growth -> MARKET_STRESS shock -> income/fixed ->
  extra saving -> one-time inflows -> goal check -> one-time outflows ->
  liquidity-floor check.
- Withdrawal order: CASH, LIQUID, FIXED_DEPOSIT (exit penalty on the gross),
  EQUITY. Anything that cannot be paid is recorded as `unpaid`.
- Liquidity floor/month = max(all RISK_CONSTRAINT.min_liquidity,
  emergency_buffer_months x that month's fixed outflow); liquid value is CASH +
  LIQUID balances only.
- Every balance is quantized to the paisa (ROUND_HALF_EVEN) after each step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any, Iterable

from pydantic import BaseModel, TypeAdapter, ValidationError

from app.debate.schemas import INSTRUMENT_ORDER, Instrument, Plan
from app.enums import Cadence, PrimitiveType
from app.optimizer.assumptions import Assumptions, Scenario
from app.schemas.primitives import Primitive
from app.schemas.state import FinancialState

MAX_HORIZON = 120
CENT = Decimal("0.01")
ZERO = Decimal(0)
_CASH, _LIQ, _FD, _EQ = (
    Instrument.CASH_SAVINGS,
    Instrument.LIQUID_FUND,
    Instrument.FIXED_DEPOSIT,
    Instrument.EQUITY_INDEX,
)
_PRIM = TypeAdapter(Primitive)


def q(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_EVEN)


def parse_primitives(raw: Iterable[Any]) -> list:
    """Accept primitive models or stored dicts. Unrecognised entries are
    skipped: a pre-check must not crash on a primitive type it cannot model."""
    out = []
    for p in raw or []:
        if isinstance(p, BaseModel):
            out.append(p)
            continue
        try:
            out.append(_PRIM.validate_python(p))
        except ValidationError:
            continue
    return out


def opening_pool(state: FinancialState) -> Decimal:
    """savings + liquid_cash, falling back to cash when liquid_cash is None."""
    return (state.savings or ZERO) + (state.liquid_cash if state.liquid_cash is not None else (state.cash or ZERO))


def month_index(as_of: date, d: date) -> int:
    return max(0, (d.year - as_of.year) * 12 + (d.month - as_of.month))


def monthly_equivalent(amount: Decimal, cadence: Cadence) -> Decimal:
    if cadence == Cadence.WEEKLY:
        return amount * Decimal(52) / Decimal(12)
    if cadence == Cadence.MONTHLY:
        return amount
    if cadence == Cadence.QUARTERLY:
        return amount / Decimal(3)
    return amount / Decimal(12)


# ---- schedule --------------------------------------------------------------
@dataclass(frozen=True)
class Schedule:
    horizon: int
    income: list[Decimal]
    fixed: list[Decimal]
    inflows: list[Decimal]
    outflows: list[Decimal]
    goals: list[tuple[int, Decimal]]  # (deadline month, target), within cap
    unreachable_goal_total: Decimal  # goals beyond the 120-month cap
    unreachable_goal_count: int
    min_liquidity: Decimal
    opening_pool: Decimal
    first_goal_month: int | None
    first_event_month: int | None
    has_living_expense: bool = False
    notes: list[str] = field(default_factory=list)


def build_schedule(
    state: FinancialState,
    primitives: Iterable[Any],
    plan: Plan,
    as_of: date,
    monthly_living_expense: Decimal | None = None,
) -> Schedule:
    prims = parse_primitives(primitives)
    shifts = [p for p in prims if p.type == PrimitiveType.HORIZON_SHIFT]
    goals_raw = [p for p in prims if p.type == PrimitiveType.GOAL]

    goal_months: list[tuple[int, Decimal]] = []
    moved: dict[date, int] = {}  # original goal date -> new month
    unreachable_total, unreachable_n = ZERO, 0
    for g in goals_raw:
        new_date = g.deadline
        for s in shifts:
            new_date = s.new_date
        m = month_index(as_of, new_date) + plan.deadline_shift_months
        moved[g.deadline] = m
        if m >= MAX_HORIZON:
            unreachable_total += g.target_amount
            unreachable_n += 1
        else:
            goal_months.append((m, g.target_amount))

    out_events: list[tuple[int, Decimal]] = []
    in_events: list[tuple[int, Decimal]] = []
    for p in prims:
        if p.type == PrimitiveType.ONE_TIME_OUTFLOW:
            m = moved.get(p.due_date, month_index(as_of, p.due_date))
            out_events.append((m, p.amount))
        elif p.type == PrimitiveType.ONE_TIME_INFLOW:
            in_events.append((month_index(as_of, p.date), p.amount))

    in_range = [m for m, _ in goal_months] + [m for m, _ in out_events if m < MAX_HORIZON] + [
        m for m, _ in in_events if m < MAX_HORIZON
    ]
    horizon = min(max(in_range, default=0) + 1, MAX_HORIZON)

    def at(events):
        arr = [ZERO] * horizon
        for m, amt in events:
            if m < horizon:
                arr[m] += amt
        return arr

    income, fixed = [ZERO] * horizon, [ZERO] * horizon
    base_income = state.recurring_income or ZERO
    premiums = sum((i.premium or ZERO for i in state.insurance), ZERO) / Decimal(12)
    for m in range(horizon):
        inc = base_income
        for p in prims:
            if p.type == PrimitiveType.INFLOW_CHANGE and month_index(as_of, p.start) <= m:
                inc += monthly_equivalent(p.delta, p.cadence)
        income[m] = inc
        fx = premiums + (monthly_living_expense or ZERO)
        for e in state.emis:
            if e.remaining_months is None or m < e.remaining_months:
                fx += e.amount
        for p in prims:
            if p.type == PrimitiveType.RECURRING_OUTFLOW:
                start = month_index(as_of, p.start)
                end = month_index(as_of, p.end) if p.end else None
                if start <= m and (end is None or m <= end):
                    fx += monthly_equivalent(p.amount, p.cadence)
        fixed[m] = fx

    pool = opening_pool(state)
    min_liq = max((p.min_liquidity for p in prims if p.type == PrimitiveType.RISK_CONSTRAINT), default=ZERO)
    all_goal_m = [m for m, _ in goal_months]
    all_out_m = [m for m, _ in out_events if m < horizon]
    return Schedule(
        horizon=horizon,
        income=income,
        fixed=fixed,
        inflows=at(in_events),
        outflows=at(out_events),
        goals=sorted(goal_months),
        unreachable_goal_total=unreachable_total,
        unreachable_goal_count=unreachable_n,
        min_liquidity=min_liq,
        opening_pool=pool,
        first_goal_month=min(all_goal_m) if all_goal_m else None,
        first_event_month=min(all_goal_m + all_out_m) if (all_goal_m or all_out_m) else None,
        has_living_expense=monthly_living_expense is not None,
    )


# ---- simulation ------------------------------------------------------------
class MonthRecord(BaseModel):
    month: int
    balances: dict[Instrument, Decimal]
    pool: Decimal
    liquid: Decimal
    floor: Decimal
    slack: Decimal


class ScenarioResult(BaseModel):
    scenario: Scenario
    feasible: bool
    goals_met: int
    goals_total: int
    goal_shortfall: Decimal
    min_liquidity_slack: Decimal
    first_breach_month: int | None
    unpaid: Decimal
    fd_penalties: Decimal
    terminal_pool: Decimal
    trace: list[MonthRecord] = []


def split_amount(amount: Decimal, allocation: dict[Instrument, int]) -> dict[Instrument, Decimal]:
    """Split by percent; paise left over go to the first non-zero instrument."""
    parts = {i: q(amount * Decimal(allocation[i]) / Decimal(100)) for i in INSTRUMENT_ORDER}
    first = next(i for i in INSTRUMENT_ORDER if allocation[i] > 0)
    parts[first] += amount - sum(parts.values(), ZERO)
    return parts


def _withdraw(bal: dict[Instrument, Decimal], need: Decimal, penalty: Decimal) -> tuple[Decimal, Decimal]:
    """Pay `need` (net). Returns (unpaid, penalty_paid). Mutates bal."""
    paid_penalty = ZERO
    for inst in (_CASH, _LIQ):
        if need <= 0:
            break
        take = min(bal[inst], need)
        bal[inst] -= take
        need -= take
    if need > 0 and bal[_FD] > 0:
        gross = (need / (Decimal(1) - penalty)).quantize(CENT, rounding=ROUND_CEILING)
        if gross <= bal[_FD]:
            bal[_FD] -= gross
            paid_penalty += gross - need
            need = ZERO
        else:
            net = (bal[_FD] * (Decimal(1) - penalty)).quantize(CENT, rounding=ROUND_DOWN)
            paid_penalty += bal[_FD] - net
            bal[_FD] = ZERO
            need -= net
    if need > 0:
        take = min(bal[_EQ], need)
        bal[_EQ] -= take
        need -= take
    return max(need, ZERO), paid_penalty


def simulate(
    sched: Schedule,
    plan: Plan,
    assumptions: Assumptions,
    scenario: Scenario = Scenario.BASE,
    extra_monthly_saving: Decimal = ZERO,
    keep_trace: bool = False,
) -> ScenarioResult:
    alloc = plan.allocation
    rate_pct = Decimal(plan.monthly_saving_rate) / Decimal(100)
    mrate = {i: assumptions.monthly_rate(i) for i in INSTRUMENT_ORDER}
    bal = split_amount(q(sched.opening_pool), alloc)

    stress_month = None
    if scenario == Scenario.MARKET_STRESS:
        anchor = sched.first_goal_month if sched.first_goal_month is not None else sched.first_event_month
        if anchor is not None:
            stress_month = max(anchor - 1, 0)
    income_mult = assumptions.income_stress if scenario == Scenario.INCOME_STRESS else Decimal(1)

    shortfall = sched.unreachable_goal_total
    goals_met = 0
    unpaid = ZERO
    penalties = ZERO
    min_slack: Decimal | None = None
    first_breach: int | None = None
    trace: list[MonthRecord] = []
    goals_by_month: dict[int, list[Decimal]] = {}
    for gm, target in sched.goals:
        goals_by_month.setdefault(gm, []).append(target)

    for m in range(sched.horizon):
        for i in INSTRUMENT_ORDER:
            bal[i] = q(bal[i] + bal[i] * mrate[i])
        if stress_month == m:
            bal[_EQ] = q(bal[_EQ] * (Decimal(1) - assumptions.market_shock))

        surplus = q(sched.income[m] * income_mult) - q(sched.fixed[m])
        if surplus > 0:
            saved = q(surplus * rate_pct)
            if saved > 0:
                for i, v in split_amount(saved, alloc).items():
                    bal[i] += v
        elif surplus < 0:
            short, pen = _withdraw(bal, -surplus, assumptions.fd_exit_penalty)
            unpaid += short
            penalties += pen
        if extra_monthly_saving > 0:
            for i, v in split_amount(extra_monthly_saving, alloc).items():
                bal[i] += v
        if sched.inflows[m] > 0:
            for i, v in split_amount(q(sched.inflows[m]), alloc).items():
                bal[i] += v

        pool_before = sum(bal.values(), ZERO)
        for target in goals_by_month.get(m, []):
            if pool_before >= target:
                goals_met += 1
            else:
                shortfall += target - pool_before

        if sched.outflows[m] > 0:
            short, pen = _withdraw(bal, q(sched.outflows[m]), assumptions.fd_exit_penalty)
            unpaid += short
            penalties += pen

        floor = max(sched.min_liquidity, q(Decimal(plan.emergency_buffer_months) * sched.fixed[m]))
        liquid = bal[_CASH] + bal[_LIQ]
        slack = liquid - floor
        if min_slack is None or slack < min_slack:
            min_slack = slack
        if slack < 0 and first_breach is None:
            first_breach = m
        if keep_trace:
            trace.append(
                MonthRecord(month=m, balances=dict(bal), pool=sum(bal.values(), ZERO), liquid=liquid, floor=floor, slack=slack)
            )

    goals_total = len(sched.goals) + sched.unreachable_goal_count
    return ScenarioResult(
        scenario=scenario,
        feasible=(goals_met == goals_total and shortfall == 0 and unpaid == 0 and first_breach is None),
        goals_met=goals_met,
        goals_total=goals_total,
        goal_shortfall=shortfall,
        min_liquidity_slack=min_slack if min_slack is not None else ZERO,
        first_breach_month=first_breach,
        unpaid=unpaid,
        fd_penalties=penalties,
        terminal_pool=sum(bal.values(), ZERO),
        trace=trace,
    )
