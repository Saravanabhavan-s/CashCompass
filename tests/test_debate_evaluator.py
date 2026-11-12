"""Pure (no DB, no network) 3c tests: claim scoring, hallucination metrics,
EvidenceQualitySelector, report assembly."""
import json
from decimal import Decimal as D

import pytest

from app.debate.aggregate import aggregate_plans, plan_distance
from app.debate.context import DebateContext, EvidenceRef
from app.debate.engine import UniformSelector, run_debate
from app.debate.evaluator import (
    EvaluationReport,
    EvidenceQualitySelector,
    claim_redundancy,
    evaluate_debate,
    hallucination_metrics,
    score_agent,
    score_claim,
    score_group,
)
from app.debate.llm import ScriptedLLM
from app.debate.schemas import AgentOutput, AgentTurn, Claim, DebateConfig, DebateResult, LeaderDecision, Rebuttal
from app.evidence.personas import Persona
from tests.debate_helpers import fact, inference, plan, reply

ALL = frozenset(p.value for p in Persona)


def _ref(n, source, content):
    return EvidenceRef(handle=f"E{n}", source=source, query=f"q{n}", lens_tags=ALL, content=content, citation=f"c{n}")


REFS = (
    _ref(1, "FRED", "CPI rose 3.5 percent in 2024 and the index level was 1,234.5 at year end."),
    _ref(2, "ALPHA_VANTAGE", "SPY monthly close was 520.10 with   volume 7 million."),
    _ref(3, "LOCAL_REGULATORY", "DICGC insures deposits up to Rs 5 lakh per depositor per bank."),
)
CASE_REF = EvidenceRef(
    handle="C1", source="CASE", query="case", lens_tags=ALL, citation="case",
    content='{"hurdle": [{"goal": "₹5–6 lakh", "months_from_now": "6–12 months"}]}',
)
HM = {r.handle: r for r in REFS}
HM["C1"] = CASE_REF  # the banded user case, citable like evidence (not part of ctx.items)


def C(kind, ids=(), figs=(), text="t"):
    return Claim(kind=kind, text=text, evidence_ids=list(ids), quoted_figures=list(figs))


# ---- claim scoring: one per flag reason + clean + inference ----------------
def test_clean_fact_scores_source_weight():
    s = score_claim(C("FACT", ["E1"], ["3.5"]), HM)
    assert (s.flagged, s.reasons, s.score, s.figure_grounding_ratio, s.mean_source_weight) == (False, [], 1.0, 1.0, 1.0)
    assert score_claim(C("FACT", ["E2"], ["520.10"]), HM).score == 0.8
    assert score_claim(C("FACT", ["E3"]), HM).score == 1.0  # no figures -> ratio 1


def test_mixed_source_average_exact():
    assert score_claim(C("FACT", ["E1", "E2"]), HM).score == 0.9  # (1.0 + 0.8) / 2
    assert score_claim(C("FACT", ["E2", "E3", "E1"]), HM).score == 0.933333  # 2.8 / 3
    assert score_claim(C("FACT", ["E2", "E2"]), HM).score == 0.8  # repeated handle counted once


def test_figure_normalisation_commas_and_whitespace():
    assert not score_claim(C("FACT", ["E1"], ["1234.5"]), HM).flagged  # content has 1,234.5
    assert not score_claim(C("FACT", ["E2"], ["with volume 7"]), HM).flagged  # extra spaces in content
    assert not score_claim(C("FACT", ["E3"], ["Rs 5 lakh"]), HM).flagged


def test_flag_no_citation():
    s = score_claim(C("FACT"), HM)
    assert s.flagged and s.reasons == ["no_citation"] and s.score == 0.0


def test_flag_unknown_handle():
    s = score_claim(C("FACT", ["E99"]), HM)
    assert s.flagged and s.reasons == ["unknown_handle"] and s.score == 0.0
    s = score_claim(C("FACT", ["E1", "E99"]), HM)
    assert s.flagged and s.reasons == ["unknown_handle"] and s.score == 0.0


def test_flag_ungrounded_figure_and_ratio():
    s = score_claim(C("FACT", ["E1"], ["3.5", "9.9"]), HM)
    assert s.flagged and s.reasons == ["ungrounded_figure"] and s.score == 0.0
    assert s.figure_grounding_ratio == 0.5
    assert score_claim(C("FACT", ["E1"], ["3.5", "9.9", "7.7"]), HM).figure_grounding_ratio == 0.333333
    # figure exists, but only in an item that was NOT cited
    assert score_claim(C("FACT", ["E1"], ["520.10"]), HM).flagged
    # grounded in ANY cited item is enough
    assert not score_claim(C("FACT", ["E1", "E2"], ["520.10"]), HM).flagged


# ---- C1: the banded case as citable evidence --------------------------------
def test_case_figure_present_and_citing_c1_is_grounded():
    s = score_claim(C("FACT", ["C1"], ["5-6 lakh"]), HM)  # hyphen typed; the case has an en dash
    assert (s.flagged, s.reasons, s.score, s.mean_source_weight) == (False, [], 1.0, 1.0)
    assert not score_claim(C("FACT", ["C1"], ["₹5–6 lakh", "6-12 months"]), HM).flagged


def test_case_figure_absent_is_ungrounded():
    s = score_claim(C("FACT", ["C1"], ["9-10 lakh"]), HM)
    assert s.flagged and s.reasons == ["ungrounded_figure"] and s.score == 0.0


def test_c2_is_unknown_handle():
    s = score_claim(C("FACT", ["C2"]), HM)
    assert s.flagged and s.reasons == ["unknown_handle"] and s.score == 0.0
    assert score_claim(C("FACT", ["C1", "C2"]), HM).reasons == ["unknown_handle"]


def test_claim_citing_c1_and_e3_is_checked_against_both():
    ok = score_claim(C("FACT", ["C1", "E3"], ["5-6 lakh", "Rs 5 lakh"]), HM)  # one figure per item
    assert not ok.flagged and ok.figure_grounding_ratio == 1.0 and ok.score == 1.0
    bad = score_claim(C("FACT", ["C1", "E3"], ["5-6 lakh", "nowhere"]), HM)
    assert bad.flagged and bad.reasons == ["ungrounded_figure"] and bad.figure_grounding_ratio == 0.5
    assert score_claim(C("FACT", ["C1", "E2"], ["5-6 lakh"]), HM).score == 0.9  # (1.0 case + 0.8 AV) / 2
    assert score_claim(C("FACT", ["E3"], ["5-6 lakh"]), HM).flagged  # not cited -> not checked against C1


def test_multiple_reasons_all_recorded():
    s = score_claim(C("FACT", ["E99"], ["3.5"]), HM)
    assert s.reasons == ["unknown_handle", "ungrounded_figure"]


def test_inference_fixed_weight_never_flagged():
    for c in (C("INFERENCE"), C("INFERENCE", ["E99"], ["nonsense"])):
        s = score_claim(c, HM)
        assert (s.score, s.flagged, s.reasons) == (0.3, False, [])


def _out(claims, p=None):
    return AgentOutput(plan=p or plan(), claims=claims, summary="s")


def test_agent_and_group_score_arithmetic():
    a1 = _out([C("FACT", ["E1"], ["3.5"]), C("INFERENCE")])  # (1.0 + 0.3) / 2 = 0.65
    a2 = _out([C("FACT", ["E2"], ["520.10"]), C("INFERENCE")])  # (0.8 + 0.3) / 2 = 0.55
    a3 = _out([C("FACT"), C("INFERENCE"), C("FACT", ["E1"])])  # (0 + 0.3 + 1.0) / 3
    assert score_agent(a1, HM) == 0.65 and score_agent(a2, HM) == 0.55
    assert score_agent(a3, HM) == 0.433333
    turns = [AgentTurn(group="g", persona=Persona.SAVER, round_no=1, prompt_hash="h", output=o) for o in (a1, a2)]
    turns.append(AgentTurn(group="g", persona=Persona.SKEPTIC, round_no=1, prompt_hash="h", error="dead"))
    assert score_group(turns, HM) == 0.6  # dead agent excluded
    assert score_group([], HM) == 0.0


# ---- selector --------------------------------------------------------------
def _turn(group, persona, claims, p=None, rnd=1, rebuttals=()):
    return AgentTurn(
        group=group,
        persona=persona,
        round_no=rnd,
        prompt_hash="h",
        output=AgentOutput(plan=p or plan(), claims=claims, rebuttals=list(rebuttals), summary="s"),
    )


def _ctx():
    return DebateContext(case={}, snapshot_id="s", snapshot_hash="h", items=REFS, handle_map=HM)


GOOD = [C("FACT", ["E1"], ["3.5"])]
BAD = [C("FACT")]
PERS = [Persona.SKEPTIC, Persona.SAVER, Persona.LIQUIDITY_HAWK]


def test_selector_prefers_grounded_group_over_larger_hallucinating_group():
    out = {
        "g_good": [_turn("g_good", p, GOOD) for p in PERS[:2]],
        "g_bad": [_turn("g_bad", p, BAD) for p in PERS],  # more agents, all hallucinating
    }
    d = EvidenceQualitySelector().select(1, out, _ctx())
    assert d.group == "g_good" and d.scores == {"g_good": 1.0, "g_bad": 0.0}
    assert UniformSelector().select(1, out, _ctx()).group == "g_bad"  # contrast: headcount picks the wrong one


def test_evidence_weighted_consensus_differs_from_uniform():
    p_good, p_bad = plan(100, 0, 0, 0, rate=10), plan(0, 0, 0, 100, rate=90)
    out = {"g": [_turn("g", PERS[0], GOOD, p_good), _turn("g", PERS[1], BAD, p_bad), _turn("g", PERS[2], BAD, p_bad)]}
    weighted = EvidenceQualitySelector().select(1, out, _ctx()).consensus["g"]
    uniform = UniformSelector().select(1, out, _ctx()).consensus["g"]
    assert weighted == p_good  # only the grounded agent carries weight
    assert uniform != weighted and uniform == aggregate_plans([p_good, p_bad, p_bad])


def test_all_zero_scores_fall_back_to_uniform_consensus():
    p1, p2 = plan(100, 0, 0, 0), plan(0, 0, 0, 100)
    out = {"g": [_turn("g", PERS[0], BAD, p1), _turn("g", PERS[1], BAD, p2)]}
    assert EvidenceQualitySelector().select(1, out, _ctx()).consensus["g"] == aggregate_plans([p1, p2])


def test_tie_break_two_groups_is_configured_order():
    plans = {"a": plan(100, 0, 0, 0), "b": plan(0, 0, 0, 100)}

    def mk(order):
        return {g: [_turn(g, PERS[0], GOOD, plans[g])] for g in order}

    sel = EvidenceQualitySelector()
    assert sel.select(1, mk(["a", "b"]), _ctx()).group == "a"
    assert sel.select(1, mk(["b", "a"]), _ctx()).group == "b"
    assert sel.select(1, mk(["a", "b"]), _ctx()) == sel.select(1, mk(["a", "b"]), _ctx())


def test_tie_break_three_groups_prefers_central_consensus():
    plans = {"a": plan(100, 0, 0, 0), "c": plan(0, 0, 0, 100), "b": plan(50, 0, 0, 50)}
    out = {g: [_turn(g, PERS[0], GOOD, plans[g])] for g in ["a", "c", "b"]}  # b is LAST in config order
    assert plan_distance(plans["a"], plans["b"]) < plan_distance(plans["a"], plans["c"])
    assert EvidenceQualitySelector().select(1, out, _ctx()).group == "b"


def test_selector_skips_groups_without_valid_agents():
    d = EvidenceQualitySelector().select(1, {"d": [], "g": [_turn("g", PERS[0], GOOD)]}, _ctx())
    assert d.group == "g" and set(d.consensus) == {"g"}


# ---- hallucination metrics on a small scripted debate ----------------------
def _responder(req):
    t = req.tag
    g, p, r = t["group"], t["persona"], t["round"]
    if (g, p) == ("group_1", "skeptic"):
        claims = [fact(["E1"], ["3.5"]), fact([])]  # 1 of 2 flagged, every round
    elif (g, p) == ("group_1", "saver"):
        claims = [fact(["E3"]), inference()]
    elif (g, p) == ("group_1", "liquidity_hawk"):
        claims = [fact(["E99"])] if r == 1 else [fact(["E1"])]  # hallucinates in round 1 only
    elif (g, p) == ("group_2", "skeptic"):
        claims = [fact(["E1"], ["3.5"])]
    elif (g, p) == ("group_2", "opportunity_seeker"):
        claims = [fact(["E2"], ["999"])]  # ungrounded figure every round
    else:  # group_2 contrarian: a grounded statement about the user's own case + inference
        claims = [fact(["C1"], ["5–6 lakh"]), inference()]
    return reply(plan(), claims)


@pytest.fixture(scope="module")
def scripted_result():
    ctx = _ctx()
    return ctx, run_debate(ctx, ScriptedLLM(_responder), selector=EvidenceQualitySelector())


def test_hallucination_rate_arithmetic(scripted_result):
    ctx, res = scripted_result
    m = hallucination_metrics(res.turns, ctx.handle_map)
    # round 1: facts 2+1+1+1+1+1 = 7, flagged 1+0+1+0+1+0 = 3 ; rounds 2,3: 7 facts, 2 flagged
    assert (m.overall.flagged, m.overall.total, m.overall.rate) == (7, 21, 0.333333)
    assert (m.by_round["1"].flagged, m.by_round["1"].total, m.by_round["1"].rate) == (3, 7, 0.428571)
    for r in ("2", "3"):
        assert (m.by_round[r].flagged, m.by_round[r].total, m.by_round[r].rate) == (2, 7, 0.285714)
    bp = {k: (v.flagged, v.total, v.rate) for k, v in m.by_persona.items()}
    assert bp == {
        "skeptic": (3, 9, 0.333333),  # appears in both groups
        "saver": (0, 3, 0.0),
        "liquidity_hawk": (1, 3, 0.333333),
        "opportunity_seeker": (3, 3, 1.0),
        "contrarian": (0, 3, 0.0),  # C1-grounded FACT every round
    }


def test_hallucination_rate_zero_without_fact_claims():
    m = hallucination_metrics([_turn("g", Persona.SAVER, [inference()])], HM)
    assert (m.overall.total, m.overall.flagged, m.overall.rate) == (0, 0, 0.0)


# ---- evaluate_debate on the scripted debate --------------------------------
def test_report_shape_and_roundtrip(scripted_result):
    ctx, res = scripted_result
    rep = evaluate_debate(res, ctx)
    assert rep.final_round == 3 and rep.snapshot_hash == "h"
    assert rep.agreement == 1.0  # every agent proposes the same plan
    assert len(rep.agent_scores) == 18
    # FACT claims by citation: 15 cite E-handles (incl. the unknown E99), 3 cite C1, 3 cite nothing
    assert rep.citations.model_dump() == {"facts_total": 21, "citing_case": 3, "citing_evidence": 15, "citing_both": 0, "uncited": 3}
    assert set(rep.group_scores) == {"group_1", "group_2"}
    final_flagged = {(f.group, f.persona): f.reasons for f in rep.flagged_claims if f.round_no == 3}
    assert final_flagged == {("group_1", "skeptic"): ["no_citation"], ("group_2", "opportunity_seeker"): ["ungrounded_figure"]}
    # every agent proposes plan() -> ONE deduped candidate carrying every origin
    assert len(rep.candidates) == 1
    assert {"group_1:consensus", "group_2:consensus", "group_2:contrarian"} <= set(rep.candidates[0].origins)
    assert rep.candidates[0].support == max(list(rep.group_scores.values()) + [a.score for a in rep.agent_scores if a.round_no == 3])
    dumped = rep.model_dump(mode="json")
    json.dumps(dumped)
    assert EvaluationReport.model_validate(dumped) == rep


def _manual_result(turns, consensus):
    return DebateResult(
        config=DebateConfig(),
        prompt_version="v",
        snapshot_hash="h",
        turns=turns,
        leaders=[LeaderDecision(group="group_1", scores={}, consensus=consensus)],
        final_leader_group="group_1",
        final_consensus=consensus,
    )


S, SAV, LH, OPP, CON = Persona.SKEPTIC, Persona.SAVER, Persona.LIQUIDITY_HAWK, Persona.OPPORTUNITY_SEEKER, Persona.CONTRARIAN


def test_candidates_dedupe_and_support():
    pa, pb, pc = plan(100, 0, 0, 0), plan(0, 0, 0, 100), plan(50, 50, 0, 0)
    turns = [
        _turn("group_1", S, GOOD, pa, 1), _turn("group_1", SAV, GOOD, pa, 1), _turn("group_1", LH, GOOD, pa, 1),
        _turn("group_2", S, [C("FACT", ["E2"])], pb, 1), _turn("group_2", OPP, [C("FACT", ["E2"])], pb, 1),
        _turn("group_2", CON, [inference()], pc, 1),
    ]
    rep = evaluate_debate(_manual_result(turns, {"group_1": pa, "group_2": pb}), _ctx())
    by_key = {c.plan.key(): c for c in rep.candidates}
    assert set(by_key) == {pa.key(), pb.key(), pc.key()}  # 3 unique plans from 8 proposals
    assert by_key[pa.key()].support == 1.0 and by_key[pb.key()].support == 0.8 and by_key[pc.key()].support == 0.3
    assert [c.support for c in rep.candidates] == [1.0, 0.8, 0.3]
    assert rep.group_scores == {"group_1": 1.0, "group_2": 0.633333}  # (0.8 + 0.8 + 0.3) / 3
    assert rep.agreement == 0.75  # distance(pa, pb) = (200/200) / 4


def test_unresolved_dissent_rule():
    p1, p3 = plan(25, 25, 25, 25), plan(10, 10, 10, 70)

    def rb(t):
        return [Rebuttal(target=t, point=f"against {t.value}")]

    turns = [
        # round 1
        _turn("group_1", S, GOOD, p1, 1), _turn("group_1", SAV, GOOD, p1, 1), _turn("group_1", LH, GOOD, p1, 1),
        _turn("group_2", S, GOOD, p1, 1), _turn("group_2", OPP, GOOD, p1, 1), _turn("group_2", CON, GOOD, p1, 1),
        # round 2: only the opportunity seeker moves
        _turn("group_1", S, GOOD, p1, 2, rb(CON)),  # contrarian (other group) did not move -> unresolved
        _turn("group_1", SAV, GOOD, p1, 2, rb(S)),  # not a skeptic/contrarian rebuttal -> ignored
        _turn("group_1", LH, GOOD, p1, 2),
        _turn("group_2", S, GOOD, p1, 2, rb(OPP)),  # OPP moved -> resolved
        _turn("group_2", OPP, GOOD, p3, 2),
        _turn("group_2", CON, GOOD, p1, 2, rb(SAV)),  # saver did not move -> unresolved
    ]
    rep = evaluate_debate(_manual_result(turns, {"group_1": p1, "group_2": p1}), _ctx())
    assert [(d.group, d.from_persona, d.target) for d in rep.unresolved_dissent] == [
        ("group_1", "skeptic", "contrarian"),
        ("group_2", "contrarian", "saver"),
    ]


# ---- diversity --------------------------------------------------------------
def _div_turns(shared_by: int, rnd: int = 1):
    """4 agents x 2 claims; the first claim is the same text (differently cased/spaced) for `shared_by` agents."""
    texts = ["Shared  Claim", "shared claim", "SHARED CLAIM", "SHARED	claim"]
    pers = [S, SAV, LH, OPP]
    turns = []
    for i, p in enumerate(pers):
        first = texts[i] if i < shared_by else f"unique opener {i}"
        turns.append(_turn("group_1" if i < 2 else "group_2", p, [C("INFERENCE", text=first), C("INFERENCE", text=f"own point {i}")], rnd=rnd))
    return turns


def test_claim_redundancy_counts_cross_agent_copies_after_normalisation():
    assert claim_redundancy(_div_turns(4)) == D("0.5")  # 4 of 8 claims are copies
    assert claim_redundancy(_div_turns(3)) == D("0.375")  # 3 of 8
    assert claim_redundancy(_div_turns(1)) == D(0)  # a lone claim is not a copy
    assert claim_redundancy([]) == D(0)


def test_repeats_inside_one_agent_are_not_redundancy():
    t = _turn("g", S, [C("INFERENCE", text="same"), C("INFERENCE", text="Same")])
    assert claim_redundancy([t]) == D(0)


def test_low_diversity_flag_at_the_half_boundary():
    def report(shared_by):
        turns = _div_turns(shared_by)
        return evaluate_debate(_manual_result(turns, {"group_1": plan(), "group_2": plan()}), _ctx())

    at, below = report(4), report(3)
    assert at.claim_redundancy == 0.5 and at.low_diversity is True  # >= 50% triggers
    assert below.claim_redundancy == 0.375 and below.low_diversity is False


def test_only_the_final_round_counts_for_diversity():
    final = _div_turns(1, rnd=2)  # diverse
    earlier = _div_turns(4, rnd=1)  # fully copied, but not the final round
    rep = evaluate_debate(_manual_result(earlier + final, {"group_1": plan(), "group_2": plan()}), _ctx())
    assert rep.final_round == 2 and rep.low_diversity is False
