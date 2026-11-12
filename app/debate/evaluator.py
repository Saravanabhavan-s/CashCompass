"""Layer 3c: evidence-quality meta-evaluator. Fully deterministic -- NO LLM
judge (a judge model can itself hallucinate).

Claim scoring
  FACT       flagged (score 0) if ANY of:
               no_citation        empty evidence_ids
               unknown_handle     a cited handle is not in the snapshot
               ungrounded_figure  a quoted figure is not found verbatim (after
                                  whitespace/comma normalisation) in ANY cited
                                  item's content
             otherwise score = mean source weight of the (distinct) cited items
             x figure_grounding_ratio. The ratio is always reported; for an
             unflagged claim it is necessarily 1 (any ungrounded figure flags).
  INFERENCE  fixed weight 0.3; never counted as a hallucination.
Agent score = mean claim score; group score = mean agent score (valid agents).

hallucination_rate = flagged FACT claims / total FACT claims over all valid
turns (0.0 when there are no FACT claims), with per-round and per-persona
breakdowns and the raw counts.

unresolved_dissent (deliberately simple): a last-round rebuttal from a SKEPTIC
or CONTRARIAN counts as unresolved when its target persona did NOT change its
plan (any of the four levers) between the previous round and the last round.
"Target" agents = agents of that persona in the rebutting agent's own group, or
in every group if that persona is not in the rebutting group; the dissent is
resolved if ANY of them changed. A single-round debate cannot show any change,
so all its rebuttals are unresolved.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

from pydantic import BaseModel

from app.debate.aggregate import aggregate_plans, plan_distance
from app.debate.context import CASE_HANDLE, DebateContext, EvidenceRef
from app.debate.schemas import AgentOutput, AgentTurn, Claim, ClaimKind, DebateResult, LeaderDecision, Plan
from app.evidence.personas import Persona
from app.evidence.sources import EvidenceSource

SOURCE_WEIGHT: dict[str, Decimal] = {
    EvidenceSource.FRED.value: Decimal("1.0"),
    EvidenceSource.LOCAL_REGULATORY.value: Decimal("1.0"),
    EvidenceSource.ALPHA_VANTAGE.value: Decimal("0.8"),
    "CASE": Decimal("1.0"),  # C1: the user's own banded case file
}
INFERENCE_WEIGHT = Decimal("0.3")

_Q = Decimal("0.000001")


def _f(d: Decimal) -> float:
    return float(d.quantize(_Q, rounding=ROUND_HALF_UP))


_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-"})  # en/em dash, minus -> '-'
_CURRENCY = re.compile(r"₹|\brs\b\.?|\binr\b", re.IGNORECASE)  # rupee sign, Rs, Rs., INR
_LAKH = re.compile(r"\b(?:lakhs?|lacs?)\b", re.IGNORECASE)


def _norm(s: str) -> str:
    """Normalise a figure / evidence text before the verbatim check:
    - dash variants (en/em dash, minus) -> '-'  (the banded case prints "5-6 lakh" with an en dash)
    - currency markers (rupee sign, Rs, Rs., INR; any case) are removed, so "Rs 5 lakh" == "5 lakh"
    - "lakh", "lakhs", "lac", "lacs" -> "lakh"
    - all whitespace and commas are removed, which also makes "6.5 %" == "6.5%"
    Everything else stays case- and digit-sensitive: "6 lakh" never matches "5 lakh"."""
    s = s.translate(_DASHES)
    s = _CURRENCY.sub("", s)
    s = _LAKH.sub("lakh", s)
    return re.sub(r"[\s,]+", "", s)


# ---- report models ---------------------------------------------------------
class ClaimScore(BaseModel):
    kind: str
    text: str
    score: float
    flagged: bool
    reasons: list[str]
    figure_grounding_ratio: float
    mean_source_weight: float | None = None


class AgentScore(BaseModel):
    group: str
    persona: str
    round_no: int
    score: float
    claims: list[ClaimScore]


class Counts(BaseModel):
    flagged: int
    total: int
    rate: float


class HallucinationMetrics(BaseModel):
    overall: Counts
    by_round: dict[str, Counts]
    by_persona: dict[str, Counts]


class FlaggedClaim(BaseModel):
    group: str
    persona: str
    round_no: int
    text: str
    reasons: list[str]
    evidence_ids: list[str]


class Candidate(BaseModel):
    plan: Plan
    support: float
    origins: list[str]  # e.g. "group_1:consensus", "group_2:skeptic"


class Dissent(BaseModel):
    group: str
    from_persona: str
    target: str
    point: str
    round_no: int


LOW_DIVERSITY_AT = Decimal("0.5")


def _norm_text(s: str) -> str:
    return " ".join(s.lower().split())


def claim_redundancy(turns: list[AgentTurn]) -> Decimal:
    """Share of claims whose whitespace/case-normalised text also appears in at least
    one OTHER agent's claims. Pass the final-round turns. Repeats inside one agent
    do not count: only cross-agent copying signals a converged-by-copying debate."""
    per_agent = [{_norm_text(c.text) for c in t.output.claims} for t in turns if t.output]
    total = sum(len(t.output.claims) for t in turns if t.output)
    if total == 0:
        return Decimal(0)
    shared = 0
    for i, t in enumerate([t for t in turns if t.output]):
        for c in t.output.claims:
            text = _norm_text(c.text)
            if any(text in per_agent[j] for j in range(len(per_agent)) if j != i):
                shared += 1
    return Decimal(shared) / Decimal(total)


class CitationCounts(BaseModel):
    """FACT claims by what they cite: the user case (C1), snapshot evidence (E-handles), both, or nothing."""

    facts_total: int
    citing_case: int
    citing_evidence: int
    citing_both: int
    uncited: int


class EvaluationReport(BaseModel):
    snapshot_hash: str
    prompt_version: str
    final_round: int
    agent_scores: list[AgentScore]
    group_scores: dict[str, float]  # final round
    hallucination: HallucinationMetrics
    citations: CitationCounts
    flagged_claims: list[FlaggedClaim]
    agreement: float
    claim_redundancy: float  # final round; see claim_redundancy()
    low_diversity: bool  # redundancy >= 0.5 -> agreement is capped at 0.8 in confidence
    candidates: list[Candidate]
    unresolved_dissent: list[Dissent]


# ---- scoring ---------------------------------------------------------------
class _ClaimEval:
    __slots__ = ("score", "flagged", "reasons", "ratio", "weight")

    def __init__(self, score: Decimal, flagged: bool, reasons: list[str], ratio: Decimal, weight: Decimal | None):
        self.score, self.flagged, self.reasons, self.ratio, self.weight = score, flagged, reasons, ratio, weight


def _eval_claim(claim: Claim, handle_map: Mapping[str, EvidenceRef]) -> _ClaimEval:
    if claim.kind == ClaimKind.INFERENCE:
        return _ClaimEval(INFERENCE_WEIGHT, False, [], Decimal(1), None)

    handles = list(dict.fromkeys(claim.evidence_ids))
    reasons: list[str] = []
    if not handles:
        reasons.append("no_citation")
    known = [handle_map[h] for h in handles if h in handle_map]
    if len(known) != len(handles):
        reasons.append("unknown_handle")

    figures = claim.quoted_figures
    contents = [_norm(i.content) for i in known]
    grounded = sum(1 for fig in figures if _norm(fig) and any(_norm(fig) in c for c in contents))
    ratio = Decimal(grounded) / Decimal(len(figures)) if figures else Decimal(1)
    if grounded < len(figures):
        reasons.append("ungrounded_figure")

    weight = sum((SOURCE_WEIGHT[i.source] for i in known), Decimal(0)) / len(known) if known else None
    if reasons:
        return _ClaimEval(Decimal(0), True, reasons, ratio, weight)
    return _ClaimEval(weight * ratio, False, [], ratio, weight)


def score_claim(claim: Claim, handle_map: Mapping[str, EvidenceRef]) -> ClaimScore:
    e = _eval_claim(claim, handle_map)
    return ClaimScore(
        kind=claim.kind.value,
        text=claim.text,
        score=_f(e.score),
        flagged=e.flagged,
        reasons=e.reasons,
        figure_grounding_ratio=_f(e.ratio),
        mean_source_weight=_f(e.weight) if e.weight is not None else None,
    )


def _agent_score(output: AgentOutput, handle_map: Mapping[str, EvidenceRef]) -> Decimal:
    scores = [_eval_claim(c, handle_map).score for c in output.claims]
    return sum(scores, Decimal(0)) / len(scores)


def score_agent(output: AgentOutput, handle_map: Mapping[str, EvidenceRef]) -> float:
    return _f(_agent_score(output, handle_map))


def _group_score(turns: list[AgentTurn], handle_map) -> Decimal:
    valid = [t for t in turns if t.output]
    if not valid:
        return Decimal(0)
    return sum((_agent_score(t.output, handle_map) for t in valid), Decimal(0)) / len(valid)


def score_group(turns: list[AgentTurn], handle_map: Mapping[str, EvidenceRef]) -> float:
    return _f(_group_score(turns, handle_map))


def _counts(flagged: int, total: int) -> Counts:
    rate = _f(Decimal(flagged) / Decimal(total)) if total else 0.0
    return Counts(flagged=flagged, total=total, rate=rate)


def hallucination_metrics(turns: list[AgentTurn], handle_map: Mapping[str, EvidenceRef]) -> HallucinationMetrics:
    tally: dict[tuple, list[int]] = {}

    def bump(key, flagged):
        c = tally.setdefault(key, [0, 0])
        c[1] += 1
        c[0] += int(flagged)

    for t in turns:
        if not t.output:
            continue
        for c in t.output.claims:
            if c.kind != ClaimKind.FACT:
                continue
            fl = _eval_claim(c, handle_map).flagged
            bump(("all",), fl)
            bump(("round", t.round_no), fl)
            bump(("persona", t.persona.value), fl)

    def get(*key):
        f, n = tally.get(key, [0, 0])
        return _counts(f, n)

    rounds = sorted({t.round_no for t in turns})
    personas = sorted({t.persona.value for t in turns})
    return HallucinationMetrics(
        overall=get("all"),
        by_round={str(r): get("round", r) for r in rounds},
        by_persona={p: get("persona", p) for p in personas},
    )


def citation_counts(turns: list[AgentTurn]) -> CitationCounts:
    total = case = ev = both = none = 0
    for t in turns:
        if not t.output:
            continue
        for c in t.output.claims:
            if c.kind != ClaimKind.FACT:
                continue
            total += 1
            has_case = CASE_HANDLE in c.evidence_ids
            has_ev = any(h.startswith("E") for h in c.evidence_ids)
            case += has_case
            ev += has_ev
            both += has_case and has_ev
            none += not c.evidence_ids
    return CitationCounts(facts_total=total, citing_case=case, citing_evidence=ev, citing_both=both, uncited=none)


# ---- selector --------------------------------------------------------------
class EvidenceQualitySelector:
    """LeaderSelector: agent weights = evidence scores; group consensus =
    aggregate_plans(plans, weights); leader = highest group score.

    Tie-break: the group whose consensus is closer (plan_distance) to the other
    group(s)' consensus -- mean distance when there are more than two -- then
    configured group order. NOTE with exactly two groups that distance is
    symmetric, so a two-group tie always falls through to configured order."""

    def select(self, round_no: int, group_outputs: dict[str, list[AgentTurn]], ctx: DebateContext) -> LeaderDecision:
        from app.exceptions import DebateFailed

        order = list(group_outputs)
        consensus: dict[str, Plan] = {}
        scores: dict[str, Decimal] = {}
        for g, turns in group_outputs.items():
            valid = [t for t in turns if t.output]
            if not valid:
                continue
            agent = [_agent_score(t.output, ctx.handle_map) for t in valid]
            consensus[g] = aggregate_plans([t.output.plan for t in valid], [float(a) for a in agent])
            scores[g] = sum(agent, Decimal(0)) / len(agent)
        if not consensus:
            raise DebateFailed(f"round {round_no}: no valid outputs")

        best = max(scores.values())
        tied = [g for g in consensus if scores[g] == best]
        if len(tied) > 1:
            def closeness(g: str) -> float:
                others = [o for o in consensus if o != g]
                return sum(plan_distance(consensus[g], consensus[o]) for o in others) / len(others) if others else 0.0

            tied.sort(key=lambda g: (closeness(g), order.index(g)))
        return LeaderDecision(group=tied[0], scores={g: _f(s) for g, s in scores.items()}, consensus=consensus)


# ---- report ----------------------------------------------------------------
def _unresolved_dissent(result: DebateResult, last: int) -> list[Dissent]:
    cfg = result.config
    prev_by = {(t.group, t.persona): t for t in result.turns_for(last - 1) if t.output} if last > 1 else {}
    out: list[Dissent] = []
    for t in result.turns_for(last):
        if not t.output or t.persona not in (Persona.SKEPTIC, Persona.CONTRARIAN):
            continue
        for rb in t.output.rebuttals:
            groups = [t.group] if rb.target in cfg.groups.get(t.group, ()) else cfg.group_order()
            changed = False
            for g in groups:
                now = next((x for x in result.turns_for(last) if x.group == g and x.persona == rb.target and x.output), None)
                before = prev_by.get((g, rb.target))
                if now and before and now.output.plan.key() != before.output.plan.key():
                    changed = True
            if not changed:
                out.append(Dissent(group=t.group, from_persona=t.persona.value, target=rb.target.value, point=rb.point, round_no=last))
    return out


def evaluate_debate(result: DebateResult, ctx: DebateContext) -> EvaluationReport:
    hm = ctx.handle_map
    last = max(t.round_no for t in result.turns)
    final_turns = result.turns_for(last)

    agent_scores: list[AgentScore] = []
    for t in result.turns:
        if t.output:
            agent_scores.append(
                AgentScore(
                    group=t.group,
                    persona=t.persona.value,
                    round_no=t.round_no,
                    score=score_agent(t.output, hm),
                    claims=[score_claim(c, hm) for c in t.output.claims],
                )
            )

    flagged_claims = [
        FlaggedClaim(
            group=t.group,
            persona=t.persona.value,
            round_no=t.round_no,
            text=c.text,
            reasons=_eval_claim(c, hm).reasons,
            evidence_ids=list(c.evidence_ids),
        )
        for t in result.turns
        if t.output
        for c in t.output.claims
        if c.kind == ClaimKind.FACT and _eval_claim(c, hm).flagged
    ]

    group_dec = {g: _group_score([t for t in final_turns if t.group == g], hm) for g in result.config.group_order()}
    group_scores = {g: _f(s) for g, s in group_dec.items() if any(t.group == g and t.output for t in final_turns)}

    cons = [result.final_consensus[g] for g in result.config.group_order() if g in result.final_consensus]
    if len(cons) < 2:
        agreement = 1.0
    else:
        pairs = [plan_distance(a, b) for i, a in enumerate(cons) for b in cons[i + 1:]]
        agreement = _f(Decimal(1) - Decimal(str(sum(pairs) / len(pairs))))

    redundancy = claim_redundancy(final_turns)

    cands: dict[tuple, Candidate] = {}

    def add(plan: Plan, support: Decimal, origin: str):
        c = cands.get(plan.key())
        if c is None:
            cands[plan.key()] = Candidate(plan=plan, support=_f(support), origins=[origin])
        else:
            c.origins.append(origin)
            c.support = max(c.support, _f(support))

    for g in result.config.group_order():
        if g in result.final_consensus:
            add(result.final_consensus[g], group_dec[g], f"{g}:consensus")
    for t in final_turns:
        if t.output:
            add(t.output.plan, _agent_score(t.output, hm), f"{t.group}:{t.persona.value}")
    candidates = sorted(cands.values(), key=lambda c: -c.support)  # stable: ties keep insertion order

    return EvaluationReport(
        snapshot_hash=result.snapshot_hash,
        prompt_version=result.prompt_version,
        final_round=last,
        agent_scores=agent_scores,
        group_scores=group_scores,
        hallucination=hallucination_metrics(result.turns, hm),
        citations=citation_counts(result.turns),
        flagged_claims=flagged_claims,
        agreement=agreement,
        claim_redundancy=_f(redundancy),
        low_diversity=redundancy >= LOW_DIVERSITY_AT,
        candidates=candidates,
        unresolved_dissent=_unresolved_dissent(result, last),
    )
