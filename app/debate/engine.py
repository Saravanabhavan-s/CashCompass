"""G-DMAD debate engine (Algorithm 1): G groups x N agents x R rounds.

Round 1 is independent. In round r>1 each agent sees its own previous answer,
its group-mates' previous answers, and the LEADER group's consensus + top
claims (cited evidence is appended to every pack). There is no direct
cross-group agent chat. After every round a pluggable LeaderSelector picks the
leader; 3c supplies the evidence-weighted one.

Agents read ONLY the frozen snapshot (via DebateContext). The LLM argues;
deterministic code (aggregate.py) computes.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.debate import prompts
from app.debate.aggregate import aggregate_plans
from app.debate.context import DebateContext
from app.debate.llm import LLMClient, LLMRequest
from app.debate.schemas import AgentOutput, AgentTurn, DebateConfig, DebateResult, LeaderDecision, Plan
from app.evidence.personas import Persona
from app.exceptions import DebateFailed, EgressPolicyViolation
from app.models.debate import DebateRun, DebateTurn
from app.models.evidence import EvidenceSnapshot

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def parse_agent_output(text: str) -> AgentOutput:
    """Accept bare JSON, fenced JSON, or JSON embedded in prose. Raises
    ValueError (incl. pydantic ValidationError) otherwise."""
    candidates: list[str] = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    dec = json.JSONDecoder()
    last_err: Exception | None = None
    for cand in candidates:
        for m in re.finditer(r"\{", cand):
            try:
                obj, _ = dec.raw_decode(cand[m.start():])
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            try:
                return AgentOutput.model_validate(obj)
            except ValidationError as e:
                last_err = last_err or e  # keep the FIRST (outermost) error for the repair prompt
                continue
    if last_err is not None:
        raise ValueError(str(last_err))
    raise ValueError("no JSON object found in reply")


class LeaderSelector(Protocol):
    def select(self, round_no: int, group_outputs: dict[str, list[AgentTurn]], ctx: DebateContext) -> LeaderDecision: ...


class UniformSelector:
    """Unweighted consensus; the group with the most valid agents leads
    (ties -> configured group order)."""

    def select(self, round_no: int, group_outputs: dict[str, list[AgentTurn]], ctx: DebateContext) -> LeaderDecision:
        consensus: dict[str, Plan] = {}
        scores: dict[str, float] = {}
        for g, turns in group_outputs.items():
            if turns:
                consensus[g] = aggregate_plans([t.output.plan for t in turns])
                scores[g] = float(len(turns))
        if not consensus:
            raise DebateFailed(f"round {round_no}: no valid outputs")
        leader = max(consensus, key=lambda g: (scores[g], -list(group_outputs).index(g)))
        return LeaderDecision(group=leader, scores=scores, consensus=consensus)


def _call_agent(
    llm: LLMClient, config: DebateConfig, group: str, persona: Persona, round_no: int, system: str, prompt: str
) -> AgentTurn:
    turn = AgentTurn(group=group, persona=persona, round_no=round_no, prompt_hash=prompts.prompt_hash(system, prompt))
    tag = {"group": group, "persona": persona.value, "round": round_no}

    def ask(p: str, repair: bool) -> str:
        req = LLMRequest(system=system, prompt=p, temperature=config.temperature, max_tokens=config.max_tokens, tag={**tag, "repair": repair})
        return llm.complete(req).text

    try:
        raw = ask(prompt, False)
    except EgressPolicyViolation:
        raise  # a leak is never a "dead agent": abort the whole debate (fail closed)
    except Exception as e:  # noqa: BLE001 - any provider failure kills this turn only
        turn.error = f"llm: {type(e).__name__}: {e}"[:500]
        return turn
    turn.raw = raw
    try:
        turn.output = parse_agent_output(raw)
        return turn
    except ValueError as first:
        err = str(first)
    try:
        raw2 = ask(f"{prompt}\n\nYOUR PREVIOUS REPLY:\n{raw[:3000]}\n\n{prompts.render_repair(err)}", True)
    except EgressPolicyViolation:
        raise
    except Exception as e:  # noqa: BLE001
        turn.error = f"llm(repair): {type(e).__name__}: {e}"[:500]
        return turn
    turn.raw, turn.repaired = raw2, True
    try:
        turn.output = parse_agent_output(raw2)
    except ValueError as second:
        turn.error = f"unparsable after repair: {second}"[:500]
    return turn


def run_debate(
    ctx: DebateContext,
    llm: LLMClient,
    config: DebateConfig | None = None,
    selector: LeaderSelector | None = None,
) -> DebateResult:
    config = config or DebateConfig()
    selector = selector or UniformSelector()
    order = config.group_order()
    all_turns: list[AgentTurn] = []
    leaders: list[LeaderDecision] = []
    prev: dict[tuple[str, Persona], AgentTurn] = {}

    for round_no in range(1, config.rounds + 1):
        specs: list[tuple[str, Persona, str, str]] = []
        for group, personas in config.groups.items():
            for persona in personas:
                system = prompts.system_prompt(persona)
                if round_no == 1:
                    body = prompts.render_round1(ctx, persona, config.rounds)
                else:
                    own = prev.get((group, persona))
                    mates = [
                        (p, prev[(group, p)].output)
                        for p in personas
                        if p != persona and (group, p) in prev and prev[(group, p)].output
                    ]
                    lead = leaders[-1]
                    lead_outs = [t.output for (g, _), t in prev.items() if g == lead.group and t.output]
                    body = prompts.render_round_n(
                        ctx, persona, round_no, config.rounds, own.output if own else None, mates, lead, lead_outs
                    )
                specs.append((group, persona, system, body))

        with ThreadPoolExecutor(max_workers=config.max_workers) as pool:
            turns = list(pool.map(lambda s: _call_agent(llm, config, s[0], s[1], round_no, s[2], s[3]), specs))

        if not any(t.valid for t in turns):
            raise DebateFailed(f"round {round_no}: zero valid agent outputs")
        all_turns += turns
        prev = {(t.group, t.persona): t for t in turns}
        group_outputs = {g: [t for t in turns if t.group == g and t.valid] for g in order}
        leaders.append(selector.select(round_no, group_outputs, ctx))

    final = leaders[-1]
    return DebateResult(
        config=config,
        prompt_version=prompts.PROMPT_VERSION,
        snapshot_hash=ctx.snapshot_hash,
        turns=all_turns,
        leaders=leaders,
        final_leader_group=final.group,
        final_consensus=final.consensus,
        degraded=any(not t.valid for t in all_turns),
    )


def save_debate_run(db: Session, hurdle_id: str, snapshot: EvidenceSnapshot, result: DebateResult, llm_name: str) -> DebateRun:
    run = DebateRun(
        hurdle_id=hurdle_id,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        prompt_version=result.prompt_version,
        config=result.config.model_dump(mode="json"),
        llm_name=llm_name,
        final_leader_group=result.final_leader_group,
        final_consensus={g: p.model_dump(mode="json") for g, p in result.final_consensus.items()},
        leaders=[d.model_dump(mode="json") for d in result.leaders],
        degraded=result.degraded,
    )
    run.turns = [
        DebateTurn(
            seq=n,
            group_name=t.group,
            persona=t.persona.value,
            round_no=t.round_no,
            prompt_hash=t.prompt_hash,
            raw=t.raw,
            output=t.output.model_dump(mode="json") if t.output else None,
            error=t.error,
            repaired=t.repaired,
        )
        for n, t in enumerate(result.turns)
    ]
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def load_debate_result(run: DebateRun) -> DebateResult:
    turns = sorted(run.turns, key=lambda t: t.seq)
    return DebateResult(
        config=DebateConfig.model_validate(run.config),
        prompt_version=run.prompt_version,
        snapshot_hash=run.snapshot_hash,
        turns=[
            AgentTurn(
                group=t.group_name,
                persona=Persona(t.persona),
                round_no=t.round_no,
                prompt_hash=t.prompt_hash,
                raw=t.raw,
                output=AgentOutput.model_validate(t.output) if t.output else None,
                error=t.error,
                repaired=t.repaired,
            )
            for t in turns
        ],
        leaders=[LeaderDecision.model_validate(d) for d in run.leaders],
        final_leader_group=run.final_leader_group,
        final_consensus={g: Plan.model_validate(p) for g, p in run.final_consensus.items()},
        degraded=run.degraded,
    )
