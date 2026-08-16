"""
Proves rebuild_state_from_ledger()/_fold_entries() need NO changes for
Layer 2: financial magnitudes are never tokenized (app/vault/egress.py's
classification registry), so a tokenized ledger folds to a byte-identical
FinancialState. Pure — no DB required.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.services.state import _fold_entries


@dataclass
class MockTokenizedEntry:
    """Duck-type of LedgerEntry, but carrying a tokenized `description` —
    proving _fold_entries never reads description at all, so tokenizing it
    (app/services/ledger.py:append_entry_tokenized) cannot affect the fold."""

    effect: list
    description: str = "TKN_NOTE_8f3a1c2b4d5e6f70"
    effective_date: date = field(default_factory=date.today)


def test_fold_is_unchanged_by_tokenized_descriptions():
    entries_with_token = [
        MockTokenizedEntry([{"field": "savings", "op": "set", "value": 800000.0}], description="TKN_NOTE_aaaa")
    ]
    entries_plain = [
        MockTokenizedEntry([{"field": "savings", "op": "set", "value": 800000.0}], description="plain text note")
    ]
    state_tokenized = _fold_entries(entries_with_token)
    state_plain = _fold_entries(entries_plain)
    assert state_tokenized == state_plain
    assert state_tokenized.savings == Decimal("800000")


def test_folded_state_contains_no_token_strings():
    entries = [
        MockTokenizedEntry(
            [
                {"field": "savings", "op": "set", "value": 800000.0},
                {"field": "recurring_income", "op": "set", "value": 75000.0},
                {"field": "stocks", "op": "append", "value": {"symbol": "INFY", "units": 10}},
            ],
            description="TKN_NOTE_8f3a1c2b4d5e6f70",
        )
    ]
    state = _fold_entries(entries)
    dumped = state.model_dump_json()
    assert "TKN_" not in dumped
