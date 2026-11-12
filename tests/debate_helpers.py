"""Shared helpers for debate tests: scripted agent replies. No network, no real LLM."""
from __future__ import annotations

import json
import re

from app.debate.schemas import Plan

HANDLE_RE = re.compile(r"^\[(E\d+)\] source=", re.MULTILINE)


def plan(cash=25, liquid=25, fd=25, eq=25, rate=20, shift=0, buf=3) -> Plan:
    return Plan(
        allocation={"CASH_SAVINGS": cash, "LIQUID_FUND": liquid, "FIXED_DEPOSIT": fd, "EQUITY_INDEX": eq},
        monthly_saving_rate=rate,
        deadline_shift_months=shift,
        emergency_buffer_months=buf,
    )


def reply(p: Plan, claims: list[dict], summary: str = "ok", rebuttals: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "plan": p.model_dump(mode="json"),
            "claims": claims,
            "rebuttals": rebuttals or [],
            "summary": summary,
        }
    )


def fact(handles: list[str], figures: list[str] | None = None, text: str = "a sourced fact") -> dict:
    return {"kind": "FACT", "text": text, "evidence_ids": handles, "quoted_figures": figures or []}


def inference(text: str = "my own reasoning") -> dict:
    return {"kind": "INFERENCE", "text": text, "evidence_ids": [], "quoted_figures": []}


def handles_in(prompt: str) -> list[str]:
    return HANDLE_RE.findall(prompt)


def default_responder(req) -> str:
    """Deterministic valid reply: cites the first handle in the agent's pack,
    plan varies by persona so aggregation is non-trivial."""
    t = req.tag
    hs = handles_in(req.prompt)
    persona, group, rnd = t["persona"], t["group"], t["round"]
    p = {
        "saver": plan(20, 20, 50, 10, rate=30),
        "liquidity_hawk": plan(30, 50, 15, 5, rate=25, buf=6),
        "skeptic": plan(30, 30, 30, 10, rate=20, buf=4),
        "opportunity_seeker": plan(10, 20, 20, 50, rate=40),
        "contrarian": plan(40, 30, 25, 5, rate=15, buf=6),
    }[persona]
    claims = [fact(hs[:1]) if hs else inference(), inference(f"CLAIM-{group}-{persona}-r{rnd}")]
    return reply(p, claims, summary=f"SUMMARY-{group}-{persona}-r{rnd}")
