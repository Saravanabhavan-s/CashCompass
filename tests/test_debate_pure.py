"""Pure (no DB) 3b tests: Plan validation, aggregation, distance, parsing."""
import json

import pytest
from pydantic import ValidationError

from app.debate.aggregate import aggregate_plans, plan_distance
from app.debate.engine import parse_agent_output
from app.debate.schemas import INSTRUMENT_ORDER, Claim, Instrument, Plan
from tests.debate_helpers import fact, inference, plan, reply


# ---- Plan validation -------------------------------------------------------
def test_plan_sum_must_be_100():
    with pytest.raises(ValidationError, match="sum to exactly 100"):
        plan(25, 25, 25, 24)


@pytest.mark.parametrize(
    "kw",
    [
        {"monthly_saving_rate": 101},
        {"monthly_saving_rate": -1},
        {"deadline_shift_months": 13},
        {"emergency_buffer_months": 13},
        {"emergency_buffer_months": -1},
    ],
)
def test_plan_levers_out_of_range(kw):
    base = dict(
        allocation={"CASH_SAVINGS": 100},
        monthly_saving_rate=10,
        deadline_shift_months=0,
        emergency_buffer_months=3,
    )
    base.update(kw)
    with pytest.raises(ValidationError):
        Plan(**base)


def test_plan_allocation_item_out_of_range():
    with pytest.raises(ValidationError):
        Plan(allocation={"CASH_SAVINGS": 120, "LIQUID_FUND": -20}, monthly_saving_rate=1, deadline_shift_months=0, emergency_buffer_months=0)


def test_missing_instruments_default_to_zero():
    p = Plan(allocation={"FIXED_DEPOSIT": 60, "EQUITY_INDEX": 40}, monthly_saving_rate=10, deadline_shift_months=0, emergency_buffer_months=0)
    assert p.allocation[Instrument.CASH_SAVINGS] == 0 and p.allocation[Instrument.LIQUID_FUND] == 0


def test_plan_key_hashable_and_equal():
    assert plan().key() == plan().key()
    assert plan().key() != plan(rate=21).key()
    assert len({plan().key(), plan().key()}) == 1


def test_claim_rejects_bad_handle():
    with pytest.raises(ValidationError):
        Claim(kind="FACT", text="x", evidence_ids=["3"])


# ---- aggregate_plans -------------------------------------------------------
def _plans():
    return [plan(20, 20, 50, 10, rate=30), plan(30, 50, 15, 5, rate=25, buf=6), plan(10, 20, 20, 50, rate=40), plan(33, 33, 33, 1, rate=7, shift=2)]


def test_aggregate_sums_100_multiples_of_5():
    for w in (None, [1, 2, 3, 4], [0.1, 0.1, 0.1, 5], [0, 0, 0, 0]):
        agg = aggregate_plans(_plans(), w)
        vals = [agg.allocation[i] for i in INSTRUMENT_ORDER]
        assert sum(vals) == 100 and all(v % 5 == 0 for v in vals)
        assert agg.monthly_saving_rate % 5 == 0


def test_aggregate_deterministic():
    assert aggregate_plans(_plans(), [1, 2, 3, 4]) == aggregate_plans(_plans(), [1, 2, 3, 4])


def test_weights_change_outcome():
    a, b = plan(100, 0, 0, 0, rate=0), plan(0, 0, 0, 100, rate=100)
    uni = aggregate_plans([a, b])
    assert (uni.allocation[Instrument.CASH_SAVINGS], uni.allocation[Instrument.EQUITY_INDEX]) == (50, 50)
    w = aggregate_plans([a, b], [3, 1])
    assert (w.allocation[Instrument.CASH_SAVINGS], w.allocation[Instrument.EQUITY_INDEX]) == (75, 25)
    assert aggregate_plans([a, b]).monthly_saving_rate == 0
    assert aggregate_plans([a, b], [1, 3]).monthly_saving_rate == 100


def test_largest_remainder_ties_follow_instrument_order():
    agg = aggregate_plans([plan(100, 0, 0, 0), plan(0, 100, 0, 0), plan(0, 0, 100, 0)])
    assert [agg.allocation[i] for i in INSTRUMENT_ORDER] == [35, 35, 30, 0]


def test_aggregate_rejects_empty_and_bad_weights():
    with pytest.raises(ValueError):
        aggregate_plans([])
    with pytest.raises(ValueError):
        aggregate_plans(_plans(), [1, 2])
    with pytest.raises(ValueError):
        aggregate_plans(_plans(), [-1, 1, 1, 1])


def test_plan_distance_bounds():
    a = plan(100, 0, 0, 0, rate=0, shift=0, buf=0)
    b = plan(0, 0, 0, 100, rate=100, shift=12, buf=12)
    assert plan_distance(a, a) == 0
    assert plan_distance(a, b) == 1
    assert plan_distance(a, b) == plan_distance(b, a)
    for x in _plans():
        for y in _plans():
            assert 0 <= plan_distance(x, y) <= 1


# ---- parse_agent_output ----------------------------------------------------
GOOD = reply(plan(), [fact(["E1"], ["3.5"]), inference()], summary="s")


def test_parse_plain():
    out = parse_agent_output(GOOD)
    assert out.plan == plan() and len(out.claims) == 2


def test_parse_fenced():
    assert parse_agent_output(f"```json\n{GOOD}\n```").plan == plan()
    assert parse_agent_output(f"```\n{GOOD}\n```").plan == plan()


def test_parse_prose_around():
    assert parse_agent_output(f"Sure! Here is my answer:\n{GOOD}\nHope that helps {{really}}.").plan == plan()


@pytest.mark.parametrize("bad", ["", "no json here", "{not json", "[1,2,3]", json.dumps({"plan": {}})])
def test_parse_garbage_rejected(bad):
    with pytest.raises(ValueError):
        parse_agent_output(bad)


def test_parse_invalid_plan_rejected_with_message():
    bad = json.loads(GOOD)
    bad["plan"]["allocation"]["CASH_SAVINGS"] = 99
    with pytest.raises(ValueError, match="sum to exactly 100"):
        parse_agent_output(json.dumps(bad))
