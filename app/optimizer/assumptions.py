"""Modelling assumptions. EVERY default here is illustrative -- replace with
verified rates before relying on any output."""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict

from app.debate.schemas import Instrument

ASSUMPTIONS_LABEL = "illustrative, replace with verified rates"


class Scenario(str, Enum):
    BASE = "BASE"
    MARKET_STRESS = "MARKET_STRESS"
    INCOME_STRESS = "INCOME_STRESS"


class Assumptions(BaseModel):
    model_config = ConfigDict(frozen=True)

    # annual percentage rates, compounded monthly
    cash_rate: Decimal = Decimal("3.0")
    liquid_rate: Decimal = Decimal("6.0")
    fd_rate: Decimal = Decimal("6.5")
    equity_rate: Decimal = Decimal("11.0")
    fd_exit_penalty: Decimal = Decimal("0.03")  # fraction of gross FD withdrawn
    market_shock: Decimal = Decimal("0.30")  # EQUITY drop in MARKET_STRESS
    income_stress: Decimal = Decimal("0.70")  # income multiplier in INCOME_STRESS
    label: str = ASSUMPTIONS_LABEL

    def with_overrides(self, **kw) -> "Assumptions":
        return self.model_copy(update={k: Decimal(str(v)) if k != "label" else v for k, v in kw.items()})

    def annual_rate(self, inst: Instrument) -> Decimal:
        return {
            Instrument.CASH_SAVINGS: self.cash_rate,
            Instrument.LIQUID_FUND: self.liquid_rate,
            Instrument.FIXED_DEPOSIT: self.fd_rate,
            Instrument.EQUITY_INDEX: self.equity_rate,
        }[inst]

    def monthly_rate(self, inst: Instrument) -> Decimal:
        return self.annual_rate(inst) / Decimal(100) / Decimal(12)

    def describe(self) -> str:
        return (
            f"annual rates ({self.label}): CASH {self.cash_rate}%, LIQUID {self.liquid_rate}%, "
            f"FD {self.fd_rate}%, EQUITY {self.equity_rate}%; FD exit penalty {self.fd_exit_penalty * 100}%; "
            f"MARKET_STRESS equity shock -{self.market_shock * 100}%; INCOME_STRESS income x {self.income_stress}"
        )
