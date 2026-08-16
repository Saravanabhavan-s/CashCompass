from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ledger import LedgerEntry
from app.schemas.state import FinancialState

_SCALAR_FIELDS = {"cash", "liquid_cash", "savings", "recurring_income"}
_LIST_FIELDS = {"stocks", "loans", "emis", "debts", "insurance", "one_time_inflows"}


def _fold_entries(entries: list) -> FinancialState:
    """
    Pure function: fold a sequence of LedgerEntry-like objects into a FinancialState.

    Processes entries in order (caller is responsible for ordering by effective_date).
    Each entry's `effect` list is a sequence of EffectDelta dicts:
      { field, op ("set"|"add"|"append"), value, account? }

    Corrections are expressed as new entries (e.g. op="add", value=-X) — never
    by mutating prior entries.
    """
    state: dict = {
        "cash": None,
        "liquid_cash": None,
        "savings": None,
        "stocks": [],
        "loans": [],
        "emis": [],
        "debts": [],
        "insurance": [],
        "recurring_income": None,
        "one_time_inflows": [],
    }

    for entry in entries:
        effects = entry.effect if isinstance(entry.effect, list) else []
        for delta in effects:
            field = delta.get("field")
            op = delta.get("op")
            value = delta.get("value")

            if field not in state or value is None:
                continue

            if field in _SCALAR_FIELDS:
                d_value = Decimal(str(value))
                if op == "set":
                    state[field] = d_value
                elif op == "add":
                    state[field] = (state[field] or Decimal("0")) + d_value
            elif field in _LIST_FIELDS:
                if op == "append":
                    state[field].append(value)
                elif op == "set" and isinstance(value, list):
                    state[field] = value

    return FinancialState(**state)


def rebuild_state_from_ledger(db: Session) -> FinancialState:
    entries = list(
        db.scalars(
            select(LedgerEntry).order_by(LedgerEntry.effective_date, LedgerEntry.created_at)
        ).all()
    )
    return _fold_entries(entries)
