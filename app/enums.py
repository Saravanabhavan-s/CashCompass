from enum import Enum


class PrimitiveType(str, Enum):
    ONE_TIME_OUTFLOW = "ONE_TIME_OUTFLOW"
    RECURRING_OUTFLOW = "RECURRING_OUTFLOW"
    ONE_TIME_INFLOW = "ONE_TIME_INFLOW"
    INFLOW_CHANGE = "INFLOW_CHANGE"
    HORIZON_SHIFT = "HORIZON_SHIFT"
    RISK_CONSTRAINT = "RISK_CONSTRAINT"
    GOAL = "GOAL"


class Cadence(str, Enum):
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"


class LedgerEntryType(str, Enum):
    DECISION = "DECISION"
    COMMITMENT = "COMMITMENT"
    STATE_SNAPSHOT = "STATE_SNAPSHOT"


class LedgerSource(str, Enum):
    USER_STATED = "USER_STATED"
    AGENT_SUGGESTED_USER_CONFIRMED = "AGENT_SUGGESTED_USER_CONFIRMED"
    SYSTEM_DERIVED = "SYSTEM_DERIVED"


class SensitivityTier(str, Enum):
    # TIER_STANDARD is the Layer 2 (spec) name; TIER_FINANCIAL_PII is the Layer 1
    # name for the same tier, retained as an alias so nothing upstream breaks.
    TIER_STANDARD = "TIER_STANDARD"
    TIER_FINANCIAL_PII = "TIER_STANDARD"
    TIER_HEALTH_FAMILY = "TIER_HEALTH_FAMILY"


class VaultFieldType(str, Enum):
    # Canonical identity set (Layer 2 spec §5).
    NAME = "NAME"
    PAN = "PAN"
    ACCOUNT = "ACCOUNT"
    EMPLOYER = "EMPLOYER"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    ADDRESS = "ADDRESS"
    # Extension for TIER_HEALTH_FAMILY fields (SensitiveProfile has no field
    # matching the seven identity types above).
    MEDICAL = "MEDICAL"
    RELATION = "RELATION"
    NOTE = "NOTE"


class EgressDestination(str, Enum):
    DEBATE_AGENT = "DEBATE_AGENT"
    OPTIMIZER = "OPTIMIZER"
    USER_FACING = "USER_FACING"


class FieldKind(str, Enum):
    # Second classification axis: SensitivityTier alone can't express that,
    # within TIER_STANDARD, identity is tokenized but magnitudes stay cleartext.
    IDENTITY = "IDENTITY"
    MAGNITUDE = "MAGNITUDE"
    STRUCTURAL = "STRUCTURAL"
    FREEFORM = "FREEFORM"
