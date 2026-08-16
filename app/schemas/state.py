from decimal import Decimal

from pydantic import BaseModel


class StockHolding(BaseModel):
    symbol: str
    units: Decimal
    current_value: Decimal | None = None


class Loan(BaseModel):
    label: str
    outstanding: Decimal
    interest_rate: Decimal | None = None


class EMI(BaseModel):
    label: str
    amount: Decimal
    remaining_months: int | None = None


class Debt(BaseModel):
    label: str
    amount: Decimal


class InsurancePolicy(BaseModel):
    label: str
    premium: Decimal | None = None
    cover: Decimal | None = None


class FinancialState(BaseModel):
    """
    Derived projection of the ledger. Never edited directly.
    Reconstructed by folding all LedgerEntry.effect deltas in effective_date order.
    """
    cash: Decimal | None = None
    liquid_cash: Decimal | None = None
    savings: Decimal | None = None
    stocks: list[StockHolding] = []
    loans: list[Loan] = []
    emis: list[EMI] = []
    debts: list[Debt] = []
    insurance: list[InsurancePolicy] = []
    recurring_income: Decimal | None = None
    one_time_inflows: list[dict] = []
