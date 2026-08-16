"""
Financial state rebuild tests.

Tests the pure _fold_entries() function directly — no DB required.
This proves that state is a deterministic projection of ledger entries,
and that compensating entries correctly amend prior state.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.services.state import _fold_entries


@dataclass
class MockEntry:
    """Minimal duck-type of LedgerEntry for testing the pure fold function."""
    effect: list
    effective_date: date = None

    def __post_init__(self):
        if self.effective_date is None:
            self.effective_date = date.today()


def test_savings_set():
    entries = [MockEntry([{"field": "savings", "op": "set", "value": 800000.0}])]
    state = _fold_entries(entries)
    assert state.savings == Decimal("800000")


def test_recurring_income_set():
    entries = [MockEntry([{"field": "recurring_income", "op": "set", "value": 75000.0}])]
    state = _fold_entries(entries)
    assert state.recurring_income == Decimal("75000")


def test_add_accumulates():
    entries = [
        MockEntry([{"field": "savings", "op": "set", "value": 800000.0}]),
        MockEntry([{"field": "savings", "op": "add", "value": 200000.0}]),
    ]
    state = _fold_entries(entries)
    assert state.savings == Decimal("1000000")


def test_compensating_entry_reduces():
    """A negative add simulates a correcting/compensating entry."""
    entries = [
        MockEntry([{"field": "savings", "op": "set", "value": 800000.0}]),
        MockEntry([{"field": "savings", "op": "add", "value": -100000.0}]),
    ]
    state = _fold_entries(entries)
    assert state.savings == Decimal("700000")


def test_stock_append():
    stock = {"symbol": "INFY", "units": 50, "current_value": 800000.0}
    entries = [MockEntry([{"field": "stocks", "op": "append", "value": stock}])]
    state = _fold_entries(entries)
    assert len(state.stocks) == 1
    assert state.stocks[0].symbol == "INFY"
    assert state.stocks[0].units == Decimal("50")


def test_multiple_stocks_append():
    entries = [
        MockEntry([
            {"field": "stocks", "op": "append", "value": {"symbol": "INFY", "units": 50, "current_value": 800000.0}},
            {"field": "stocks", "op": "append", "value": {"symbol": "RELIANCE", "units": 20, "current_value": 600000.0}},
        ])
    ]
    state = _fold_entries(entries)
    symbols = [s.symbol for s in state.stocks]
    assert "INFY" in symbols
    assert "RELIANCE" in symbols
    assert len(state.stocks) == 2


def test_empty_ledger_returns_null_state():
    state = _fold_entries([])
    assert state.savings is None
    assert state.recurring_income is None
    assert state.stocks == []


def test_unknown_field_ignored():
    entries = [MockEntry([{"field": "NONEXISTENT", "op": "set", "value": 999}])]
    state = _fold_entries(entries)  # must not raise
    assert state.savings is None


def test_full_demo_state():
    """Mirrors what the seed script writes to the ledger."""
    entries = [
        MockEntry([
            {"field": "savings", "op": "set", "value": 800000.0},
            {"field": "recurring_income", "op": "set", "value": 75000.0},
            {"field": "stocks", "op": "append", "value": {"symbol": "INFY", "units": 50, "current_value": 800000.0}},
            {"field": "stocks", "op": "append", "value": {"symbol": "RELIANCE", "units": 20, "current_value": 600000.0}},
        ]),
    ]
    state = _fold_entries(entries)
    assert state.savings == Decimal("800000")
    assert state.recurring_income == Decimal("75000")
    assert len(state.stocks) == 2
