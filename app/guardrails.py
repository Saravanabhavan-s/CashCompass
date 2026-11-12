"""Layer 3e guardrails and confidence. Pure, deterministic, Decimal where it
touches scores. BLOCK findings make a recommendation un-confirmable; WARN
findings (and LOW confidence) require explicit acknowledgement."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Iterable

from pydantic import BaseModel

DISCLAIMER = "Decision support, not financial advice."
HALLUCINATION_WARN_ABOVE = 0.34
LOW_BELOW = Decimal("0.5")
HIGH_ABOVE = Decimal("0.75")

INJECTION_RE = re.compile(
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)"
    r"|system\s+prompt|disregard|you\s+are\s+now|new\s+instructions"
    r"|override\s+(the\s+)?(rules|instructions|policy)|reveal\s+(your\s+)?(prompt|instructions)",
    re.IGNORECASE,
)


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    BLOCK = "BLOCK"


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str


class GuardrailReport(BaseModel):
    findings: list[Finding]
    blocked: bool
    needs_ack: bool
    disclaimer: str = DISCLAIMER

    def codes(self, severity: Severity | None = None) -> list[str]:
        return [f.code for f in self.findings if severity is None or f.severity == severity]


def scan_injection(texts: Iterable[str]) -> list[str]:
    """Return the distinct matched phrases (lower-cased)."""
    hits: list[str] = []
    for t in texts:
        for m in INJECTION_RE.finditer(t or ""):
            phrase = re.sub(r"\s+", " ", m.group(0).lower())
            if phrase not in hits:
                hits.append(phrase)
    return hits


def run_guardrails(
    *,
    leak: str | None = None,  # description of a scanner hit, if any
    infeasible: bool = False,
    policy_violations: Iterable[str] = (),
    texts: Iterable[str] = (),  # evidence + agent text to scan for injection
    grounded_facts: int | None = None,  # None = no debate ran (skip the check)
    hallucination_rate: float | None = None,
    fabrication_rate: float | None = None,  # split of the strict rate, for the message only
    misattribution_rate: float | None = None,
    living_expense_missing: bool = False,
    pool_empty: bool = False,
    failed_turns: int = 0,
    repaired: bool = False,
    low_diversity: bool = False,
) -> GuardrailReport:
    f: list[Finding] = []
    B, W, I = Severity.BLOCK, Severity.WARN, Severity.INFO

    if leak:
        f.append(Finding(code="LEAK", severity=B, message=f"Egress scanner blocked outgoing model input: {leak}"))
    if infeasible:
        f.append(Finding(code="INFEASIBLE", severity=B, message="No plan within policy can reach the goal; see the quantified gap."))
    pv = list(policy_violations)
    if pv:
        f.append(Finding(code="POLICY_VIOLATION", severity=B, message="Chosen plan violates hard policy: " + ", ".join(pv)))
    if grounded_facts is not None and grounded_facts == 0:
        f.append(Finding(code="NO_GROUNDED_EVIDENCE", severity=B, message="No grounded FACT claim supports the chosen plan."))
    hits = scan_injection(texts)
    if hits:
        f.append(Finding(code="PROMPT_INJECTION", severity=W, message="Instruction-like text found in evidence or agent output: " + "; ".join(hits)))
    if hallucination_rate is not None and hallucination_rate > HALLUCINATION_WARN_ABOVE:
        split = (
            f" (fabricated {fabrication_rate:.2%}, misattributed {misattribution_rate:.2%})"
            if fabrication_rate is not None and misattribution_rate is not None
            else ""
        )
        f.append(Finding(code="HIGH_HALLUCINATION", severity=W, message=f"Hallucination rate {hallucination_rate:.2%}{split} exceeds {HALLUCINATION_WARN_ABOVE:.0%}."))
    missing = []
    if living_expense_missing:
        missing.append("monthly living expense not provided (surplus is an upper bound)")
    if pool_empty:
        missing.append("opening savings pool is empty")
    if missing:
        f.append(Finding(code="INCOMPLETE_INPUT", severity=W, message="; ".join(missing)))
    if failed_turns > 0:
        f.append(Finding(code="DEGRADED_DEBATE", severity=W, message=f"{failed_turns} agent turn(s) failed and were excluded."))
    if low_diversity:
        f.append(Finding(code="LOW_DIVERSITY", severity=I, message="Half or more of the final-round claims are identical across agents; agreement is capped at 0.8 because the debate may have converged by copying."))
    if repaired:
        f.append(Finding(code="PLAN_REPAIRED", severity=I, message="No debated plan was feasible; the optimizer adjusted the nearest feasible plan."))

    return GuardrailReport(
        findings=f,
        blocked=any(x.severity == B for x in f),
        needs_ack=any(x.severity == W for x in f),
    )


# ---- confidence -------------------------------------------------------------
WEIGHTS = {
    "robustness": Decimal("0.30"),
    "agreement": Decimal("0.25"),
    "evidence_quality": Decimal("0.25"),
    "completeness": Decimal("0.20"),
}
_Q = Decimal("0.0001")


def _q(x: Decimal) -> Decimal:
    return x.quantize(_Q, rounding=ROUND_HALF_EVEN)


MIN_EVIDENCE_FOR_HIGH = Decimal("0.5")
LOW_DIVERSITY_AGREEMENT_CAP = Decimal("0.8")


class Confidence(BaseModel):
    value: Decimal  # the numeric score is never altered by the band cap
    band: str  # LOW | MEDIUM | HIGH
    components: dict[str, Decimal]  # raw 0-1 inputs
    weights: dict[str, Decimal] = WEIGHTS
    band_cap_reasons: list[str] = []  # why a HIGH score was shown as MEDIUM


def band_for(value: Decimal) -> str:
    if value < LOW_BELOW:
        return "LOW"
    if value > HIGH_ABOVE:
        return "HIGH"
    return "MEDIUM"


def compute_confidence(
    scenarios_feasible: int,
    agreement: float | Decimal,
    evidence_quality: float | Decimal,
    complete: bool,
    has_warn: bool = False,
    low_diversity: bool = False,
) -> Confidence:
    """0.30 x robustness (scenarios_feasible/3) + 0.25 x agreement +
    0.25 x evidence quality of the agents behind the plan + 0.20 x completeness
    (inputs present AND no failed turns).

    When the debate shows LOW_DIVERSITY the agreement component is capped at 0.8
    (identical claims are copying, not independent agreement).

    The numeric value is kept as is, but the BAND may be HIGH only if there are
    no WARN findings (`has_warn`) and evidence_quality >= 0.5; otherwise a score
    above 0.75 is shown as MEDIUM, with the reasons in `band_cap_reasons`."""
    raw = {
        "robustness": Decimal(scenarios_feasible) / Decimal(3),
        "agreement": min(Decimal(str(agreement)), LOW_DIVERSITY_AGREEMENT_CAP) if low_diversity else Decimal(str(agreement)),
        "evidence_quality": Decimal(str(evidence_quality)),
        "completeness": Decimal(1) if complete else Decimal(0),
    }
    value = _q(sum((WEIGHTS[k] * v for k, v in raw.items()), Decimal(0)))  # from unrounded inputs
    comps = {k: _q(v) for k, v in raw.items()}
    band, reasons = band_for(value), []
    if band == "HIGH":
        if has_warn:
            reasons.append("WARN_FINDINGS")
        if raw["evidence_quality"] < MIN_EVIDENCE_FOR_HIGH:
            reasons.append("LOW_EVIDENCE_QUALITY")
        if reasons:
            band = "MEDIUM"
    return Confidence(value=value, band=band, components=comps, band_cap_reasons=reasons)
