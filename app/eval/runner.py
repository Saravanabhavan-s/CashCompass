"""Evaluation runner: debate ON vs debate OFF on the SAME snapshot.

  Debate ON  = the full pipeline (6 agents x 3 rounds, evidence-quality leader,
               evaluator, optimizer, guardrails, confidence).
  Debate OFF = ONE generalist agent that sees ALL snapshot items, same Plan /
               claims schema, same evaluator, same optimizer, same guardrails.
               Its "agreement" term in confidence is 1.0 (a lone agent cannot
               disagree with itself), which is generous to the OFF arm.

Both arms share one DebateContext per query, so they provably read identical
evidence and handles (their snapshot_hash is recorded and asserted equal).

Refuses to run without a real LLM unless dry_run (ScriptedLLM, plumbing only;
the output is stamped "DRY RUN - not results"). Eval queries create hurdle rows
labelled "eval:<id>" in the connected database.
"""
from __future__ import annotations

import re
import threading
from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.debate import prompts
from app.debate.context import DebateContext, _magnitudes, build_debate_context
from app.debate.engine import parse_agent_output
from app.debate.evaluator import classify_flag, score_agent, score_claim
from app.debate.llm import LLMClient, LLMRequest, LLMResponse, ScriptedLLM, get_llm_client
from app.debate.schemas import AgentOutput, ClaimKind, DebateConfig
from app.eval.dataset import EvalQuery
from app.eval.metrics import ArmResult, ArmSummary, QueryResult, check_labels, summarize, to_markdown
from app.exceptions import DebateInputLeak
from app.guardrails import compute_confidence, run_guardrails
from app.optimizer.assumptions import Assumptions
from app.optimizer.projection import opening_pool
from app.optimizer.search import assess_feasibility, optimize
from app.schemas.hurdle import HurdleCreate
from app.schemas.state import FinancialState
from app.services.evidence import build_evidence_snapshot
from app.services.hurdles import create_hurdle
from app.services.recommendation import _GuardedLLM, run_pipeline

DRY_RUN_STAMP = "DRY RUN - not results"
GENERALIST_BRIEF = (
    "You are a single generalist planner. Weigh safety, liquidity, return and source "
    "reliability yourself; you see ALL evidence items and there is no debate."
)
_HANDLE = re.compile(r"^\[(E\d+)\] source=", re.MULTILINE)


class EvalRun(BaseModel):
    stamp: str | None
    dry_run: bool
    llm: str
    prompt_version: str
    n_queries: int
    queries: list[QueryResult]
    summary: dict[str, ArmSummary]
    markdown: str


# ---- dry-run LLM ------------------------------------------------------------
def dry_run_responder(req: LLMRequest) -> str:
    """Plumbing-only replies: cite the first evidence handle shown, equity 0."""
    import json

    hs = _HANDLE.findall(req.prompt)
    persona = req.tag.get("persona", "generalist")
    alloc = {
        "saver": (20, 30, 50, 0),
        "liquidity_hawk": (30, 70, 0, 0),
        "skeptic": (30, 50, 20, 0),
        "opportunity_seeker": (10, 60, 30, 0),
        "contrarian": (40, 60, 0, 0),
    }.get(persona, (30, 60, 10, 0))
    claims = [{"kind": "INFERENCE", "text": f"dry-run reasoning by {persona}", "evidence_ids": [], "quoted_figures": []}]
    if hs:
        claims.insert(0, {"kind": "FACT", "text": "dry-run cited fact", "evidence_ids": hs[:1], "quoted_figures": []})
    return json.dumps(
        {
            "plan": {
                "allocation": dict(zip(["CASH_SAVINGS", "LIQUID_FUND", "FIXED_DEPOSIT", "EQUITY_INDEX"], alloc)),
                "monthly_saving_rate": 50,
                "deadline_shift_months": 0,
                "emergency_buffer_months": 3,
            },
            "claims": claims,
            "rebuttals": [],
            "summary": "dry run",
        }
    )


class _Counting(LLMClient):
    def __init__(self, inner: LLMClient):
        self._inner, self.calls, self._lock = inner, 0, threading.Lock()

    @property
    def name(self) -> str:  # type: ignore[override]
        return getattr(self._inner, "name", "llm")

    def complete(self, req: LLMRequest) -> LLMResponse:
        resp = self._inner.complete(req)
        with self._lock:
            self.calls += 1
        return resp


# ---- arms -------------------------------------------------------------------
def _terminal_arm(arm: str, status: str, snapshot_hash: str, q: EvalQuery, error: str | None = None, codes=None, calls: int = 0) -> ArmResult:
    return ArmResult(
        arm=arm,
        status=status,
        error=error,
        snapshot_hash=snapshot_hash,
        llm_calls=calls,
        guardrail_codes=codes or [],
        confidence=Decimal(0) if status in ("INFEASIBLE", "BLOCKED") else None,
        confidence_band="LOW" if status in ("INFEASIBLE", "BLOCKED") else None,
        label_checks=check_labels(q.expectations, None, False) if status != "FAILED" else {},
    )


def _arm_from_outcome(arm: str, o, q: EvalQuery, calls: int) -> ArmResult:
    opt, rep = o.optimizer, o.report
    final_feasible = bool(opt and opt.chosen is not None)
    return ArmResult(
        arm=arm,
        status="OK" if o.outcome == "RECOMMENDED" else o.outcome,
        snapshot_hash=o.snapshot.snapshot_hash,
        llm_calls=calls,
        facts_total=rep.hallucination.overall.total if rep else 0,
        facts_flagged=rep.hallucination.overall.flagged if rep else 0,
        proposals=len(rep.candidates) if rep else 0,
        proposals_policy_violating=len(opt.rejected_policy) if opt else 0,
        plan_feasible=bool(opt and opt.status == "FEASIBLE"),
        final_feasible=final_feasible,
        chosen_plan=o.chosen,
        confidence=o.confidence.value,
        confidence_band=o.confidence.band,
        guardrail_codes=o.guardrails.codes(),
        label_checks=check_labels(q.expectations, o.chosen, final_feasible),
    )


def _generalist_prompt(ctx: DebateContext) -> tuple[str, str]:
    system = prompts.SYSTEM_TEMPLATE.format(persona_name="generalist", brief=GENERALIST_BRIEF)
    user = prompts.ROUND1_TEMPLATE.format(
        round_no=1,
        rounds=1,
        case=ctx.case_json(),
        evidence=prompts._render_pack(list(ctx.items)),  # ALL snapshot items
        output_spec=prompts.OUTPUT_SPEC,
    )
    return system, user


def _run_off(q: EvalQuery, ctx: DebateContext, state: FinancialState, snapshot, hurdle, llm: LLMClient, assumptions: Assumptions, as_of: date) -> ArmResult:
    counting = _Counting(llm)
    forbidden = _magnitudes(state.model_dump(mode="python")) + _magnitudes(hurdle.primitives)
    guarded = _GuardedLLM(counting, forbidden)
    cfg = DebateConfig()
    system, user = _generalist_prompt(ctx)

    def ask(p: str, repair: bool) -> str:
        return guarded.complete(
            LLMRequest(system=system, prompt=p, temperature=cfg.temperature, max_tokens=cfg.max_tokens,
                       tag={"group": "single", "persona": "generalist", "round": 1, "repair": repair})
        ).text

    out: AgentOutput | None = None
    try:
        raw = ask(user, False)
        try:
            out = parse_agent_output(raw)
        except ValueError as first:
            raw2 = ask(f"{user}\n\nYOUR PREVIOUS REPLY:\n{raw[:3000]}\n\n{prompts.render_repair(str(first))}", True)
            out = parse_agent_output(raw2)
    except DebateInputLeak as e:
        return _terminal_arm("debate_off", "BLOCKED", snapshot.snapshot_hash, q, codes=["LEAK"], calls=counting.calls, error=str(e))
    except Exception as e:  # noqa: BLE001 - any provider/parse failure fails this arm for this query
        return _terminal_arm("debate_off", "FAILED", snapshot.snapshot_hash, q, error=f"{type(e).__name__}: {e}"[:300], calls=counting.calls)

    hm = ctx.handle_map
    facts = [c for c in out.claims if c.kind == ClaimKind.FACT]
    kinds = [classify_flag(score_claim(c, hm).reasons) for c in facts]
    flagged = sum(k is not None for k in kinds)
    score = score_agent(out, hm)
    living = q.monthly_living_expense
    opt = optimize(state, hurdle.primitives, [(out.plan, score)], assumptions, living, as_of)
    base = dict(
        arm="debate_off",
        snapshot_hash=snapshot.snapshot_hash,
        llm_calls=counting.calls,
        facts_total=len(facts),
        facts_flagged=flagged,
        proposals=1,
        proposals_policy_violating=len(opt.rejected_policy),
    )
    if opt.status == "INFEASIBLE":
        return ArmResult(status="INFEASIBLE", confidence=Decimal(0), confidence_band="LOW",
                         guardrail_codes=["INFEASIBLE"], label_checks=check_labels(q.expectations, None, False), **base)

    ev = opt.chosen_eval
    texts = [i.content for i in ctx.items] + [out.summary] + [c.text for c in out.claims]
    g = run_guardrails(
        policy_violations=ev.policy_violations,
        texts=texts,
        grounded_facts=len(facts) - flagged,
        hallucination_rate=(flagged / len(facts)) if facts else 0.0,
        fabrication_rate=(kinds.count("fabricated") / len(facts)) if facts else 0.0,
        misattribution_rate=(kinds.count("misattributed") / len(facts)) if facts else 0.0,
        living_expense_missing=living is None,
        pool_empty=opening_pool(state) == 0,
        repaired=opt.status == "REPAIRED",
    )
    conf = compute_confidence(ev.scenarios_feasible, 1.0, score, living is not None and opening_pool(state) != 0, has_warn=g.needs_ack)
    return ArmResult(
        status="BLOCKED" if g.blocked else "OK",
        plan_feasible=opt.status == "FEASIBLE",
        final_feasible=True,
        chosen_plan=opt.chosen,
        confidence=conf.value,
        confidence_band=conf.band,
        guardrail_codes=g.codes(),
        label_checks=check_labels(q.expectations, opt.chosen, True),
        **base,
    )


def run_query(db: Session, q: EvalQuery, llm: LLMClient, assumptions: Assumptions, as_of: date) -> QueryResult:
    hurdle = create_hurdle(db, HurdleCreate(label=f"eval:{q.id}", raw_description=q.description or q.id, primitives=q.primitives))
    snapshot = build_evidence_snapshot(db, hurdle.id)
    h = snapshot.snapshot_hash
    state = q.state if q.state is not None else FinancialState()
    living = q.monthly_living_expense

    if assess_feasibility(state, hurdle.primitives, assumptions, living, as_of).status == "INFEASIBLE":
        a = _terminal_arm("debate_on", "INFEASIBLE", h, q, codes=["INFEASIBLE"])
        return QueryResult(id=q.id, description=q.description, snapshot_hash=h, debate_on=a, debate_off=a.model_copy(update={"arm": "debate_off"}))

    try:
        ctx = build_debate_context(db, hurdle.id, snapshot, as_of=as_of, state=state)
    except DebateInputLeak as e:
        a = _terminal_arm("debate_on", "BLOCKED", h, q, codes=["LEAK"], error=str(e))
        return QueryResult(id=q.id, description=q.description, snapshot_hash=h, debate_on=a, debate_off=a.model_copy(update={"arm": "debate_off"}))

    on_llm = _Counting(llm)
    try:
        o = run_pipeline(db, hurdle.id, on_llm, assumptions=assumptions, monthly_living_expense=living, as_of=as_of,
                         state=state, snapshot=snapshot, persist_debate=False, ctx=ctx)
        on = _arm_from_outcome("debate_on", o, q, on_llm.calls)
    except Exception as e:  # noqa: BLE001
        on = _terminal_arm("debate_on", "FAILED", h, q, error=f"{type(e).__name__}: {e}"[:300], calls=on_llm.calls)

    off = _run_off(q, ctx, state, snapshot, hurdle, llm, assumptions, as_of)
    return QueryResult(id=q.id, description=q.description, snapshot_hash=h, debate_on=on, debate_off=off)


def run_eval(
    db: Session,
    queries: list[EvalQuery],
    llm: LLMClient | None = None,
    *,
    dry_run: bool = False,
    assumptions: Assumptions | None = None,
    as_of: date | None = None,
) -> EvalRun:
    if dry_run:
        llm = ScriptedLLM(dry_run_responder)
    elif llm is None:
        llm = get_llm_client()  # raises LLMNotConfigured: results need a real model
    assumptions = assumptions or Assumptions()
    as_of = as_of or date.today()
    results = [run_query(db, q, llm, assumptions, as_of) for q in queries]
    for r in results:
        assert r.debate_on.snapshot_hash == r.debate_off.snapshot_hash == r.snapshot_hash, "arms saw different evidence"
    summary = summarize(results)
    stamp = DRY_RUN_STAMP if dry_run else None
    return EvalRun(
        stamp=stamp,
        dry_run=dry_run,
        llm=getattr(llm, "name", "llm"),
        prompt_version=prompts.PROMPT_VERSION,
        n_queries=len(results),
        queries=results,
        summary=summary,
        markdown=to_markdown(summary, stamp, len(results)),
    )
