"""3e: recommendation orchestrator + API with ScriptedLLM (no network, no real model)."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_llm
from app.debate.engine import run_debate
from app.debate.llm import ScriptedLLM
from app.enums import LedgerEntryType, LedgerSource
from app.exceptions import AppendOnlyViolation, DebateInputLeak
from app.main import app
from app.models.ledger import LedgerEntry
from app.models.recommendation import Recommendation, RecommendationDecision
from app.schemas.hurdle import HurdleCreate
from app.schemas.ledger import EffectDelta, LedgerEntryCreate
from app.schemas.primitives import Goal, HorizonShift, OneTimeOutflow, RiskConstraint
from app.services.evidence import build_evidence_snapshot
from app.services.hurdles import create_hurdle
from app.services.ledger import append_entry
from app.services.recommendation import _GuardedLLM
from app.services.state import rebuild_state_from_ledger
from tests.debate_helpers import fact, handles_in, inference, plan, reply

D = Decimal
LIVING = {"monthly_living_expense": "30000"}


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)


def safe_responder(req):
    hs = handles_in(req.prompt)
    p = plan(30, 70, 0, 0, rate=50, shift=0, buf=3)  # no equity: policy-clean for an 8-month goal
    return reply(p, [fact(hs[:1]), inference()], summary=f"s-{req.tag['persona']}")


def ungrounded_responder(req):
    return reply(plan(30, 70, 0, 0, rate=50, buf=3), [inference("only my opinion")], summary="s")


@pytest.fixture
def llm():
    return ScriptedLLM(safe_responder)


@pytest.fixture
def api(client, db_session, llm):
    """TestClient with the LLM dependency overridden (cleared by the client fixture)."""
    app.dependency_overrides[get_llm] = lambda: llm
    return client


def seed_state(db):
    append_entry(
        db,
        LedgerEntryCreate(
            effective_date=date.today(),
            type=LedgerEntryType.STATE_SNAPSHOT,
            source=LedgerSource.USER_STATED,
            description="seed",
            effect=[
                EffectDelta(field="savings", op="set", value=800000.0),
                EffectDelta(field="recurring_income", op="set", value=75000.0),
            ],
        ),
    )


def wedding(db, amount="500000", days=240, label="Wedding"):
    d = date.today() + timedelta(days=days)
    return create_hurdle(
        db,
        HurdleCreate(
            label=label,
            raw_description="Wedding in ~8 months",
            primitives=[
                OneTimeOutflow(amount=D(amount), due_date=d),
                Goal(target_amount=D(amount), deadline=d, priority=1),
                RiskConstraint(min_liquidity=D("100000"), reason="wedding buffer"),
            ],
        ),
    ).id


@pytest.fixture
def hid(db_session):
    seed_state(db_session)
    return wedding(db_session)


def post_rec(api, hid, body=LIVING):
    return api.post(f"/hurdles/{hid}/recommendation", json=body)


def confirm(api, rid, ack=False, by="demo-user"):
    return api.post(f"/recommendations/{rid}/confirm", json={"confirmed_by": by, "acknowledge_warnings": ack})


def ledger_commitments(db):
    db.expire_all()
    return db.query(LedgerEntry).filter(LedgerEntry.type == "COMMITMENT").all()


# ---- full flow --------------------------------------------------------------
def test_full_flow_create_get_confirm(api, db_session, hid, llm):
    r = post_rec(api, hid)
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["outcome"] == "RECOMMENDED" and rec["status"] == "PENDING"
    assert rec["disclaimer"] == "Decision support, not financial advice."
    assert rec["guardrails"]["blocked"] is False and rec["guardrails"]["needs_ack"] is False
    assert rec["chosen_plan"]["allocation"]["EQUITY_INDEX"] == 0
    assert rec["citations"] and {"handle", "source", "citation", "query"} <= set(rec["citations"][0])
    assert rec["debate_run_id"] and rec["snapshot_hash"]
    assert D(rec["confidence"]) > D("0.75") and rec["confidence_band"] == "HIGH"
    assert set(rec["confidence_components"]) == {"robustness", "agreement", "evidence_quality", "completeness"}
    assert D(rec["user_figures"]["opening_pool"]) == D("800000")  # exact figures, user's own view only
    assert D(rec["user_figures"]["monthly_surplus"]) == D("45000")
    assert len(llm.calls) == 18

    assert api.get(f"/recommendations/{rec['id']}").json() == rec

    state_before = rebuild_state_from_ledger(db_session)
    assert ledger_commitments(db_session) == []
    c = confirm(api, rec["id"])
    assert c.status_code == 200, c.text
    out = c.json()
    assert out["status"] == "CONFIRMED" and out["decision"]["decided_by"] == "demo-user"

    entries = ledger_commitments(db_session)
    assert len(entries) == 1
    e = entries[0]
    assert (e.type, e.source, e.linked_hurdle_id, e.effect) == ("COMMITMENT", "AGENT_SUGGESTED_USER_CONFIRMED", hid, [])
    assert e.evidence["recommendation_id"] == rec["id"] and e.evidence["debate_run_id"] == rec["debate_run_id"]
    assert e.evidence["snapshot_hash"] == rec["snapshot_hash"] and e.evidence["citations"] == rec["citations"]
    assert e.evidence["confidence"]["band"] == "HIGH"
    assert "Allocate 30% cash savings, 70% liquid fund" in e.description
    assert out["decision"]["ledger_entry_id"] == e.id
    assert rebuild_state_from_ledger(db_session) == state_before  # state changes only with real transactions


def test_reject_path(api, db_session, hid):
    rid = post_rec(api, hid).json()["id"]
    r = api.post(f"/recommendations/{rid}/reject", json={"rejected_by": "demo-user", "reason": "too aggressive"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED" and r.json()["decision"]["reason"] == "too aggressive"
    assert ledger_commitments(db_session) == []
    assert confirm(api, rid).status_code == 409  # decided already
    assert api.post(f"/recommendations/{rid}/reject", json={"rejected_by": "x", "reason": "y"}).status_code == 409


def test_double_confirm_409_and_single_ledger_entry(api, db_session, hid):
    rid = post_rec(api, hid).json()["id"]
    assert confirm(api, rid).status_code == 200
    assert confirm(api, rid).status_code == 409
    assert len(ledger_commitments(db_session)) == 1


# ---- warnings / blocks ------------------------------------------------------
def test_warn_requires_acknowledgement(api, db_session, hid):
    rec = post_rec(api, hid, {}).json()  # no living expense -> INCOMPLETE_INPUT
    codes = [f["code"] for f in rec["guardrails"]["findings"]]
    assert "INCOMPLETE_INPUT" in codes and rec["guardrails"]["needs_ack"] and not rec["guardrails"]["blocked"]
    assert rec["confidence_band"] != "HIGH"  # WARN findings cap the band
    assert confirm(api, rec["id"]).status_code == 409
    assert ledger_commitments(db_session) == []
    assert confirm(api, rec["id"], ack=True).status_code == 200
    assert len(ledger_commitments(db_session)) == 1


def test_block_cannot_be_confirmed(api, hid, llm):
    llm._responder = ungrounded_responder  # no grounded FACT behind any plan
    rec = post_rec(api, hid).json()
    assert rec["outcome"] == "BLOCKED"
    assert "NO_GROUNDED_EVIDENCE" in [f["code"] for f in rec["guardrails"]["findings"]]
    assert confirm(api, rec["id"], ack=True).status_code == 409


def test_low_confidence_requires_acknowledgement(api, db_session, hid):
    # LOW band without any WARN finding: insert such a row directly
    rec = Recommendation(
        hurdle_id=hid, outcome="RECOMMENDED",
        snapshot_id=build_evidence_snapshot(db_session, hid).id, snapshot_hash="h",
        chosen_plan=plan(30, 70, 0, 0).model_dump(mode="json"), evaluation={}, citations=[],
        confidence=D("0.4000"), confidence_components={}, confidence_band="LOW",
        guardrails={"findings": [], "blocked": False, "needs_ack": False}, user_figures={}, disclaimer="d",
    )
    db_session.add(rec)
    db_session.commit()
    assert confirm(api, rec.id).status_code == 409
    assert confirm(api, rec.id, ack=True).status_code == 200


def test_infeasible_hurdle_makes_zero_llm_calls(api, db_session, llm):
    seed_state(db_session)
    h = wedding(db_session, amount="5000000")
    r = post_rec(api, h)
    assert r.status_code == 201
    rec = r.json()
    assert len(llm.calls) == 0
    assert rec["outcome"] == "INFEASIBLE" and rec["chosen_plan"] is None and rec["debate_run_id"] is None
    assert [f["code"] for f in rec["guardrails"]["findings"] if f["severity"] == "BLOCK"] == ["INFEASIBLE"]
    figs = rec["user_figures"]
    assert D(figs["best_case_shortfall"]) > 0
    assert D(figs["extra_monthly_saving_needed"]) > 0 and int(figs["months_of_delay_needed"]) > 12
    assert rec["confidence_band"] == "LOW" and D(rec["confidence"]) == 0
    assert confirm(api, rec["id"], ack=True).status_code == 409


def test_infeasible_gap_reports_none_when_no_delay_helps(api, db_session):
    seed_state(db_session)
    rec = post_rec(api, wedding(db_session, amount="50000000")).json()  # needs > 120 months of saving
    assert rec["outcome"] == "INFEASIBLE"
    assert rec["user_figures"]["months_of_delay_needed"] is None  # honest: no delay within the 120-month cap works
    assert D(rec["user_figures"]["extra_monthly_saving_needed"]) > 0


def test_leak_blocks_with_zero_llm_calls(api, db_session, llm):
    seed_state(db_session)
    d = date.today() + timedelta(days=100)
    h = create_hurdle(
        db_session,
        HurdleCreate(
            label="Acme Corp bonus", raw_description="x",
            primitives=[Goal(target_amount=D(100000), deadline=d, priority=1), HorizonShift(goal_ref="Acme Corp bonus", new_date=d)],
        ),
    ).id
    rec = post_rec(api, h).json()
    assert rec["outcome"] == "BLOCKED" and len(llm.calls) == 0
    assert [f["code"] for f in rec["guardrails"]["findings"] if f["severity"] == "BLOCK"] == ["LEAK"]
    assert confirm(api, rec["id"], ack=True).status_code == 409


# ---- errors -----------------------------------------------------------------
def test_503_without_llm(client, db_session, hid, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_provider", "none")
    r = client.post(f"/hurdles/{hid}/recommendation", json=LIVING)
    assert r.status_code == 503 and "LLM_PROVIDER" in r.json()["detail"]


def test_404s(api):
    assert api.post("/hurdles/nope/recommendation", json={}).status_code == 404
    assert api.get("/recommendations/nope").status_code == 404
    assert confirm(api, "nope").status_code == 404
    assert api.post("/recommendations/nope/reject", json={"rejected_by": "a", "reason": "b"}).status_code == 404


def test_confirm_validation(api, hid):
    rid = post_rec(api, hid).json()["id"]
    assert api.post(f"/recommendations/{rid}/confirm", json={"confirmed_by": ""}).status_code == 422


# ---- privacy ----------------------------------------------------------------
def test_prompts_contain_no_exact_amounts_or_tokens(api, hid, llm):
    assert post_rec(api, hid).status_code == 201
    assert len(llm.calls) == 18
    for req in llm.calls:
        blob = req.system + req.prompt
        for bad in ("800000", "8,00,000", "800,000", "75000", "75,000", "500000", "5,00,000", "500,000",
                    "100000", "1,00,000", "100,000", "30000", "45000", "TKN_", "Wedding", "wedding buffer"):
            assert bad not in blob, bad


def test_guarded_llm_blocks_amounts_and_tokens():
    inner = ScriptedLLM(lambda r: "{}")
    g = _GuardedLLM(inner, [D("800000.0"), D("1500"), D("75000")])
    from app.debate.llm import LLMRequest

    ok = g.complete(LLMRequest(system="s", prompt="saves 1500 and 5800000 and 800001"))  # <10k ignored; digit boundaries
    assert ok.text == "{}"
    for bad in ("balance 800000", "balance 8,00,000", "balance 800,000.00", "income 75000"):
        with pytest.raises(DebateInputLeak):
            g.complete(LLMRequest(system="s", prompt=bad))
    with pytest.raises(DebateInputLeak):
        g.complete(LLMRequest(system="s", prompt="note TKN_NOTE_ab12"))
    assert len(inner.calls) == 1  # nothing blocked ever reached the provider


def test_leak_during_debate_aborts_instead_of_degrading(db_session, hid):
    from app.debate.context import build_debate_context

    ctx = build_debate_context(db_session, hid, build_evidence_snapshot(db_session, hid))

    def responder(req):
        raise DebateInputLeak("leak")

    with pytest.raises(DebateInputLeak):
        run_debate(ctx, ScriptedLLM(responder))


# ---- determinism ------------------------------------------------------------
def test_same_inputs_same_scripted_replies_same_payload(api, hid):
    a = post_rec(api, hid).json()
    b = post_rec(api, hid).json()
    assert a["id"] != b["id"]
    for k in ("id", "created_at", "debate_run_id", "decision"):
        a.pop(k), b.pop(k)
    assert a == b


# ---- immutability -----------------------------------------------------------
def _rec_and_decision(api, db_session, hid):
    rid = post_rec(api, hid).json()["id"]
    confirm(api, rid)
    db_session.expire_all()
    return rid


@pytest.mark.parametrize("model,attr", [(Recommendation, "disclaimer"), (RecommendationDecision, "decided_by")])
def test_orm_update_delete_blocked(api, db_session, hid, model, attr):
    _rec_and_decision(api, db_session, hid)
    row = db_session.query(model).first()
    setattr(row, attr, "tampered")
    with pytest.raises(AppendOnlyViolation, match="append-only"):
        db_session.flush()
    db_session.rollback()
    row = db_session.query(model).first()
    db_session.delete(row)
    with pytest.raises(AppendOnlyViolation, match="append-only"):
        db_session.flush()
    db_session.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE recommendation SET disclaimer = 'x'",
        "DELETE FROM recommendation",
        "UPDATE recommendation_decision SET decided_by = 'x'",
        "DELETE FROM recommendation_decision",
    ],
)
def test_db_trigger_blocks_raw_sql(api, db_session, db_engine, hid, sql):
    _rec_and_decision(api, db_session, hid)
    with pytest.raises(Exception, match="append-only"):
        with db_engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()


def test_db_unique_decision_per_recommendation(api, db_session, db_engine, hid):
    rid = _rec_and_decision(api, db_session, hid)
    with pytest.raises(Exception, match="uq_recommendation_decision_one_per_rec"):
        with db_engine.connect() as conn:
            conn.execute(
                text("INSERT INTO recommendation_decision (id, recommendation_id, decision, decided_by) VALUES (:i, :r, 'REJECTED', 'x')"),
                {"i": str(uuid.uuid4()), "r": rid},
            )
            conn.commit()
