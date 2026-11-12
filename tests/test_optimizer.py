"""3d: deterministic optimizer. Hand-computed exact Decimals; zero-return
assumptions unless a test says otherwise."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.debate.schemas import Instrument, Plan
from app.evidence.feasibility import quick_feasibility_check
from app.optimizer.assumptions import Assumptions, Scenario
from app.optimizer.policy import V_CAP, V_ZERO, policy_violations
from app.optimizer.projection import _withdraw, build_schedule, simulate
from app.optimizer.search import (
    PlanEvaluation,
    assess_feasibility,
    evaluate_plan,
    optimize,
    rank_candidates,
)
from app.schemas.primitives import Goal, HorizonShift, OneTimeOutflow, RiskConstraint
from app.schemas.state import EMI, FinancialState

D = Decimal
AS_OF = date(2026, 1, 15)
ZERO_RATES = Assumptions(cash_rate=D(0), liquid_rate=D(0), fd_rate=D(0), equity_rate=D(0))
CASH, LIQ, FD, EQ = Instrument.CASH_SAVINGS, Instrument.LIQUID_FUND, Instrument.FIXED_DEPOSIT, Instrument.EQUITY_INDEX


def mk(cash=0, liq=0, fd=0, eq=0, rate=50, shift=0, buf=0) -> Plan:
    return Plan(
        allocation={"CASH_SAVINGS": cash, "LIQUID_FUND": liq, "FIXED_DEPOSIT": fd, "EQUITY_INDEX": eq},
        monthly_saving_rate=rate,
        deadline_shift_months=shift,
        emergency_buffer_months=buf,
    )


def st(savings="100000", income="50000", **kw) -> FinancialState:
    return FinancialState(savings=D(savings), recurring_income=D(income), **kw)


LIVING = D("20000")  # -> surplus 30000 with the default state
LIQ100 = mk(liq=100)


def sim(state, prims, plan=LIQ100, scenario=Scenario.BASE, living=LIVING, assumptions=ZERO_RATES, trace=True):
    sched = build_schedule(state, prims, plan, AS_OF, living)
    return simulate(sched, plan, assumptions, scenario, keep_trace=trace)


# ---- projection with 0% returns --------------------------------------------
def test_one_month_projection_exact():
    r = sim(st(), [Goal(target_amount=D(110000), deadline=date(2026, 1, 31), priority=1)])
    assert len(r.trace) == 1
    assert r.trace[0].balances[LIQ] == D("115000.00")  # 100000 + 30000 x 50%
    assert r.terminal_pool == D("115000.00") and r.goals_met == 1 and r.goal_shortfall == 0 and r.feasible


def test_three_month_projection_exact():
    g = lambda t: [Goal(target_amount=D(t), deadline=date(2026, 3, 20), priority=1)]
    r = sim(st(), g(145000))
    assert [m.pool for m in r.trace] == [D("115000.00"), D("130000.00"), D("145000.00")]
    assert r.goals_met == 1 and r.feasible  # >= target counts


def test_goal_missed_by_known_shortfall():
    r = sim(st(), [Goal(target_amount=D(160000), deadline=date(2026, 3, 20), priority=1)])
    assert r.goals_met == 0 and r.goal_shortfall == D("15000.00") and not r.feasible


def test_liquidity_floor_breach_in_specific_month():
    prims = lambda floor: [
        Goal(target_amount=D(100000), deadline=date(2026, 3, 20), priority=1),
        OneTimeOutflow(amount=D(120000), due_date=date(2026, 3, 20)),
        RiskConstraint(min_liquidity=D(floor), reason="r"),
    ]
    r = sim(st(), prims(30000))
    assert r.terminal_pool == D("25000.00")
    assert r.first_breach_month == 2 and r.min_liquidity_slack == D("-5000.00") and not r.feasible
    assert r.trace[0].slack == D("85000.00") and r.trace[1].slack == D("100000.00")
    ok = sim(st(), prims(20000))
    assert ok.first_breach_month is None and ok.min_liquidity_slack == D("5000.00") and ok.feasible


def test_emergency_buffer_scales_with_fixed_outflow():
    prims = [Goal(target_amount=D(1), deadline=date(2026, 1, 31), priority=1)]
    r = sim(st(), prims, plan=mk(liq=100, buf=3))
    assert r.trace[0].floor == D("60000.00")  # 3 x 20000 living
    assert r.trace[0].slack == D("55000.00")  # 115000 - 60000


def test_emi_and_cadence_in_fixed_outflow():
    state = st(emis=[EMI(label="x", amount=D(5000), remaining_months=2)])
    prims = [Goal(target_amount=D(1), deadline=date(2026, 4, 30), priority=1)]
    sched = build_schedule(state, prims, LIQ100, AS_OF, None)
    assert sched.fixed == [D(5000), D(5000), D(0), D(0)]  # EMI only while remaining_months > month


# ---- HORIZON_SHIFT ----------------------------------------------------------
def test_horizon_shift_moves_goal_and_its_outflow():
    prims = [
        Goal(target_amount=D(145000), deadline=date(2026, 3, 20), priority=1),
        OneTimeOutflow(amount=D(100000), due_date=date(2026, 3, 20)),  # shares the goal date -> moves
        OneTimeOutflow(amount=D(7000), due_date=date(2026, 2, 10)),  # unrelated -> stays
        HorizonShift(goal_ref="any", new_date=date(2026, 5, 20)),
    ]
    s = build_schedule(st(), prims, LIQ100, AS_OF, LIVING)
    assert s.goals == [(4, D(145000))] and s.horizon == 5
    assert s.outflows[4] == D(100000) and s.outflows[2] == 0 and s.outflows[1] == D(7000)
    # plan.deadline_shift_months alone does the same; both stack
    s2 = build_schedule(st(), prims[:3], mk(liq=100, shift=2), AS_OF, LIVING)
    assert s2.goals == [(4, D(145000))] and s2.outflows[4] == D(100000)
    s3 = build_schedule(st(), prims, mk(liq=100, shift=2), AS_OF, LIVING)
    assert s3.goals == [(6, D(145000))]


# ---- policy -----------------------------------------------------------------
@pytest.mark.parametrize(
    "months,eq,expected",
    [
        (6, 10, [V_ZERO]), (6, 0, []), (11, 5, [V_ZERO]),
        (12, 20, []), (12, 25, [V_CAP]), (35, 20, []), (35, 30, [V_CAP]),
        (36, 100, []), (None, 100, []),
    ],
)
def test_equity_policy_caps(months, eq, expected):
    p = mk(liq=100 - eq, eq=eq)
    assert policy_violations(p, months) == expected


def test_policy_violating_plan_is_rejected_never_chosen():
    prims = [Goal(target_amount=D(50000), deadline=date(2026, 7, 20), priority=1)]  # month 6
    bad, good = mk(liq=90, eq=10, rate=90), mk(liq=100, rate=50)
    assert evaluate_plan(st(), prims, bad, ZERO_RATES, LIVING, AS_OF).policy_violations == [V_ZERO]
    res = optimize(st(), prims, [(bad, 0.99), (good, 0.1)], ZERO_RATES, LIVING, AS_OF)
    assert res.status == "FEASIBLE" and res.chosen == good
    assert [r.plan for r in res.rejected_policy] == [bad]
    assert all(c.plan != bad for c in res.ranked)


# ---- scenarios --------------------------------------------------------------
def test_market_stress_hits_equity_the_month_before_the_deadline():
    state = FinancialState(savings=D(100000))
    prims = [Goal(target_amount=D(90000), deadline=date(2026, 3, 20), priority=1)]
    plan = mk(eq=100, rate=0)
    base = sim(state, prims, plan, living=None)
    stress = sim(state, prims, plan, Scenario.MARKET_STRESS, living=None)
    assert base.feasible and base.goal_shortfall == 0
    assert stress.goal_shortfall == D("20000.00") and not stress.feasible  # 100000 x 0.7 = 70000 < 90000
    assert stress.trace[0].balances[EQ] == D("100000.00") and stress.trace[1].balances[EQ] == D("70000.00")


def test_income_stress_cuts_income_and_drains_pool_on_deficit():
    state = FinancialState(savings=D(20000), recurring_income=D(50000))
    prims = [Goal(target_amount=D(40000), deadline=date(2026, 3, 20), priority=1)]
    plan = mk(liq=100, rate=100)
    living = D(40000)  # surplus 10000 base, -5000 at 70% income
    base = sim(state, prims, plan, living=living)
    stress = sim(state, prims, plan, Scenario.INCOME_STRESS, living=living)
    assert base.terminal_pool == D("50000.00") and base.feasible
    assert stress.terminal_pool == D("5000.00") and stress.goal_shortfall == D("35000.00") and not stress.feasible


def test_evaluate_plan_reports_all_three_scenarios():
    ev = evaluate_plan(st(), [Goal(target_amount=D(100000), deadline=date(2026, 3, 20), priority=1)], LIQ100, ZERO_RATES, LIVING, AS_OF)
    assert set(ev.scenario_results) == {"BASE", "MARKET_STRESS", "INCOME_STRESS"}
    assert ev.scenarios_feasible == 3 and ev.feasible_base
    assert all(r.trace == [] for r in ev.scenario_results.values())


# ---- withdrawal order -------------------------------------------------------
def _bal(c="1000", l="2000", f="10000", e="5000"):
    return {CASH: D(c), LIQ: D(l), FD: D(f), EQ: D(e)}


def test_withdrawal_order_cash_then_liquid_first():
    b = _bal()
    unpaid, pen = _withdraw(b, D(2500), D("0.03"))
    assert (unpaid, pen) == (0, 0) and b == {CASH: D(0), LIQ: D(500), FD: D(10000), EQ: D(5000)}


def test_withdrawal_fd_exit_penalty():
    b = _bal()
    unpaid, pen = _withdraw(b, D(4000), D("0.03"))  # 3000 from cash+liquid, 1000 net from FD
    assert unpaid == 0 and b[FD] == D("8969.07") and pen == D("30.93")  # gross 1000/0.97 rounded up
    assert b[EQ] == D(5000)


def test_withdrawal_exhausts_fd_then_equity_then_unpaid():
    b = _bal()
    unpaid, pen = _withdraw(b, D(14000), D("0.03"))  # FD yields 9700 net (300 penalty), 1300 from equity
    assert unpaid == 0 and b[FD] == 0 and pen == D("300.00") and b[EQ] == D("3700.00")
    b = _bal()
    unpaid, _ = _withdraw(b, D(20000), D("0.03"))  # total available 1000+2000+9700+5000 = 17700
    assert unpaid == D("2300.00") and sum(b.values()) == 0


# ---- ranking ----------------------------------------------------------------
def _ev(plan, sf=3, short="0", term="100", viol=()):
    return PlanEvaluation(
        plan=plan, feasible_base=True, scenario_results={}, scenarios_feasible=sf, goal_shortfall=D(short),
        min_liquidity_slack=D(0), terminal_pool=D(term), policy_violations=list(viol), assumptions_used=[],
    )


def test_ranking_order_lexicographic():
    A = (_ev(mk(liq=100, rate=20), term="100"), 0.5)
    B = (_ev(mk(liq=100, rate=30), term="50"), 0.9)
    C = (_ev(mk(liq=100, rate=40, shift=2), term="500"), 1.0)
    Dd = (_ev(mk(liq=100, rate=50), sf=2, term="900"), 1.0)
    E = (_ev(mk(liq=100, rate=60), term="200"), 0.5)
    F = (_ev(mk(liq=100, rate=70), short="10", term="900"), 1.0)
    G = (_ev(mk(liq=100, rate=80), viol=[V_ZERO]), 1.0)  # policy violator: excluded
    H = (_ev(mk(liq=100, rate=10), term="100"), 0.5)
    ranked = rank_candidates([A, B, C, Dd, E, F, G, H])
    assert [e.plan.monthly_saving_rate for e, _ in ranked] == [30, 60, 10, 20, 40, 70, 50]
    # B: best support | E: support tie, higher terminal | H,A: all tie -> Plan.key() | C: shift 2 | F: shortfall | D: 2 scenarios


# ---- repair + infeasible ----------------------------------------------------
def test_repair_search_finds_known_least_change_solution():
    state = st(savings="0")
    prims = [Goal(target_amount=D(100000), deadline=date(2026, 4, 20), priority=1)]  # month 3
    origin = mk(liq=100, rate=20)  # saves 6000 x 4 months = 24000: infeasible
    res = optimize(state, prims, [(origin, 0.8)], ZERO_RATES, LIVING, AS_OF)
    # surplus 30000/mo. shift 3 + rate 50 -> 7 months x 15000 = 105000 >= 100000 at distance 0.1375,
    # beating rate 85 / shift 0 (0.1625) and every other shift (>= 0.1417)
    assert res.status == "REPAIRED" and res.repaired_from == origin
    assert res.chosen == mk(liq=100, rate=50, shift=3)
    assert res.chosen_eval.feasible_base and res.gap is None


def test_infeasible_gap_numbers_by_hand():
    state = st(savings="0", income="10000")
    prims = [Goal(target_amount=D(100000), deadline=date(2026, 4, 20), priority=1)]  # month 3
    a = assess_feasibility(state, prims, ZERO_RATES, D(5000), AS_OF)
    # best case: 100% of 5000 surplus x 16 months (3 + 12 shift + 1) = 80000 -> short by 20000
    assert a.status == "INFEASIBLE" and a.evaluation.goal_shortfall == D("20000.00")
    # no delay: 4 months x (5000 + extra) >= 100000 -> extra = 20000
    assert a.gap.extra_monthly_saving == D(20000)
    # 100% saving: (4 + s) x 5000 >= 100000 -> s = 16 (beyond the 12-month lever)
    assert a.gap.months_of_delay == 16 and a.gap.best_case_shortfall == D("20000.00")
    res = optimize(state, prims, [(mk(liq=100, rate=100), 0.5)], ZERO_RATES, D(5000), AS_OF)
    assert res.status == "INFEASIBLE" and res.chosen is None and res.gap == a.gap


def test_feasible_hurdle_has_no_gap():
    a = assess_feasibility(st(), [Goal(target_amount=D(100000), deadline=date(2026, 3, 20), priority=1)], ZERO_RATES, LIVING, AS_OF)
    assert a.status == "FEASIBLE" and a.gap is None


# ---- assumptions ------------------------------------------------------------
def test_missing_living_expense_is_listed():
    prims = [Goal(target_amount=D(1000), deadline=date(2026, 3, 20), priority=1)]
    no = evaluate_plan(st(), prims, LIQ100, None, None, AS_OF)
    yes = evaluate_plan(st(), prims, LIQ100, None, LIVING, AS_OF)
    assert "living expense not provided -> surplus is an upper bound" in no.assumptions_used
    assert "living expense not provided -> surplus is an upper bound" not in yes.assumptions_used
    assert any("illustrative, replace with verified rates" in a for a in no.assumptions_used)


# ---- seed sanity ------------------------------------------------------------
def _seed_prims(today=None):
    d = (today or date.today()) + timedelta(days=240)
    return [
        OneTimeOutflow(amount=D(500000), due_date=d),
        Goal(target_amount=D(500000), deadline=d, priority=1),
        RiskConstraint(min_liquidity=D(100000), reason="wedding buffer"),
    ]


def test_seed_wedding_is_clearly_feasible():
    state = FinancialState(savings=D(800000), recurring_income=D(75000))
    ev = evaluate_plan(state, _seed_prims(), mk(cash=50, liq=50, rate=50))
    assert ev.feasible_base and ev.scenarios_feasible == 3 and not ev.policy_violations
    assert quick_feasibility_check(state, _seed_prims()) is True
    assert quick_feasibility_check(state, [p.model_dump(mode="json") for p in _seed_prims()]) is True  # stored form


def test_quick_check_false_for_impossible_hurdle():
    state = FinancialState(savings=D(1000), recurring_income=D(1000))
    d = date.today() + timedelta(days=180)
    prims = [Goal(target_amount=D(5000000), deadline=d, priority=1)]
    assert quick_feasibility_check(state, prims) is False
    assert quick_feasibility_check(state, [{"type": "MYSTERY"}]) is True  # unmodellable primitives are skipped


# ---- no floats, determinism -------------------------------------------------
def _assert_no_float(node, path="$"):
    assert not isinstance(node, float), f"float at {path}"
    if isinstance(node, dict):
        for k, v in node.items():
            _assert_no_float(v, f"{path}.{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _assert_no_float(v, f"{path}[{i}]")


def test_no_float_and_deterministic():
    state = st(savings="800000", income="75000")
    prims = _seed_prims(AS_OF)
    cands = [(mk(cash=25, liq=25, fd=25, eq=25, rate=30), 0.7), (mk(liq=100, rate=40), 0.4)]
    r1 = optimize(state, prims, cands, None, None, AS_OF)
    r2 = optimize(state, prims, cands, None, None, AS_OF)
    assert r1 == r2 and r1.model_dump_json() == r2.model_dump_json()
    dump = r1.model_dump(mode="python")
    dump["ranked"] = [{**c, "support": 0} for c in dump["ranked"]]  # `support` is the debate score, not rupees
    _assert_no_float(dump)
    _assert_no_float(evaluate_plan(state, prims, mk(liq=100), None, None, AS_OF).model_dump(mode="python"))


# ---- existing pipeline seam still works -------------------------------------
def test_run_evidence_pipeline_still_works(db_session):
    from app.schemas.hurdle import HurdleCreate
    from app.services.evidence import run_evidence_pipeline
    from app.services.hurdles import create_hurdle

    h = create_hurdle(
        db_session,
        HurdleCreate(label="x", raw_description="y", primitives=[Goal(target_amount=D(9999999), deadline=date.today() + timedelta(days=90), priority=1)]),
    )
    snap = run_evidence_pipeline(db_session, h.id)  # infeasible hurdle: the pipeline still builds; blocking is 3e's job
    assert snap.hurdle_id == h.id
