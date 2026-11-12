"""Persona query lenses.

A lens is a pure function (EvidenceQuestion) -> list[QueryStub] encoding one
persona's retrieval bias. This is the project's diversity axis (see README):
different personas retrieve genuinely different evidence over a shared
question -- not just different tone over the same facts. Lenses only generate
QUERIES; they never argue.

Query grammar per source:
  FRED              "series:<ID>"
  ALPHA_VANTAGE     "function=<F>&symbol=<S>"
  LOCAL_REGULATORY  free-text keywords matched against the bundled corpus
"""
from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, ConfigDict

from app.evidence.personas import Persona
from app.evidence.questions import EvidenceQuestion, EvidenceTopic
from app.evidence.sources import EvidenceSource

AV, FRED, LOCAL = EvidenceSource.ALPHA_VANTAGE, EvidenceSource.FRED, EvidenceSource.LOCAL_REGULATORY
T = EvidenceTopic


class QueryStub(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    source: EvidenceSource
    lens_tags: frozenset[Persona]


QueryLens = Callable[[EvidenceQuestion], list[QueryStub]]

_Q = tuple[EvidenceSource, str]

# persona -> topic -> [(source, query)]
_TABLE: dict[Persona, dict[EvidenceTopic, list[_Q]]] = {
    Persona.SAVER: {
        T.SAVINGS_INSTRUMENTS: [(LOCAL, "fixed deposit recurring deposit interest rate guaranteed return"), (FRED, "series:DGS1")],
        T.EMERGENCY_FUND: [(LOCAL, "guaranteed return fixed deposit emergency corpus")],
        T.COST_OF_LIVING: [(FRED, "series:CPIAUCSL"), (LOCAL, "PPF guaranteed return small savings inflation")],
        T.INCOME_SMOOTHING: [(LOCAL, "guaranteed return fixed deposit income stability senior citizen")],
        T.INVESTMENT_CAPACITY: [(LOCAL, "PPF guaranteed return tax saving 80C")],
        T.OPPORTUNITY_COST_OF_DELAY: [(FRED, "series:FEDFUNDS")],
    },
    Persona.LIQUIDITY_HAWK: {
        T.SAVINGS_INSTRUMENTS: [(LOCAL, "liquid fund exit load redemption withdrawal"), (LOCAL, "fixed deposit premature withdrawal penalty")],
        T.EMERGENCY_FUND: [(LOCAL, "emergency fund months of expenses sizing"), (LOCAL, "liquid fund exit load redemption withdrawal"), (LOCAL, "DICGC deposit insurance limit")],
        T.COST_OF_LIVING: [(FRED, "series:CPIAUCSL"), (LOCAL, "emergency fund months of expenses sizing")],
        T.INCOME_SMOOTHING: [(LOCAL, "emergency fund months of expenses sizing job loss"), (FRED, "series:UNRATE")],
        T.INVESTMENT_CAPACITY: [(LOCAL, "emergency fund months of expenses before investing")],
        T.OPPORTUNITY_COST_OF_DELAY: [(LOCAL, "liquid fund exit load redemption withdrawal")],
    },
    Persona.SKEPTIC: {
        T.SAVINGS_INSTRUMENTS: [(LOCAL, "RBI fixed deposit rate regulation source"), (LOCAL, "recent changes revisions small savings scheme rate"), (LOCAL, "DICGC deposit insurance limit")],
        T.EMERGENCY_FUND: [(LOCAL, "DICGC deposit insurance limit"), (LOCAL, "liquid fund SEBI rules still valid")],
        T.COST_OF_LIVING: [(LOCAL, "recent changes revisions CPI inflation methodology"), (FRED, "series:CPILFESL")],
        T.INCOME_SMOOTHING: [(LOCAL, "unemployment benefit scheme still valid eligibility revisions")],
        T.INVESTMENT_CAPACITY: [(LOCAL, "SEBI regulation mutual fund returns disclosure verification")],
        T.OPPORTUNITY_COST_OF_DELAY: [(LOCAL, "recent changes revisions small savings scheme rate")],
    },
    Persona.OPPORTUNITY_SEEKER: {
        T.SAVINGS_INSTRUMENTS: [(AV, "function=TIME_SERIES_MONTHLY&symbol=SPY"), (AV, "function=TIME_SERIES_MONTHLY&symbol=INDA")],
        T.EMERGENCY_FUND: [(LOCAL, "liquid fund yield versus savings account")],
        T.COST_OF_LIVING: [(AV, "function=TIME_SERIES_MONTHLY&symbol=SPY"), (FRED, "series:A939RX0Q048SBEA")],
        T.INCOME_SMOOTHING: [(AV, "function=TIME_SERIES_MONTHLY&symbol=SPY")],
        T.INVESTMENT_CAPACITY: [(AV, "function=TIME_SERIES_MONTHLY&symbol=INDA"), (AV, "function=TIME_SERIES_MONTHLY&symbol=SPY"), (LOCAL, "equity long term capital gains tax growth")],
        T.OPPORTUNITY_COST_OF_DELAY: [(AV, "function=TIME_SERIES_MONTHLY&symbol=SPY"), (FRED, "series:SP500")],
    },
    Persona.CONTRARIAN: {
        T.SAVINGS_INSTRUMENTS: [(FRED, "series:CPIAUCSL"), (FRED, "series:T10Y2Y"), (LOCAL, "fixed deposit real return after inflation tax TDS")],
        T.EMERGENCY_FUND: [(FRED, "series:RECPROUSM156N"), (LOCAL, "liquid fund risk credit risk debt fund warning")],
        T.COST_OF_LIVING: [(FRED, "series:RECPROUSM156N"), (FRED, "series:PCEPI")],
        T.INCOME_SMOOTHING: [(FRED, "series:RECPROUSM156N"), (FRED, "series:ICSA")],
        T.INVESTMENT_CAPACITY: [(FRED, "series:T10Y2Y"), (FRED, "series:RECPROUSM156N")],
        T.OPPORTUNITY_COST_OF_DELAY: [(FRED, "series:T10Y2Y"), (LOCAL, "market downturn risk warning")],
    },
}


def _make_lens(persona: Persona) -> QueryLens:
    table = _TABLE[persona]

    def lens(question: EvidenceQuestion) -> list[QueryStub]:
        return [
            QueryStub(query=q, source=s, lens_tags=frozenset({persona}))
            for s, q in table.get(question.topic, [])
        ]

    lens.__name__ = f"{persona.value}_lens"
    return lens


saver_lens = _make_lens(Persona.SAVER)
liquidity_hawk_lens = _make_lens(Persona.LIQUIDITY_HAWK)
skeptic_lens = _make_lens(Persona.SKEPTIC)
opportunity_seeker_lens = _make_lens(Persona.OPPORTUNITY_SEEKER)
contrarian_lens = _make_lens(Persona.CONTRARIAN)

ALL_LENSES: list[QueryLens] = [
    saver_lens,
    liquidity_hawk_lens,
    skeptic_lens,
    opportunity_seeker_lens,
    contrarian_lens,
]


def dedupe_merge_tags(stubs: list[QueryStub]) -> list[QueryStub]:
    """Same (source, query) from several lenses -> ONE stub carrying the union
    of every contributing persona tag. Output order is deterministic."""
    merged: dict[tuple[str, str], frozenset[Persona]] = {}
    for s in stubs:
        key = (s.source.value, s.query)
        merged[key] = merged.get(key, frozenset()) | s.lens_tags
    return [
        QueryStub(query=q, source=EvidenceSource(src), lens_tags=tags)
        for (src, q), tags in sorted(merged.items())
    ]
