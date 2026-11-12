"""Prompt templates and FROZEN persona briefs.

Briefs never change across rounds (G-DMAD: only the answer is refined, the
reasoning strategy stays fixed -- that is what keeps the diversity axis alive).
PROMPT_VERSION hashes every template and brief so any edit is visible in the
persisted debate_run.
"""
from __future__ import annotations

import hashlib
import json

from app.debate.context import DebateContext, EvidenceRef
from app.debate.schemas import AgentOutput, LeaderDecision
from app.evidence.personas import Persona

SYSTEM_TEMPLATE = """You are one participant in a structured financial-planning debate.
Your persona: {persona_name}.
{brief}

Rules:
- You argue STRATEGY only. Never do arithmetic on money; deterministic code computes amounts later.
- All user figures are coarse bands. Do not try to infer exact amounts.
- Use ONLY the numbered evidence items (E1, E2, ...) and the user case [C1] you are shown. Do not rely on outside knowledge for FACT claims.
- Statements about the user's own situation cite C1 (the user case below); external facts cite E-handles; a claim may cite both.
- A FACT claim must cite at least one evidence handle in evidence_ids, and every number in quoted_figures must appear verbatim in a cited item.
- An INFERENCE claim is your own reasoning; label it INFERENCE and do not present it as sourced.
- Reply with ONE JSON object and nothing else."""

OUTPUT_SPEC = """Return JSON with exactly these keys:
{
  "plan": {
    "allocation": {"CASH_SAVINGS": int, "LIQUID_FUND": int, "FIXED_DEPOSIT": int, "EQUITY_INDEX": int},
    "monthly_saving_rate": int 0-100 (percent of surplus saved),
    "deadline_shift_months": int 0-12,
    "emergency_buffer_months": int 0-12
  },
  "claims": [ 1 to 6 items: {"kind": "FACT" | "INFERENCE", "text": str (<=500 chars), "evidence_ids": ["E3", ...], "quoted_figures": ["verbatim number or phrase", ...]} ],
  "rebuttals": [ 0 to 4 items: {"target": "<persona id>", "point": str} ],
  "summary": str (<=400 chars)
}
allocation values are integers 0-100 that sum to exactly 100.
Persona ids: saver, liquidity_hawk, skeptic, opportunity_seeker, contrarian."""

ROUND1_TEMPLATE = """ROUND {round_no} of {rounds} (independent answer).

USER CASE [C1] (egress-projected, banded):
{case}

EVIDENCE (frozen snapshot):
{evidence}

{output_spec}"""

KEEP_FACT_RULE = (
    "Keep at least one FACT claim with a valid handle and verbatim quoted figures; "
    "do not downgrade all claims to INFERENCE."
)

ROUND_N_TEMPLATE = """ROUND {round_no} of {rounds} (refine your answer; keep your persona's reasoning strategy).

USER CASE [C1] (egress-projected, banded):
{case}

EVIDENCE (frozen snapshot):
{evidence}

YOUR PREVIOUS ANSWER:
{own_prev}

YOUR GROUP-MATES' PREVIOUS ANSWERS:
{mates_prev}

LEADING GROUP'S CONSENSUS PLAN (hint, not an order):
{leader_plan}

LEADING GROUP'S TOP CLAIMS:
{leader_claims}

Update your plan and claims in light of the above. Rebut specific personas where you disagree.
Keep at least one FACT claim with a valid handle and verbatim quoted figures; do not downgrade all claims to INFERENCE.

{output_spec}"""

REPAIR_TEMPLATE = """Your previous reply could not be used.
Validation error:
{error}

Reply again with ONE valid JSON object only, following the same schema.

{output_spec}"""

PERSONA_BRIEFS: dict[Persona, str] = {
    Persona.SAVER: "You favour guaranteed, low-volatility instruments and steady saving. You weigh certainty of principal and sovereign-backed returns heavily.",
    Persona.LIQUIDITY_HAWK: "You prioritise access to cash: emergency buffer size, exit loads, withdrawal penalties. A plan that cannot be liquidated in time is a bad plan.",
    Persona.SKEPTIC: "You challenge sources. Check whether claims are cited, current, and within regulatory limits. Flag anything unverifiable and say what evidence is missing.",
    Persona.OPPORTUNITY_SEEKER: "You look for growth and return above inflation, accepting measured risk when the horizon allows. You weigh the cost of staying too conservative.",
    Persona.CONTRARIAN: "You argue the case others are ignoring: downturn risk, rate reversals, real returns after inflation and tax. Stress-test the emerging consensus.",
}

PROMPT_VERSION: str = hashlib.sha256(
    json.dumps(
        {
            "system": SYSTEM_TEMPLATE,
            "output": OUTPUT_SPEC,
            "r1": ROUND1_TEMPLATE,
            "rn": ROUND_N_TEMPLATE,
            "repair": REPAIR_TEMPLATE,
            "briefs": {p.value: b for p, b in sorted(PERSONA_BRIEFS.items(), key=lambda kv: kv[0].value)},
        },
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()[:16]


assert KEEP_FACT_RULE in ROUND_N_TEMPLATE and KEEP_FACT_RULE not in ROUND1_TEMPLATE + SYSTEM_TEMPLATE + REPAIR_TEMPLATE


def system_prompt(persona: Persona) -> str:
    return SYSTEM_TEMPLATE.format(persona_name=persona.value, brief=PERSONA_BRIEFS[persona])


def prompt_hash(system: str, prompt: str) -> str:
    return hashlib.sha256((system + "\n\x00\n" + prompt).encode("utf-8")).hexdigest()


def _render_item(i: EvidenceRef) -> str:
    return f"[{i.handle}] source={i.source} citation={i.citation}\n{i.content.strip()}"


def evidence_pack(ctx: DebateContext, persona: Persona, extra_handles: set[str] | frozenset[str] = frozenset()) -> list[EvidenceRef]:
    """Items tagged for this persona, plus leader-cited extras, in handle order."""
    return [i for i in ctx.items if persona.value in i.lens_tags or i.handle in extra_handles]


def _render_pack(items: list[EvidenceRef]) -> str:
    return "\n\n".join(_render_item(i) for i in items) if items else "(no evidence items for your persona)"


def _compact(out: AgentOutput) -> str:
    return json.dumps(out.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def render_round1(ctx: DebateContext, persona: Persona, rounds: int) -> str:
    return ROUND1_TEMPLATE.format(
        round_no=1,
        rounds=rounds,
        case=ctx.case_json(),
        evidence=_render_pack(evidence_pack(ctx, persona)),
        output_spec=OUTPUT_SPEC,
    )


def leader_cited_handles(leader_outputs: list[AgentOutput], top_n: int = 3) -> list[str]:
    handles: list[str] = []
    for c in _top_claims(leader_outputs, top_n):
        for h in c.evidence_ids:
            if h not in handles:
                handles.append(h)
    return handles


def _top_claims(leader_outputs: list[AgentOutput], top_n: int):
    """Deterministic: first claim of each valid leader agent, then second, ... (round-robin)."""
    picked = []
    depth = 0
    while len(picked) < top_n and any(len(o.claims) > depth for o in leader_outputs):
        for o in leader_outputs:
            if len(o.claims) > depth and len(picked) < top_n:
                picked.append(o.claims[depth])
        depth += 1
    return picked


def render_round_n(
    ctx: DebateContext,
    persona: Persona,
    round_no: int,
    rounds: int,
    own_prev: AgentOutput | None,
    mates_prev: list[tuple[Persona, AgentOutput]],
    leader: LeaderDecision,
    leader_outputs: list[AgentOutput],
) -> str:
    top = _top_claims(leader_outputs, 3)
    extra = frozenset(leader_cited_handles(leader_outputs))
    claims_txt = (
        "\n".join(
            json.dumps(c.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) for c in top
        )
        or "(none)"
    )
    mates_txt = (
        "\n".join(f"- {p.value}: {_compact(o)}" for p, o in mates_prev) or "(none valid)"
    )
    return ROUND_N_TEMPLATE.format(
        round_no=round_no,
        rounds=rounds,
        case=ctx.case_json(),
        evidence=_render_pack(evidence_pack(ctx, persona, extra)),
        own_prev=_compact(own_prev) if own_prev else "(your previous answer was unusable)",
        mates_prev=mates_txt,
        leader_plan=json.dumps(leader.consensus[leader.group].model_dump(mode="json"), sort_keys=True),
        leader_claims=claims_txt,
        output_spec=OUTPUT_SPEC,
    )


def render_repair(error: str) -> str:
    return REPAIR_TEMPLATE.format(error=error[:1500], output_spec=OUTPUT_SPEC)
