"""Layer 3e orchestrator: evidence -> feasibility -> debate -> evaluator ->
optimizer -> guardrails -> confidence -> immutable recommendation row, plus the
human-confirm flow.

Privacy: debate agents only ever see DEBATE_AGENT-projected data. Exact figures
are computed here by the deterministic optimizer and returned only in the
recommendation (the user's own view). Every outgoing model request is also
scanned for exact amounts / vault tokens by `_GuardedLLM`.

Fail closed: a leak -> BLOCKED recommendation; an infeasible hurdle -> INFEASIBLE
recommendation with ZERO LLM calls; an unusable debate -> DebateFailed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.debate.context import DebateContext, _magnitudes, build_debate_context
from app.debate.engine import run_debate, save_debate_run
from app.debate.evaluator import EvaluationReport, EvidenceQualitySelector, evaluate_debate, score_claim
from app.debate.llm import LLMClient, LLMRequest, LLMResponse
from app.debate.schemas import AgentTurn, ClaimKind, DebateResult, Plan
from app.enums import LedgerEntryType, LedgerSource
from app.exceptions import (
    DebateInputLeak,
    HurdleNotFound,
    RecommendationConflict,
    RecommendationNotFound,
)
from app.guardrails import DISCLAIMER, Confidence, GuardrailReport, compute_confidence, run_guardrails
from app.models.hurdle import Hurdle
from app.models.recommendation import Recommendation, RecommendationDecision
from app.optimizer.assumptions import Assumptions
from app.optimizer.projection import build_schedule, opening_pool, parse_primitives, q
from app.optimizer.search import FeasibilityAssessment, Gap, OptimizerResult, assess_feasibility, optimize
from app.schemas.ledger import LedgerEntryCreate
from app.schemas.state import FinancialState
from app.services.evidence import run_evidence_pipeline
from app.services.ledger import append_entry
from app.services.state import rebuild_state_from_ledger
from app.vault.scanner import TOKEN_PREFIX, _amount_renderings

LEAK_MIN_AMOUNT = Decimal(10000)  # smaller figures are too common in public text to match exactly


# ---- outgoing-request guard -------------------------------------------------
class _GuardedLLM(LLMClient):
    """Last line of defence: scan every outgoing request for exact amounts or
    vault tokens. A hit raises DebateInputLeak BEFORE anything is sent."""

    def __init__(self, inner: LLMClient, forbidden_values: list[Decimal]):
        self._inner = inner
        renderings: set[str] = set()
        for v in forbidden_values:
            if abs(v) >= LEAK_MIN_AMOUNT:
                renderings |= _amount_renderings(v)
        self._pattern = (
            re.compile(r"(?<![\d.,])(?:" + "|".join(re.escape(r) for r in sorted(renderings, key=len, reverse=True)) + r")(?!\d)")
            if renderings
            else None
        )

    @property
    def name(self) -> str:  # type: ignore[override]
        n = getattr(self._inner, "name", "llm")
        m = getattr(self._inner, "model", "")
        return f"{n}:{m}" if m else n

    def complete(self, req: LLMRequest) -> LLMResponse:
        blob = req.system + "\n" + req.prompt
        if TOKEN_PREFIX in blob:
            raise DebateInputLeak("vault token found in outgoing model request")
        if self._pattern is not None and self._pattern.search(blob):
            raise DebateInputLeak("exact amount found in outgoing model request")
        return self._inner.complete(req)


# ---- pipeline ---------------------------------------------------------------
@dataclass
class Outcome:
    outcome: str  # RECOMMENDED | INFEASIBLE | BLOCKED
    hurdle: Hurdle
    snapshot: Any
    state: FinancialState
    as_of: date
    assessment: FeasibilityAssessment
    guardrails: GuardrailReport
    confidence: Confidence
    ctx: DebateContext | None = None
    debate: DebateResult | None = None
    report: EvaluationReport | None = None
    optimizer: OptimizerResult | None = None
    chosen: Plan | None = None
    gap: Gap | None = None
    backing: list[AgentTurn] | None = None
    citations: list[dict] | None = None
    debate_run_id: str | None = None
    llm_name: str = ""


def _backing_agents(report: EvaluationReport, result: DebateResult, plan: Plan | None) -> list[AgentTurn]:
    """Final-round valid agents behind `plan` (via the candidate's origins)."""
    if plan is None:
        return []
    cand = next((c for c in report.candidates if c.plan.key() == plan.key()), None)
    if cand is None:
        return []
    final = [t for t in result.turns_for(report.final_round) if t.valid]
    picked: list[AgentTurn] = []
    for origin in cand.origins:
        group, who = origin.split(":", 1)
        for t in final:
            if t.group == group and (who == "consensus" or t.persona.value == who) and t not in picked:
                picked.append(t)
    return picked


def _grounded_claims(turns: list[AgentTurn], ctx: DebateContext) -> list:
    out = []
    for t in turns:
        for c in t.output.claims:
            if c.kind == ClaimKind.FACT and not score_claim(c, ctx.handle_map).flagged:
                out.append(c)
    return out


def _citations(claims: list, ctx: DebateContext) -> list[dict]:
    seen: dict[str, dict] = {}
    for c in claims:
        for h in c.evidence_ids:
            item = ctx.handle_map.get(h)
            if item and h not in seen:
                seen[h] = {"handle": h, "source": item.source, "citation": item.citation, "query": item.query}
    return list(seen.values())


def _user_figures_feasible(state, prims, plan: Plan, ev, as_of, living) -> dict:
    sched = build_schedule(state, prims, plan, as_of, living)
    surplus = sched.income[0] - sched.fixed[0]
    saved = q(max(surplus, Decimal(0)) * Decimal(plan.monthly_saving_rate) / Decimal(100))
    return {
        "opening_pool": str(q(sched.opening_pool)),
        "monthly_surplus": str(q(surplus)),
        "monthly_saving": str(saved),
        "terminal_pool": str(ev.terminal_pool),
        "goal_shortfall": str(ev.goal_shortfall),
        "min_liquidity_slack": str(ev.min_liquidity_slack),
        "scenarios": {
            name: {
                "feasible": r.feasible,
                "terminal_pool": str(r.terminal_pool),
                "goal_shortfall": str(r.goal_shortfall),
                "min_liquidity_slack": str(r.min_liquidity_slack),
            }
            for name, r in ev.scenario_results.items()
        },
    }


def run_pipeline(
    db: Session,
    hurdle_id: str,
    llm: LLMClient,
    *,
    assumptions: Assumptions | None = None,
    monthly_living_expense: Decimal | None = None,
    as_of: date | None = None,
    user_id: str | None = None,
    force_refresh_evidence: bool = False,
    state: FinancialState | None = None,
    snapshot: Any = None,
    persist_debate: bool = True,
    ctx: DebateContext | None = None,
) -> Outcome:
    """Steps 1-5 of the orchestrator; no recommendation row is written. `state`
    (eval harness) overrides the ledger-derived state; `ctx` lets a caller share one
    already-built (and leak-scanned) debate context between arms."""
    hurdle = db.get(Hurdle, hurdle_id)
    if hurdle is None:
        raise HurdleNotFound(f"Hurdle {hurdle_id} not found.")
    as_of = as_of or date.today()
    assumptions = assumptions or Assumptions()
    living = monthly_living_expense

    # 1. evidence
    if snapshot is None:
        snapshot = run_evidence_pipeline(db, hurdle_id, force_refresh=force_refresh_evidence)
    math_state = state if state is not None else rebuild_state_from_ledger(db)
    prims = parse_primitives(hurdle.primitives)
    pool_empty = opening_pool(math_state) == 0

    # 2. real feasibility; infeasible -> no LLM call at all
    assess = assess_feasibility(math_state, prims, assumptions, living, as_of)
    base = dict(hurdle=hurdle, snapshot=snapshot, state=math_state, as_of=as_of, assessment=assess)
    if assess.status == "INFEASIBLE":
        return Outcome(
            outcome="INFEASIBLE",
            guardrails=run_guardrails(infeasible=True, living_expense_missing=living is None, pool_empty=pool_empty),
            confidence=compute_confidence(0, 0, 0, False),
            gap=assess.gap,
            citations=[],
            backing=[],
            **base,
        )

    # 3. debate (agents see DEBATE_AGENT egress only)
    forbidden = _magnitudes(math_state.model_dump(mode="python")) + _magnitudes(hurdle.primitives)
    guarded = _GuardedLLM(llm, forbidden)
    try:
        ctx = ctx or build_debate_context(db, hurdle_id, snapshot, as_of=as_of, user_id=user_id, state=state)
        result = run_debate(ctx, guarded, selector=EvidenceQualitySelector())
    except DebateInputLeak as leak:
        return Outcome(
            outcome="BLOCKED",
            guardrails=run_guardrails(leak=str(leak), living_expense_missing=living is None, pool_empty=pool_empty),
            confidence=compute_confidence(0, 0, 0, False),
            citations=[],
            backing=[],
            **base,
        )
    run_id = save_debate_run(db, hurdle_id, snapshot, result, guarded.name).id if persist_debate else None

    # 4. evaluate, then the optimizer ranks the debated candidates
    report = evaluate_debate(result, ctx)
    opt = optimize(math_state, prims, [(c.plan, c.support) for c in report.candidates], assumptions, living, as_of)
    common = dict(ctx=ctx, debate=result, report=report, optimizer=opt, debate_run_id=run_id, llm_name=guarded.name, **base)
    failed = sum(1 for t in result.turns if not t.valid)
    if opt.status == "INFEASIBLE":
        return Outcome(
            outcome="INFEASIBLE",
            guardrails=run_guardrails(infeasible=True, living_expense_missing=living is None, pool_empty=pool_empty, failed_turns=failed),
            confidence=compute_confidence(0, report.agreement, 0, False, low_diversity=report.low_diversity),
            gap=opt.gap,
            citations=[],
            backing=[],
            **common,
        )

    # 5. guardrails + confidence
    chosen, ev = opt.chosen, opt.chosen_eval
    origin_plan = opt.repaired_from or chosen
    backing = _backing_agents(report, result, origin_plan)
    grounded = _grounded_claims(backing, ctx)
    final_scores = {(a.group, a.persona): Decimal(str(a.score)) for a in report.agent_scores if a.round_no == report.final_round}
    if backing:
        quality = sum((final_scores[(t.group, t.persona.value)] for t in backing), Decimal(0)) / len(backing)
    else:
        quality = Decimal(0)

    texts = [i.content for i in ctx.items] + [i.citation for i in ctx.items]
    for t in result.turns:
        if t.output:
            texts.append(t.output.summary)
            texts += [c.text for c in t.output.claims]
            texts += [r.point for r in t.output.rebuttals]

    g = run_guardrails(
        policy_violations=ev.policy_violations,
        texts=texts,
        grounded_facts=len(grounded),
        hallucination_rate=report.hallucination.overall.rate,
        fabrication_rate=report.hallucination.overall.fabrication_rate,
        misattribution_rate=report.hallucination.overall.misattribution_rate,
        living_expense_missing=living is None,
        pool_empty=pool_empty,
        failed_turns=failed,
        repaired=opt.status == "REPAIRED",
        low_diversity=report.low_diversity,
    )
    complete = living is not None and not pool_empty and failed == 0
    conf = compute_confidence(ev.scenarios_feasible, report.agreement, quality, complete, has_warn=g.needs_ack, low_diversity=report.low_diversity)
    return Outcome(
        outcome="BLOCKED" if g.blocked else "RECOMMENDED",
        guardrails=g,
        confidence=conf,
        chosen=chosen,
        backing=backing,
        citations=_citations(grounded, ctx),
        **common,
    )


# ---- persistence ------------------------------------------------------------
def summarize_plan(plan: dict) -> str:
    a = plan["allocation"]
    return (
        f"Allocate {a['CASH_SAVINGS']}% cash savings, {a['LIQUID_FUND']}% liquid fund, "
        f"{a['FIXED_DEPOSIT']}% fixed deposit, {a['EQUITY_INDEX']}% equity index; "
        f"save {plan['monthly_saving_rate']}% of monthly surplus; "
        f"shift the deadline by {plan['deadline_shift_months']} month(s); "
        f"hold a {plan['emergency_buffer_months']}-month emergency buffer."
    )


def _persist(db: Session, o: Outcome, assumptions: Assumptions, living: Decimal | None) -> Recommendation:
    prims = parse_primitives(o.hurdle.primitives)
    chosen_ev = o.optimizer.chosen_eval if o.optimizer else None
    if o.outcome == "INFEASIBLE":
        gap = o.gap
        figures = {
            "opening_pool": str(q(opening_pool(o.state))),
            "best_case_shortfall": str(gap.best_case_shortfall) if gap else None,
            "extra_monthly_saving_needed": str(gap.extra_monthly_saving) if gap and gap.extra_monthly_saving is not None else None,
            "months_of_delay_needed": gap.months_of_delay if gap else None,
        }
    elif chosen_ev is not None:
        figures = _user_figures_feasible(o.state, prims, o.chosen, chosen_ev, o.as_of, living)
    else:
        figures = {"opening_pool": str(q(opening_pool(o.state)))}

    evaluation = {
        "as_of": o.as_of.isoformat(),
        "living_expense_provided": living is not None,
        "assumptions": assumptions.model_dump(mode="json"),
        "feasibility": o.assessment.model_dump(mode="json"),
        "gap": o.gap.model_dump(mode="json") if o.gap else None,
        "report": o.report.model_dump(mode="json") if o.report else None,
        "optimizer": o.optimizer.model_dump(mode="json") if o.optimizer else None,
        "backing_agents": [f"{t.group}:{t.persona.value}" for t in (o.backing or [])],
    }
    rec = Recommendation(
        hurdle_id=o.hurdle.id,
        outcome=o.outcome,
        snapshot_id=o.snapshot.id,
        snapshot_hash=o.snapshot.snapshot_hash,
        debate_run_id=o.debate_run_id,
        chosen_plan=o.chosen.model_dump(mode="json") if o.chosen else None,
        evaluation=evaluation,
        citations=o.citations or [],
        confidence=o.confidence.value,
        confidence_components={k: str(v) for k, v in o.confidence.components.items()},
        confidence_band=o.confidence.band,
        guardrails=o.guardrails.model_dump(mode="json"),
        user_figures=figures,
        disclaimer=DISCLAIMER,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def generate_recommendation(
    db: Session,
    hurdle_id: str,
    llm: LLMClient,
    assumptions: Assumptions | None = None,
    monthly_living_expense: Decimal | None = None,
    as_of: date | None = None,
    user_id: str | None = None,
    force_refresh_evidence: bool = False,
) -> Recommendation:
    assumptions = assumptions or Assumptions()
    o = run_pipeline(
        db,
        hurdle_id,
        llm,
        assumptions=assumptions,
        monthly_living_expense=monthly_living_expense,
        as_of=as_of,
        user_id=user_id,
        force_refresh_evidence=force_refresh_evidence,
    )
    return _persist(db, o, assumptions, monthly_living_expense)


# ---- decisions --------------------------------------------------------------
def get_recommendation(db: Session, recommendation_id: str) -> Recommendation:
    rec = db.get(Recommendation, recommendation_id)
    if rec is None:
        raise RecommendationNotFound(f"Recommendation {recommendation_id} not found.")
    return rec


def get_decision(db: Session, recommendation_id: str) -> RecommendationDecision | None:
    return db.scalars(
        select(RecommendationDecision).where(RecommendationDecision.recommendation_id == recommendation_id)
    ).first()


def status_of(db: Session, rec: Recommendation) -> str:
    d = get_decision(db, rec.id)
    return d.decision if d else "PENDING"


def confirm_recommendation(db: Session, recommendation_id: str, confirmed_by: str, acknowledge_warnings: bool = False):
    rec = get_recommendation(db, recommendation_id)
    if get_decision(db, rec.id) is not None:
        raise RecommendationConflict("This recommendation already has a decision.")
    if rec.outcome != "RECOMMENDED" or rec.guardrails.get("blocked") or rec.chosen_plan is None:
        raise RecommendationConflict("A blocked or infeasible recommendation cannot be confirmed.")
    if (rec.guardrails.get("needs_ack") or rec.confidence_band == "LOW") and not acknowledge_warnings:
        raise RecommendationConflict("Warnings or LOW confidence require acknowledge_warnings=true.")

    entry = append_entry(
        db,
        LedgerEntryCreate(
            effective_date=date.today(),
            type=LedgerEntryType.COMMITMENT,
            source=LedgerSource.AGENT_SUGGESTED_USER_CONFIRMED,
            description="Adopted plan: " + summarize_plan(rec.chosen_plan) + f" Confidence {rec.confidence} ({rec.confidence_band}). {DISCLAIMER}",
            effect=[],  # state changes only when real transactions happen
            linked_hurdle_id=rec.hurdle_id,
            evidence={
                "recommendation_id": rec.id,
                "debate_run_id": rec.debate_run_id,
                "snapshot_hash": rec.snapshot_hash,
                "citations": rec.citations,
                "confidence": {"value": str(rec.confidence), "band": rec.confidence_band, "components": rec.confidence_components},
            },
        ),
        commit=False,
    )
    decision = RecommendationDecision(
        recommendation_id=rec.id,
        decision="CONFIRMED",
        decided_by=confirmed_by,
        acknowledge_warnings=acknowledge_warnings,
        ledger_entry_id=entry.id,
    )
    db.add(decision)
    try:
        db.commit()  # ledger entry + decision commit together or not at all
    except IntegrityError:
        db.rollback()
        raise RecommendationConflict("This recommendation already has a decision.") from None
    db.refresh(decision)
    return rec, decision


def reject_recommendation(db: Session, recommendation_id: str, rejected_by: str, reason: str):
    rec = get_recommendation(db, recommendation_id)
    if get_decision(db, rec.id) is not None:
        raise RecommendationConflict("This recommendation already has a decision.")
    decision = RecommendationDecision(recommendation_id=rec.id, decision="REJECTED", decided_by=rejected_by, reason=reason)
    db.add(decision)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise RecommendationConflict("This recommendation already has a decision.") from None
    db.refresh(decision)
    return rec, decision
