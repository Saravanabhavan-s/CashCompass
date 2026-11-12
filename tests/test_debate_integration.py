"""3c integration: full scripted debate on the REAL fixture snapshot, scored by
the deterministic evaluator. No network, no real LLM."""
import json
import re
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from app.debate.context import build_debate_context
from app.debate.engine import run_debate, save_debate_run
from app.debate.evaluator import EvaluationReport, EvidenceQualitySelector, evaluate_debate
from app.debate.llm import ScriptedLLM
from app.schemas.hurdle import HurdleCreate
from app.schemas.primitives import Goal, OneTimeOutflow, RiskConstraint
from app.services.evidence import build_evidence_snapshot
from app.services.hurdles import create_hurdle
from tests.debate_helpers import default_responder, fact, handles_in, inference, plan, reply

_NUM = re.compile(r"\d+(?:\.\d+)?")


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)


@pytest.fixture
def setup(db_session):
    d = date.today() + timedelta(days=240)
    h = create_hurdle(
        db_session,
        HurdleCreate(
            label="Wedding",
            raw_description="Wedding in ~8 months",
            primitives=[
                OneTimeOutflow(amount=Decimal("500000"), due_date=d),
                Goal(target_amount=Decimal("500000"), deadline=d, priority=1),
                RiskConstraint(min_liquidity=Decimal("100000"), reason="wedding buffer"),
            ],
        ),
    )
    snap = build_evidence_snapshot(db_session, h.id)
    return h.id, snap, build_debate_context(db_session, h.id, snap)


def test_end_to_end_report(db_session, setup):
    hid, snap, ctx = setup

    def responder(req):
        t = req.tag
        hs = handles_in(req.prompt)
        base = default_responder(req)
        if (t["group"], t["persona"]) == ("group_2", "contrarian") and t["round"] == 3:
            # hallucinating agent: cites a handle that does not exist
            return reply(plan(40, 30, 25, 5, rate=15, buf=6), [fact(["E9999"]), inference()], "SUMMARY-x")
        if (t["group"], t["persona"]) == ("group_1", "saver"):
            # grounded agent: quotes a number that really is in its first cited item
            item = ctx.handle_map[hs[0]]
            fig = _NUM.search(item.content)
            figs = [fig.group(0)] if fig else []
            return reply(plan(20, 20, 50, 10, rate=30), [fact(hs[:1], figs), inference()], "SUMMARY-x")
        return base

    llm = ScriptedLLM(responder)
    res = run_debate(ctx, llm, selector=EvidenceQualitySelector())
    assert len(llm.calls) == 18 and not res.degraded

    rep = evaluate_debate(res, ctx)
    assert 0.0 <= rep.agreement <= 1.0
    assert rep.candidates and all(0.0 <= c.support <= 1.0 for c in rep.candidates)
    keys = [c.plan.key() for c in rep.candidates]
    assert len(keys) == len(set(keys))  # deduped
    assert rep.final_round == 3 and rep.snapshot_hash == snap.snapshot_hash
    assert set(rep.group_scores) == {"group_1", "group_2"}

    # the one hallucinating claim is found, the grounded saver claims are not
    bad = [f for f in rep.flagged_claims]
    assert [(f.group, f.persona, f.round_no, f.reasons) for f in bad] == [("group_2", "contrarian", 3, ["unknown_handle"])]
    assert rep.hallucination.overall.flagged == 1
    assert rep.hallucination.by_persona["contrarian"].flagged == 1
    assert rep.hallucination.by_round["3"].flagged == 1 and rep.hallucination.by_round["1"].flagged == 0
    saver_scores = [a for a in rep.agent_scores if a.persona == "saver" and a.group == "group_1"]
    assert saver_scores and all(not c.flagged for a in saver_scores for c in a.claims)

    # the evidence-quality selector made every leader decision, with its scores
    assert all(set(d.scores) <= {"group_1", "group_2"} and d.scores for d in res.leaders)
    assert res.final_leader_group == max(res.leaders[-1].scores, key=lambda g: (res.leaders[-1].scores[g], g == "group_1"))

    # JSON round-trip
    dumped = rep.model_dump(mode="json")
    json.dumps(dumped)
    assert EvaluationReport.model_validate(dumped) == rep

    # a persisted run can be re-scored identically
    run = save_debate_run(db_session, hid, snap, res, "scripted")
    from app.debate.engine import load_debate_result

    assert evaluate_debate(load_debate_result(run), ctx) == rep


def test_clean_default_run_has_zero_hallucinations(setup):
    _, _, ctx = setup
    res = run_debate(ctx, ScriptedLLM(default_responder), selector=EvidenceQualitySelector())
    rep = evaluate_debate(res, ctx)
    assert rep.hallucination.overall.rate == 0.0 and rep.flagged_claims == []
    assert all(a.score > 0 for a in rep.agent_scores)


def test_case_citations_end_to_end(setup):
    _, _, ctx = setup
    case = ctx.handle_map["C1"].content
    assert "5–6 lakh" in case  # the 5,00,000 goal, banded

    def responder(req):
        t = req.tag
        if (t["group"], t["persona"]) == ("group_1", "saver"):
            claims = [fact(["C1"], ["5-6 lakh"]), fact(["C2"]), inference()]  # grounded case claim; unknown handle
            return reply(plan(20, 20, 50, 10, rate=30), claims, "s")
        return default_responder(req)

    res = run_debate(ctx, ScriptedLLM(responder), selector=EvidenceQualitySelector())
    rep = evaluate_debate(res, ctx)
    flagged = [(f.group, f.persona, f.reasons) for f in rep.flagged_claims]
    assert flagged == [("group_1", "saver", ["unknown_handle"])] * 3  # one per round; the C1 claim is NOT flagged
    assert rep.citations.citing_case == 3 and rep.citations.citing_evidence == 15
    assert rep.citations.facts_total == 21 and rep.citations.uncited == 0
