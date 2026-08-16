from decimal import Decimal

from app.enums import EgressDestination
from app.vault.classification import DEFAULT_DENY, classify
from app.vault.egress import (
    abstract_health_family,
    apply_egress,
    band_amount,
    band_months,
    generalize_for_debate,
)
from app.vault.scanner import scan


def test_band_75000_is_70k_to_80k():
    assert band_amount(75000) == "₹70k–80k"


def test_band_800000_is_8_to_9_lakh():
    assert band_amount(800000) == "₹8–9 lakh"


def test_band_never_contains_the_exact_value():
    for amount in [75000, 800000, 12345, 999, 1500000]:
        banded = band_amount(amount)
        assert str(amount) not in banded


def test_band_months_8_is_6_to_12():
    assert band_months(8) == "6–12 months"


def test_band_amount_zero():
    assert band_amount(0) == "₹0"


def test_band_amount_none():
    assert band_amount(None) is None


def test_default_deny_unknown_field_is_health_family():
    assert classify("some_field_nobody_added") == DEFAULT_DENY


def test_unclassified_field_not_emitted_to_debate_agent():
    payload = {"label": "HDFC home loan — private note about my mother", "amount": 5000}
    projected = apply_egress(payload, EgressDestination.DEBATE_AGENT)
    assert "label" not in projected
    assert "amount" in projected


def test_debate_agent_receives_tokens_for_identity_fields():
    payload = {"name": "TKN_NAME_abc1234567890abc"}
    projected = apply_egress(payload, EgressDestination.DEBATE_AGENT)
    assert projected["name"] == "TKN_NAME_abc1234567890abc"


def test_debate_agent_receives_zero_exact_financial_magnitudes():
    payload = {"savings": Decimal("800000"), "recurring_income": Decimal("75000")}
    projected = apply_egress(payload, EgressDestination.DEBATE_AGENT)
    leaks = scan(projected, forbidden_values=[800000, 75000])
    assert leaks == []
    assert projected["savings"] == "₹8–9 lakh"
    assert projected["recurring_income"] == "₹70k–80k"


def test_debate_agent_receives_zero_health_family_literals():
    payload = {
        "savings": Decimal("800000"),
        "medical_conditions": ["chemotherapy for user's mother"],
    }
    projected = apply_egress(payload, EgressDestination.DEBATE_AGENT)
    leaks = scan(projected, forbidden_substrings=["chemotherapy"])
    assert leaks == []
    assert "medical_conditions" not in projected


def test_debate_agent_receives_zero_health_family_tokens():
    payload = {"medical_conditions": ["TKN_MEDICAL_abc1234567890abc"]}
    projected = apply_egress(payload, EgressDestination.DEBATE_AGENT)
    leaks = scan(projected, forbid_token_prefixes=["MEDICAL", "RELATION", "NOTE"])
    assert leaks == []
    assert "medical_conditions" not in projected


def test_optimizer_receives_exact_magnitudes():
    payload = {"savings": Decimal("800000")}
    projected = apply_egress(payload, EgressDestination.OPTIMIZER)
    assert projected["savings"] == Decimal("800000")


def test_optimizer_receives_zero_health_family_literals():
    payload = {"savings": Decimal("800000"), "obligations": ["private family matter"]}
    projected = apply_egress(payload, EgressDestination.OPTIMIZER)
    assert "obligations" not in projected
    leaks = scan(projected, forbidden_substrings=["private family matter"])
    assert leaks == []


def test_generalize_for_debate_bands_state():
    from app.schemas.state import FinancialState

    state = FinancialState(savings=Decimal("800000"), recurring_income=Decimal("75000"))
    generalized = generalize_for_debate(state)
    assert generalized["savings"] == "₹8–9 lakh"
    assert generalized["recurring_income"] == "₹70k–80k"


def test_health_family_reduces_to_recurring_outflow_and_risk_constraint():
    items = [
        {
            "amount": 18000,
            "cadence": "MONTHLY",
            "duration_months": 14,
            "reason_code": "ongoing_care",
            "min_liquidity": 200000,
            "start": "2026-01-01",
        }
    ]
    primitives = abstract_health_family(items)
    types = {p["type"] for p in primitives}
    assert types == {"RECURRING_OUTFLOW", "RISK_CONSTRAINT"}
    outflow = next(p for p in primitives if p["type"] == "RECURRING_OUTFLOW")
    assert outflow["amount"] == 18000
    assert outflow["end"] == "2027-03-01"
    constraint = next(p for p in primitives if p["type"] == "RISK_CONSTRAINT")
    assert constraint["reason"] == "ongoing_care"


def test_risk_constraint_reason_is_allowlisted_code_not_free_text():
    items = [{"min_liquidity": 200000, "reason_code": "my mother has cancer"}]
    primitives = abstract_health_family(items)
    constraint = next(p for p in primitives if p["type"] == "RISK_CONSTRAINT")
    assert constraint["reason"] == "unclassified"
    assert "cancer" not in constraint["reason"]
