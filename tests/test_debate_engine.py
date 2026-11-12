"""DB-backed 3b tests: scripted end-to-end debates against the real fixture
snapshot. No network (blocked), no real LLM (ScriptedLLM)."""
import re
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.debate import prompts
from app.debate.context import build_debate_context
from app.debate.engine import load_debate_result, run_debate, save_debate_run
from app.debate.llm import ScriptedLLM, get_llm_client
from app.debate.schemas import DebateConfig
from app.evidence.personas import Persona
from app.exceptions import AppendOnlyViolation, DebateFailed, DebateInputLeak, LLMNotConfigured
from app.models.debate import DebateRun, DebateTurn
from app.schemas.hurdle import HurdleCreate
from app.schemas.primitives import Goal, HorizonShift, OneTimeOutflow, RiskConstraint
from app.services.evidence import build_evidence_snapshot
from app.services.hurdles import create_hurdle
from tests.debate_helpers import default_responder, fact, handles_in, plan, reply


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)


@pytest.fixture
def hurdle_id(db_session):
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
    return h.id


@pytest.fixture
def snapshot(db_session, hurdle_id):
    return build_evidence_snapshot(db_session, hurdle_id)


@pytest.fixture
def ctx(db_session, hurdle_id, snapshot):
    return build_debate_context(db_session, hurdle_id, snapshot)


# ---- config / llm ----------------------------------------------------------
def test_llm_not_configured_by_default(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_provider", "none")
    with pytest.raises(LLMNotConfigured):
        get_llm_client()


def test_case_is_citable_as_c1_without_exact_amounts(ctx):
    c1 = ctx.handle_map["C1"]
    assert c1.source == "CASE" and c1.handle == "C1"
    assert c1.content == ctx.case_json()  # exactly what the agents are shown
    assert "C1" not in {i.handle for i in ctx.items}  # not part of the persona evidence packs
    assert "lakh" in c1.content  # banded
    for bad in ("500000", "5,00,000", "500,000", "100000", "1,00,000", "100,000", "TKN_", "Wedding", "wedding buffer"):
        assert bad not in c1.content, bad


def test_every_prompt_labels_the_case_c1_and_system_explains_the_rule(ctx):
    llm = ScriptedLLM(default_responder)
    run_debate(ctx, llm)
    for req in llm.calls:
        assert "USER CASE [C1]" in req.prompt and ctx.case_json() in req.prompt
        assert "cite C1" in req.system and "external facts cite E-handles" in req.system


def test_keep_fact_rule_only_in_rounds_two_and_up(ctx):
    llm = ScriptedLLM(default_responder)
    run_debate(ctx, llm)
    rule = prompts.KEEP_FACT_RULE
    assert "do not downgrade all claims to INFERENCE" in rule
    for req in llm.calls:
        in_prompt = rule in req.prompt
        assert in_prompt == (req.tag["round"] >= 2), req.tag
        assert rule not in req.system  # system prompt is shared by all rounds


def test_prompt_version_is_stable():
    assert re.fullmatch(r"[0-9a-f]{16}", prompts.PROMPT_VERSION)


# ---- full run --------------------------------------------------------------
def test_full_run_18_calls_temperature(ctx):
    llm = ScriptedLLM(default_responder)
    res = run_debate(ctx, llm)
    assert len(llm.calls) == 18 and len(res.turns) == 18
    assert all(c.temperature == 0.3 for c in llm.calls)
    assert len(res.leaders) == 3
    assert res.degraded is False
    assert res.final_leader_group in ("group_1", "group_2")
    assert res.snapshot_hash == ctx.snapshot_hash
    assert res.prompt_version == prompts.PROMPT_VERSION


def test_prompts_contain_only_persona_tagged_handles(ctx):
    llm = ScriptedLLM(default_responder)
    res = run_debate(ctx, llm)
    cited_prev: dict[int, set[str]] = {}
    for t in res.turns:
        cited_prev.setdefault(t.round_no, set()).update(
            h for c in t.output.claims for h in c.evidence_ids
        )
    for req in llm.calls:
        persona = Persona(req.tag["persona"])
        rnd = req.tag["round"]
        shown = set(handles_in(req.prompt))
        own = {i.handle for i in ctx.items_for(persona)}
        assert own, "fixture snapshot must have items for every persona"
        if rnd == 1:
            assert shown == own
        else:
            assert own <= shown <= own | cited_prev[rnd - 1]


def test_no_cross_group_answers_except_leader_hint(ctx):
    llm = ScriptedLLM(default_responder)
    res = run_debate(ctx, llm)
    for req in llm.calls:
        g, rnd = req.tag["group"], req.tag["round"]
        other = "group_2" if g == "group_1" else "group_1"
        # summaries are never part of the leader hint -> never cross groups
        assert f"SUMMARY-{other}-" not in req.prompt
        if rnd == 1:
            assert f"CLAIM-{other}-" not in req.prompt
        else:
            leader = res.leaders[rnd - 2].group
            if leader != other:
                assert f"CLAIM-{other}-" not in req.prompt
            # only the previous round's claims may appear
            for r in range(1, 4):
                if r != rnd - 1:
                    assert f"-r{r}" not in "".join(re.findall(r"CLAIM-\w+-\w+-r\d", req.prompt))


def test_no_amount_strings_or_tokens_in_prompts(ctx):
    llm = ScriptedLLM(default_responder)
    run_debate(ctx, llm)
    for req in llm.calls:
        blob = req.system + req.prompt
        for bad in ("500000", "5,00,000", "500,000", "100000", "1,00,000", "100,000", "TKN_", "Wedding", "wedding buffer"):
            assert bad not in blob, bad


def test_same_inputs_identical_prompt_hashes(ctx):
    r1 = run_debate(ctx, ScriptedLLM(default_responder))
    r2 = run_debate(ctx, ScriptedLLM(default_responder))
    assert [t.prompt_hash for t in r1.turns] == [t.prompt_hash for t in r2.turns]
    assert len({t.prompt_hash for t in r1.turns}) > 1


def test_round2_hint_contains_leader_consensus(ctx):
    llm = ScriptedLLM(default_responder)
    res = run_debate(ctx, llm)
    req = next(c for c in llm.calls if c.tag["round"] == 2)
    assert "LEADING GROUP'S CONSENSUS PLAN" in req.prompt
    assert f'"monthly_saving_rate": {res.leaders[0].consensus[res.leaders[0].group].monthly_saving_rate}' in req.prompt


# ---- failure modes ---------------------------------------------------------
def test_one_dead_agent_degrades(ctx):
    def responder(req):
        if req.tag["group"] == "group_1" and req.tag["persona"] == "liquidity_hawk":
            raise RuntimeError("provider down")
        return default_responder(req)

    res = run_debate(ctx, ScriptedLLM(responder))
    assert res.degraded is True
    failed = [t for t in res.turns if not t.valid]
    assert len(failed) == 3 and all(t.persona == Persona.LIQUIDITY_HAWK and t.group == "group_1" for t in failed)
    assert all("provider down" in t.error for t in failed)
    assert len(res.leaders) == 3


def test_all_agents_dead_raises(ctx):
    def responder(req):
        raise RuntimeError("down")

    with pytest.raises(DebateFailed):
        run_debate(ctx, ScriptedLLM(responder))


def test_repair_retry_path(ctx):
    def responder(req):
        t = req.tag
        if t["group"] == "group_2" and t["persona"] == "contrarian" and t["round"] == 1 and not t["repair"]:
            return "I refuse to produce JSON"
        if t["repair"]:
            assert "Validation error" in req.prompt and "no JSON object found" in req.prompt
        return default_responder(req)

    llm = ScriptedLLM(responder)
    res = run_debate(ctx, llm)
    assert len(llm.calls) == 19
    turn = next(t for t in res.turns if t.round_no == 1 and t.group == "group_2" and t.persona == Persona.CONTRARIAN)
    assert turn.repaired and turn.valid
    assert res.degraded is False


def test_permanent_parse_failure_excluded(ctx):
    def responder(req):
        t = req.tag
        if t["group"] == "group_2" and t["persona"] == "contrarian" and t["round"] == 2:
            return "{broken"
        return default_responder(req)

    llm = ScriptedLLM(responder)
    res = run_debate(ctx, llm)
    assert len(llm.calls) == 19  # exactly ONE repair retry
    turn = next(t for t in res.turns if t.round_no == 2 and t.group == "group_2" and t.persona == Persona.CONTRARIAN)
    assert not turn.valid and turn.repaired and "unparsable" in turn.error
    assert res.degraded is True


# ---- handles ---------------------------------------------------------------
def test_handles_stable_across_equal_hash_snapshots(db_session, hurdle_id):
    s1 = build_evidence_snapshot(db_session, hurdle_id, force_refresh=True)
    s2 = build_evidence_snapshot(db_session, hurdle_id, force_refresh=True)
    assert s1.id != s2.id and s1.snapshot_hash == s2.snapshot_hash
    c1 = build_debate_context(db_session, hurdle_id, s1)
    c2 = build_debate_context(db_session, hurdle_id, s2)
    assert c1.items == c2.items
    assert [i.handle for i in c1.items] == [f"E{n}" for n in range(1, len(c1.items) + 1)]
    for p in Persona:
        assert [i.handle for i in c1.items_for(p)] == [i.handle for i in c2.items_for(p)]


# ---- egress / leak ---------------------------------------------------------
def test_context_leak_raises_and_nothing_sent(db_session):
    d = date.today() + timedelta(days=100)
    h = create_hurdle(
        db_session,
        HurdleCreate(
            label="Acme Corp bonus",
            raw_description="x",
            primitives=[HorizonShift(goal_ref="Acme Corp bonus", new_date=d)],  # STRUCTURAL passthrough = leak channel
        ),
    )
    snap = build_evidence_snapshot(db_session, h.id)
    with pytest.raises(DebateInputLeak):
        build_debate_context(db_session, h.id, snap)


def test_context_case_is_banded(ctx):
    blob = ctx.case_json()
    assert "lakh" in blob and "500000" not in blob


# ---- persistence -----------------------------------------------------------
def test_persistence_round_trip(db_session, hurdle_id, snapshot, ctx):
    res = run_debate(ctx, ScriptedLLM(default_responder))
    run = save_debate_run(db_session, hurdle_id, snapshot, res, "scripted")
    db_session.expire_all()
    row = db_session.get(DebateRun, run.id)
    assert row.snapshot_hash == snapshot.snapshot_hash and row.llm_name == "scripted"
    assert row.prompt_version == prompts.PROMPT_VERSION
    assert len(row.turns) == 18
    back = load_debate_result(row)
    assert back.model_dump(mode="json") == res.model_dump(mode="json")


def _saved(db_session, hurdle_id, snapshot, ctx):
    res = run_debate(ctx, ScriptedLLM(default_responder))
    return save_debate_run(db_session, hurdle_id, snapshot, res, "scripted")


@pytest.mark.parametrize("model,attr", [(DebateRun, "llm_name"), (DebateTurn, "raw")])
def test_orm_update_delete_blocked(db_session, hurdle_id, snapshot, ctx, model, attr):
    _saved(db_session, hurdle_id, snapshot, ctx)
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
        "UPDATE debate_run SET llm_name = 'x'",
        "DELETE FROM debate_run",
        "UPDATE debate_turn SET raw = 'x'",
        "DELETE FROM debate_turn",
    ],
)
def test_db_trigger_blocks_raw_sql(db_session, db_engine, hurdle_id, snapshot, ctx, sql):
    _saved(db_session, hurdle_id, snapshot, ctx)
    with pytest.raises(Exception, match="append-only"):
        with db_engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
