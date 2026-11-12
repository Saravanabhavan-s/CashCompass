"""Per-arm evaluation metrics (all Decimal; rates quantized to 4 dp).

hallucination_rate   flagged FACT claims / FACT claims, pooled over every
                     query and every claim the arm produced (3c definition;
                     for debate ON that is all valid turns in all rounds)
feasibility_rate     queries whose best PROPOSAL was feasible without repair
                     / non-failed queries
final_feasible_rate  queries with a feasible, policy-clean plan after the
                     optimizer's optional repair / non-failed queries
policy_violation_rate  proposals violating the hard allocation policy / proposals
label_agreement      passed expectation checks / expectation checks
mean_confidence      mean confidence over non-failed queries
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Iterable

from pydantic import BaseModel

from app.debate.schemas import Plan
from app.eval.dataset import Expectations

_Q = Decimal("0.0001")
ARMS = ("debate_on", "debate_off")


def _rate(num: int, den: int) -> Decimal | None:
    return (Decimal(num) / Decimal(den)).quantize(_Q, rounding=ROUND_HALF_EVEN) if den else None


class ArmResult(BaseModel):
    arm: str
    status: str  # OK | INFEASIBLE | BLOCKED | FAILED
    error: str | None = None
    snapshot_hash: str
    llm_calls: int = 0
    facts_total: int = 0
    facts_flagged: int = 0
    proposals: int = 0
    proposals_policy_violating: int = 0
    plan_feasible: bool = False  # best proposal feasible WITHOUT repair
    final_feasible: bool = False  # feasible, policy-clean plan exists (after optional repair)
    chosen_plan: Plan | None = None
    confidence: Decimal | None = None
    confidence_band: str | None = None
    guardrail_codes: list[str] = []
    label_checks: dict[str, bool] = {}


class QueryResult(BaseModel):
    id: str
    description: str = ""
    snapshot_hash: str
    debate_on: ArmResult
    debate_off: ArmResult


class ArmSummary(BaseModel):
    arm: str
    queries: int
    failed: int
    facts_total: int
    facts_flagged: int
    hallucination_rate: Decimal | None
    feasibility_rate: Decimal | None
    final_feasible_rate: Decimal | None
    proposals: int
    policy_violation_rate: Decimal | None
    label_checks: int
    label_agreement: Decimal | None
    mean_confidence: Decimal | None
    llm_calls: int


def check_labels(exp: Expectations, chosen: Plan | None, final_feasible: bool) -> dict[str, bool]:
    """Pass/fail per expectation that is set. With no chosen plan every plan-based check fails."""
    out: dict[str, bool] = {}
    if exp.must_be_feasible is not None:
        out["must_be_feasible"] = final_feasible == exp.must_be_feasible
    if exp.max_equity_pct is not None:
        out["max_equity_pct"] = chosen is not None and chosen.allocation[_equity()] <= exp.max_equity_pct
    if exp.min_buffer_months is not None:
        out["min_buffer_months"] = chosen is not None and chosen.emergency_buffer_months >= exp.min_buffer_months
    if exp.must_shift_deadline is not None:
        out["must_shift_deadline"] = chosen is not None and (chosen.deadline_shift_months > 0) == exp.must_shift_deadline
    return out


def _equity():
    from app.debate.schemas import Instrument

    return Instrument.EQUITY_INDEX


def summarize(results: Iterable[QueryResult]) -> dict[str, ArmSummary]:
    results = list(results)
    out: dict[str, ArmSummary] = {}
    for arm in ARMS:
        arms: list[ArmResult] = [getattr(r, arm) for r in results]
        ok = [a for a in arms if a.status != "FAILED"]
        facts_total = sum(a.facts_total for a in arms)
        facts_flagged = sum(a.facts_flagged for a in arms)
        proposals = sum(a.proposals for a in arms)
        checks = [v for a in arms for v in a.label_checks.values()]
        confs = [a.confidence for a in ok if a.confidence is not None]
        out[arm] = ArmSummary(
            arm=arm,
            queries=len(arms),
            failed=len(arms) - len(ok),
            facts_total=facts_total,
            facts_flagged=facts_flagged,
            hallucination_rate=_rate(facts_flagged, facts_total),
            feasibility_rate=_rate(sum(a.plan_feasible for a in ok), len(ok)),
            final_feasible_rate=_rate(sum(a.final_feasible for a in ok), len(ok)),
            proposals=proposals,
            policy_violation_rate=_rate(sum(a.proposals_policy_violating for a in arms), proposals),
            label_checks=len(checks),
            label_agreement=_rate(sum(checks), len(checks)),
            mean_confidence=(sum(confs, Decimal(0)) / len(confs)).quantize(_Q, rounding=ROUND_HALF_EVEN) if confs else None,
            llm_calls=sum(a.llm_calls for a in arms),
        )
    return out


def _fmt(v: Decimal | None) -> str:
    return "n/a" if v is None else f"{v:.4f}"


def to_markdown(summary: dict[str, ArmSummary], stamp: str | None = None, n_queries: int | None = None) -> str:
    on, off = summary["debate_on"], summary["debate_off"]
    rows = [
        ("hallucination_rate (flagged/FACT claims)", f"{_fmt(on.hallucination_rate)} ({on.facts_flagged}/{on.facts_total})", f"{_fmt(off.hallucination_rate)} ({off.facts_flagged}/{off.facts_total})"),
        ("plan feasibility rate (no repair)", _fmt(on.feasibility_rate), _fmt(off.feasibility_rate)),
        ("final feasible rate (after repair)", _fmt(on.final_feasible_rate), _fmt(off.final_feasible_rate)),
        ("policy-violation rate (proposals)", f"{_fmt(on.policy_violation_rate)} ({on.proposals} proposals)", f"{_fmt(off.policy_violation_rate)} ({off.proposals} proposals)"),
        ("label agreement", f"{_fmt(on.label_agreement)} ({on.label_checks} checks)", f"{_fmt(off.label_agreement)} ({off.label_checks} checks)"),
        ("mean confidence", _fmt(on.mean_confidence), _fmt(off.mean_confidence)),
        ("failed queries", str(on.failed), str(off.failed)),
        ("LLM calls", str(on.llm_calls), str(off.llm_calls)),
    ]
    lines = []
    if stamp:
        lines += [f"> **{stamp}**", ""]
    lines += [
        f"Queries: {n_queries if n_queries is not None else on.queries}. Both arms read the SAME frozen evidence snapshot per query.",
        "",
        "| metric | Debate ON | Debate OFF |",
        "|---|---|---|",
    ]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(lines) + "\n"
