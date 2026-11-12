"""Human-readable source labels, shown on every evidence line in the debate prompt.

The labels exist because small models read "DGS1" or "SPY" and describe them as
Indian bank rates or Indian equities. A label is PRESENTATION ONLY: it is added
when a prompt is rendered and never stored in, or hashed with, the snapshot
(snapshot content and snapshot_hash are unchanged).

Every FRED series and Alpha Vantage symbol used by app/evidence/lenses.py must
have an entry (tests/test_evidence_labels.py enforces both directions).
"""
from __future__ import annotations

import re

_FRED_Q = re.compile(r"^series:([A-Za-z0-9_]+)$")
_AV_SYMBOL = re.compile(r"(?:^|&)symbol=([A-Za-z0-9.\-]+)")

SERIES_LABELS: dict[str, str] = {
    # --- FRED (US Federal Reserve Economic Data): all US series ---
    "FRED:DGS1": "FRED DGS1 = US 1-year Treasury yield (a US government bond yield, NOT an Indian bank FD rate)",
    "FRED:CPIAUCSL": "FRED CPIAUCSL = US consumer price index, all urban consumers (US inflation, NOT Indian CPI)",
    "FRED:CPILFESL": "FRED CPILFESL = US core consumer price index excluding food and energy (US inflation, NOT Indian)",
    "FRED:PCEPI": "FRED PCEPI = US personal consumption expenditures price index (a US inflation measure, NOT Indian)",
    "FRED:FEDFUNDS": "FRED FEDFUNDS = US federal funds effective rate (the US central-bank policy rate, NOT the RBI repo rate)",
    "FRED:UNRATE": "FRED UNRATE = US unemployment rate (US labour market, NOT India's)",
    "FRED:ICSA": "FRED ICSA = US initial jobless claims, weekly (a US labour-market indicator, NOT Indian)",
    "FRED:A939RX0Q048SBEA": "FRED A939RX0Q048SBEA = US real GDP per capita, quarterly (US economic output, NOT India's)",
    "FRED:SP500": "FRED SP500 = S&P 500 stock index level (a US equity index, NOT an Indian index)",
    "FRED:RECPROUSM156N": "FRED RECPROUSM156N = smoothed US recession probability (a model estimate for the US only, NOT a forecast for India)",
    "FRED:T10Y2Y": "FRED T10Y2Y = US 10-year minus 2-year Treasury yield spread (a US yield-curve indicator, NOT an Indian one)",
    # --- Alpha Vantage: US-listed exchange-traded funds, prices in US dollars ---
    "ALPHA_VANTAGE:SPY": "Alpha Vantage SPY = SPDR S&P 500 ETF monthly prices (a US-listed fund tracking US large caps, NOT an Indian equity index)",
    "ALPHA_VANTAGE:INDA": "Alpha Vantage INDA = iShares MSCI India ETF monthly prices (a US-listed fund holding Indian equities, priced in US dollars, NOT the Indian index itself)",
}

LOCAL_LABEL = (
    "LOCAL_REGULATORY corpus = orientation summary of Indian rules (not legal text; verify against the primary source)"
)


def label_key(source: str, query: str) -> str | None:
    """'FRED:DGS1' / 'ALPHA_VANTAGE:SPY' for the series a query fetches; None for local text queries."""
    if source == "FRED":
        m = _FRED_Q.match(query.strip())
        return f"FRED:{m.group(1)}" if m else None
    if source == "ALPHA_VANTAGE":
        m = _AV_SYMBOL.search(query)
        return f"ALPHA_VANTAGE:{m.group(1).upper()}" if m else None
    return None


def label_for(source: str, query: str) -> str:
    if source == "LOCAL_REGULATORY":
        return LOCAL_LABEL
    key = label_key(source, query)
    if key in SERIES_LABELS:
        return SERIES_LABELS[key]
    # Fail loud-ish: never invent a description for a source we have not labelled.
    return f"{source} {query} = (no label registered; do not assume what this source measures)"
