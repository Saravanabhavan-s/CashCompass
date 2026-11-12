"""Eval dataset: a query is hurdle primitives + optional state + labelled
expectations. The user will hand-label 30-50 later; `eval/queries.sample.json`
ships 10 ILLUSTRATIVE ones.

Date fields (due_date, deadline, date, start, end, new_date) may be written as
"+<N>d", meaning as_of + N days, so a dataset never goes stale.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.primitives import Primitive
from app.schemas.state import FinancialState

_DATE_KEYS = {"due_date", "deadline", "date", "start", "end", "new_date"}


class DatasetError(ValueError):
    """The eval dataset is malformed. Fail loudly: a silently dropped query
    would bias the comparison."""


class Expectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    must_be_feasible: bool | None = None
    max_equity_pct: int | None = Field(default=None, ge=0, le=100)
    min_buffer_months: int | None = Field(default=None, ge=0, le=12)
    must_shift_deadline: bool | None = None


class EvalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = ""
    primitives: list[Primitive] = Field(min_length=1)
    state: FinancialState | None = None
    monthly_living_expense: Decimal | None = Field(default=None, ge=0)
    expectations: Expectations = Expectations()


def _is_relative(v: Any) -> bool:
    return isinstance(v, str) and len(v) > 2 and v[0] == "+" and v[-1] == "d" and v[1:-1].isdigit()


def _resolve(node: Any, as_of: date) -> Any:
    """Replace "+Nd" strings under date keys with as_of + N days (ISO)."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _DATE_KEYS and _is_relative(v):
                out[k] = (as_of + timedelta(days=int(v[1:-1]))).isoformat()
            else:
                out[k] = _resolve(v, as_of)
        return out
    if isinstance(node, list):
        return [_resolve(v, as_of) for v in node]
    return node


def parse_dataset(raw: Any, as_of: date | None = None) -> list[EvalQuery]:
    as_of = as_of or date.today()
    items = raw.get("queries") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise DatasetError("dataset must be a non-empty list of queries (or {\"queries\": [...]})")
    out: list[EvalQuery] = []
    seen: set[str] = set()
    for n, item in enumerate(items):
        try:
            q = EvalQuery.model_validate(_resolve(item, as_of))
        except ValidationError as e:
            ident = item.get("id", f"#{n}") if isinstance(item, dict) else f"#{n}"
            raise DatasetError(f"query {ident}: {e.errors()[0]['loc']}: {e.errors()[0]['msg']}") from None
        if q.id in seen:
            raise DatasetError(f"duplicate query id {q.id!r}")
        seen.add(q.id)
        out.append(q)
    return out


def load_dataset(path: str | Path, as_of: date | None = None) -> list[EvalQuery]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DatasetError(f"cannot read dataset {path}: {e}") from None
    return parse_dataset(raw, as_of)
