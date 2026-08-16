"""Field classification registry — the single source of truth for what
tier/kind a field belongs to. Implemented as an explicit table, not
scattered conditionals, so it is auditable and testable in one place.

DEFAULT DENY: any field name not listed here classifies as
TIER_HEALTH_FAMILY/FREEFORM — the most restrictive class — so a new field
added upstream (e.g. by a future Layer 1 change) fails closed, not open.
See test_default_deny_unknown_field_is_health_family.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.enums import FieldKind, SensitivityTier, VaultFieldType


@dataclass(frozen=True)
class FieldClassification:
    tier: SensitivityTier
    kind: FieldKind
    field_type: VaultFieldType | None = None  # set iff kind is IDENTITY


DEFAULT_DENY = FieldClassification(SensitivityTier.TIER_HEALTH_FAMILY, FieldKind.FREEFORM)


def _identity(field_type: VaultFieldType) -> FieldClassification:
    return FieldClassification(SensitivityTier.TIER_STANDARD, FieldKind.IDENTITY, field_type)


_MAGNITUDE = FieldClassification(SensitivityTier.TIER_STANDARD, FieldKind.MAGNITUDE)
_STRUCTURAL = FieldClassification(SensitivityTier.TIER_STANDARD, FieldKind.STRUCTURAL)
_HEALTH_FAMILY = FieldClassification(SensitivityTier.TIER_HEALTH_FAMILY, FieldKind.FREEFORM)

CLASSIFICATION: dict[str, FieldClassification] = {
    # --- IDENTITY (TIER_STANDARD) → tokenized ---
    "name": _identity(VaultFieldType.NAME),
    "full_name": _identity(VaultFieldType.NAME),
    "holder_name": _identity(VaultFieldType.NAME),
    "pan": _identity(VaultFieldType.PAN),
    "pan_number": _identity(VaultFieldType.PAN),
    "account": _identity(VaultFieldType.ACCOUNT),
    "account_no": _identity(VaultFieldType.ACCOUNT),
    "account_number": _identity(VaultFieldType.ACCOUNT),
    "employer": _identity(VaultFieldType.EMPLOYER),
    "employer_name": _identity(VaultFieldType.EMPLOYER),
    "company": _identity(VaultFieldType.EMPLOYER),
    "phone": _identity(VaultFieldType.PHONE),
    "mobile": _identity(VaultFieldType.PHONE),
    "phone_number": _identity(VaultFieldType.PHONE),
    "email": _identity(VaultFieldType.EMAIL),
    "email_address": _identity(VaultFieldType.EMAIL),
    "address": _identity(VaultFieldType.ADDRESS),
    "residential_address": _identity(VaultFieldType.ADDRESS),

    # --- MAGNITUDE (TIER_STANDARD) → cleartext to OPTIMIZER, banded to DEBATE_AGENT ---
    # Mirrors app/services/state.py's _SCALAR_FIELDS/_LIST_FIELDS plus primitive amounts.
    "cash": _MAGNITUDE,
    "liquid_cash": _MAGNITUDE,
    "savings": _MAGNITUDE,
    "recurring_income": _MAGNITUDE,
    # Layer 1's _LIST_FIELDS (app/services/state.py) — classified MAGNITUDE so
    # the list itself is not dropped wholesale; individual item fields (e.g.
    # StockHolding.current_value) are classified again on the recursive walk.
    "stocks": _MAGNITUDE,
    "loans": _MAGNITUDE,
    "emis": _MAGNITUDE,
    "debts": _MAGNITUDE,
    "insurance": _MAGNITUDE,
    "one_time_inflows": _MAGNITUDE,
    "amount": _MAGNITUDE,
    "target_amount": _MAGNITUDE,
    "min_liquidity": _MAGNITUDE,
    "delta": _MAGNITUDE,
    "outstanding": _MAGNITUDE,
    "premium": _MAGNITUDE,
    "cover": _MAGNITUDE,
    "current_value": _MAGNITUDE,
    # NOT _MAGNITUDE: band_amount() formats every MAGNITUDE value as a rupee
    # range ("₹70k–80k"), which is wrong for a share count or a percentage.
    # Neither is a rupee figure, and low-precision disclosure of either is
    # not the re-identification risk this layer defends against.
    "units": _STRUCTURAL,
    "interest_rate": _STRUCTURAL,

    # --- STRUCTURAL (TIER_STANDARD) → pass through everywhere ---
    "type": _STRUCTURAL,
    "cadence": _STRUCTURAL,
    "op": _STRUCTURAL,
    "field": _STRUCTURAL,
    "priority": _STRUCTURAL,
    "symbol": _STRUCTURAL,
    "effective_date": _STRUCTURAL,
    "due_date": _STRUCTURAL,
    "deadline": _STRUCTURAL,
    "start": _STRUCTURAL,
    "end": _STRUCTURAL,
    "date": _STRUCTURAL,
    "new_date": _STRUCTURAL,
    "goal_ref": _STRUCTURAL,
    "remaining_months": _STRUCTURAL,
    "source": _STRUCTURAL,

    # --- TIER_HEALTH_FAMILY ---
    "medical_conditions": _HEALTH_FAMILY,
    "dependents": _HEALTH_FAMILY,
    "family_commitments": _HEALTH_FAMILY,
    "obligations": _HEALTH_FAMILY,
    "relation": _HEALTH_FAMILY,
    "age": _HEALTH_FAMILY,
    "notes": _HEALTH_FAMILY,
    "diagnosis": _HEALTH_FAMILY,
    "condition": _HEALTH_FAMILY,

    # NOTE: `label`, `description`, `reason`, `raw_description` are deliberately
    # NOT listed. They are free text that routinely carries an employer or bank
    # name ("HDFC home loan", "Got a raise at Acme"). They fall through to
    # DEFAULT_DENY and are dropped for agent destinations — default-deny earning
    # its keep on real Layer 1 fields, not a synthetic test field.
}


def classify(field_name: str) -> FieldClassification:
    return CLASSIFICATION.get(field_name.strip().lower(), DEFAULT_DENY)
