import enum


class EvidenceSource(str, enum.Enum):
    ALPHA_VANTAGE = "ALPHA_VANTAGE"
    FRED = "FRED"
    LOCAL_REGULATORY = "LOCAL_REGULATORY"
