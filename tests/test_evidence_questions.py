from datetime import date, timedelta
from decimal import Decimal

from app.evidence.questions import EvidenceTopic, derive_evidence_questions
from app.schemas.primitives import (
    Goal,
    HorizonShift,
    InflowChange,
    OneTimeOutflow,
    RecurringOutflow,
    RiskConstraint,
)
from app.enums import Cadence


def _topics(prims):
    return {q.topic for q in derive_evidence_questions(prims)}


def _wedding():
    d = date.today() + timedelta(days=240)
    return [
        OneTimeOutflow(amount=Decimal("500000"), due_date=d),
        Goal(target_amount=Decimal("500000"), deadline=d, priority=1),
        RiskConstraint(min_liquidity=Decimal("100000"), reason="wedding buffer"),
    ]


def test_wedding_primitives_expected_questions():
    assert _topics(_wedding()) == {EvidenceTopic.SAVINGS_INSTRUMENTS, EvidenceTopic.EMERGENCY_FUND}


def test_works_on_stored_dict_form():
    dicts = [p.model_dump(mode="json") for p in _wedding()]
    assert _topics(dicts) == _topics(_wedding())


def test_goal_alone_is_not_savings_question():
    d = date.today()
    assert _topics([Goal(target_amount=Decimal("1"), deadline=d, priority=1)]) == set()


def test_each_remaining_mapping():
    d = date.today()
    assert _topics([RecurringOutflow(amount=Decimal("1"), cadence=Cadence.MONTHLY, start=d)]) == {EvidenceTopic.COST_OF_LIVING}
    assert _topics([HorizonShift(goal_ref="x", new_date=d)]) == {EvidenceTopic.OPPORTUNITY_COST_OF_DELAY}


def test_inflow_change_sign_split():
    d = date.today()
    neg = InflowChange(delta=Decimal("-1000"), cadence=Cadence.MONTHLY, start=d)
    pos = InflowChange(delta=Decimal("1000"), cadence=Cadence.MONTHLY, start=d)
    zero = InflowChange(delta=Decimal("0"), cadence=Cadence.MONTHLY, start=d)
    assert _topics([neg]) == {EvidenceTopic.INCOME_SMOOTHING}
    assert _topics([pos]) == {EvidenceTopic.INVESTMENT_CAPACITY}
    assert _topics([zero]) == set()


def test_unrecognized_input_degrades_never_raises():
    assert derive_evidence_questions([]) == []
    assert derive_evidence_questions(None) == []
    junk = [{"type": "WEDDING"}, {"nope": 1}, "garbage", 42, None, {"type": "INFLOW_CHANGE", "delta": "abc"}]
    assert derive_evidence_questions(junk) == []


def test_no_user_data_in_questions():
    for q in derive_evidence_questions(_wedding()):
        assert "500000" not in q.text and "wedding" not in q.text.lower()
