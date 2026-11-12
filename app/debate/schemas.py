"""Debate decision space and data carriers. The LLM argues over a Plan; it never
computes money -- deterministic code (Layer 3d, later) does that."""
from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.evidence.personas import DEBATE_GROUPS, Persona


class Instrument(str, Enum):
    CASH_SAVINGS = "CASH_SAVINGS"
    LIQUID_FUND = "LIQUID_FUND"
    FIXED_DEPOSIT = "FIXED_DEPOSIT"
    EQUITY_INDEX = "EQUITY_INDEX"


INSTRUMENT_ORDER: tuple[Instrument, ...] = tuple(Instrument)


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)

    allocation: dict[Instrument, int]
    monthly_saving_rate: int = Field(ge=0, le=100)
    deadline_shift_months: int = Field(ge=0, le=12)
    emergency_buffer_months: int = Field(ge=0, le=12)

    @field_validator("allocation", mode="before")
    @classmethod
    def _fill_missing(cls, v):
        if not isinstance(v, dict):
            raise ValueError("allocation must be an object")
        out = {(k.value if isinstance(k, Instrument) else k): val for k, val in v.items()}
        for inst in INSTRUMENT_ORDER:
            out.setdefault(inst.value, 0)
        return out

    @field_validator("allocation")
    @classmethod
    def _check_alloc(cls, v):
        for inst, pct in v.items():
            if not 0 <= pct <= 100:
                raise ValueError(f"allocation {inst.value}={pct} outside 0-100")
        if sum(v.values()) != 100:
            raise ValueError(f"allocation must sum to exactly 100, got {sum(v.values())}")
        return v

    def key(self) -> tuple:
        return (
            tuple(self.allocation[i] for i in INSTRUMENT_ORDER),
            self.monthly_saving_rate,
            self.deadline_shift_months,
            self.emergency_buffer_months,
        )


class ClaimKind(str, Enum):
    FACT = "FACT"
    INFERENCE = "INFERENCE"


_HANDLE = re.compile(r"^[EC]\d+$")  # E<n> = snapshot evidence, C<n> = case file (only C1 exists)


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ClaimKind
    text: str = Field(max_length=500)
    evidence_ids: list[str] = Field(default_factory=list)
    quoted_figures: list[str] = Field(default_factory=list)

    @field_validator("evidence_ids")
    @classmethod
    def _handles(cls, v):
        for h in v:
            if not _HANDLE.match(h):
                raise ValueError(f"bad evidence handle {h!r}")
        return v


class Rebuttal(BaseModel):
    model_config = ConfigDict(frozen=True)

    target: Persona
    point: str = Field(max_length=500)


class AgentOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan: Plan
    claims: list[Claim] = Field(min_length=1, max_length=6)
    rebuttals: list[Rebuttal] = Field(default_factory=list, max_length=4)
    summary: str = Field(max_length=400)


class DebateConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    groups: dict[str, tuple[Persona, ...]] = Field(default_factory=lambda: dict(DEBATE_GROUPS))
    rounds: int = 3
    temperature: float = 0.3
    max_tokens: int = 1800
    max_workers: int = 6

    def group_order(self) -> list[str]:
        return list(self.groups)


class AgentTurn(BaseModel):
    group: str
    persona: Persona
    round_no: int
    prompt_hash: str
    raw: str = ""
    output: AgentOutput | None = None
    error: str | None = None
    repaired: bool = False

    @property
    def valid(self) -> bool:
        return self.output is not None


class LeaderDecision(BaseModel):
    group: str
    scores: dict[str, float]
    consensus: dict[str, Plan]


class DebateResult(BaseModel):
    config: DebateConfig
    prompt_version: str
    snapshot_hash: str
    turns: list[AgentTurn]
    leaders: list[LeaderDecision]  # one per round, index = round_no - 1
    final_leader_group: str
    final_consensus: dict[str, Plan]
    degraded: bool = False

    def turns_for(self, round_no: int) -> list[AgentTurn]:
        return [t for t in self.turns if t.round_no == round_no]
