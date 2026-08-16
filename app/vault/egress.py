"""EgressPolicy — the heart of Layer 2. A destination-dependent projection of
a payload, implemented as an explicit table (PROJECTIONS), not scattered
conditionals.

Two mechanisms doing two different jobs (see README "Encryption vs.
abstraction"):
  - Envelope encryption (app/vault/crypto.py) defeats an attacker who has
    your bytes.
  - EgressPolicy defeats a legitimate recipient who re-identifies someone
    from what you handed it — encryption cannot stop that, because the
    recipient was never denied the data, only its raw identifiers.

PURITY: DEBATE_AGENT and OPTIMIZER projections are pure — they only ever
remove or coarsen information, never look anything up. Only USER_FACING
touches a database (it detokenizes, which is audited). The `db` import is
therefore deferred (see _detokenize_leaf) so this module has no hard
SQLAlchemy dependency, and the entire non-USER_FACING egress test suite runs
without a database.

DEFAULT DENY: classify() treats an unrecognized field as TIER_HEALTH_FAMILY —
the most restrictive class — so apply() drops it for agent destinations by
construction; see app/vault/classification.py.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.enums import EgressDestination, FieldKind
from app.vault.classification import classify
from app.vault.tokens import is_token

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# --- Banding ---------------------------------------------------------------

_LAKH = Decimal(10) ** 5
_CRORE = Decimal(10) ** 7
_THOUSAND = Decimal(10) ** 3


def _fmt_num(d: Decimal) -> str:
    if d == d.to_integral_value():
        return str(int(d))
    return str(d.normalize())


def band_amount(amount) -> str | None:
    """One-significant-figure flooring: band width = 10 ** floor(log10(|a|)).
    Encryption is the wrong tool for re-identification — a debate agent that
    receives an exact ₹8,00,000 savings figure plus an exact ₹75,000 income
    can single out an individual from a population without ever seeing a
    name. Banding is a k-anonymity-style generalization over quasi-
    identifiers: each released value maps to an equivalence class, not a
    point.

    Acknowledged gaps, stated rather than hidden:
      - k-anonymity does not bound attribute disclosure when a class is
        homogeneous; l-diversity addresses that and is NOT implemented here.
      - t-closeness additionally bounds the distance between a class's
        attribute distribution and the population's; also NOT implemented.
      - one-significant-figure banding releases ~log2(9) ~= 3.2 bits per
        value. Across several correlated financial fields that adds up fast
        — widen the bands before this faces a real population.
    Full precision goes ONLY to the deterministic optimizer, never to a
    debating agent.
    """
    if amount is None:
        return None
    a = Decimal(str(amount))
    if a == 0:
        return "₹0"
    sign = "−" if a < 0 else ""
    a = abs(a)
    width = Decimal(10) ** a.adjusted()
    lo = (a // width) * width
    hi = lo + width
    return sign + _render_inr_range(lo, hi)


def _render_inr_range(lo: Decimal, hi: Decimal) -> str:
    if lo >= _CRORE:
        return f"₹{_fmt_num(lo / _CRORE)}–{_fmt_num(hi / _CRORE)} crore"
    if lo >= _LAKH:
        return f"₹{_fmt_num(lo / _LAKH)}–{_fmt_num(hi / _LAKH)} lakh"
    if lo >= _THOUSAND:
        return f"₹{_fmt_num(lo / _THOUSAND)}k–{_fmt_num(hi / _THOUSAND)}k"
    return f"₹{_fmt_num(lo)}–{_fmt_num(hi)}"


def band_months(n) -> str | None:
    if n is None:
        return None
    n = int(n)
    if n < 3:
        return "under 3 months"
    if n < 6:
        return "3–6 months"
    if n < 12:
        return "6–12 months"
    if n < 24:
        return "1–2 years"
    return "over 2 years"


def band_count(n) -> str | None:
    if n is None:
        return None
    n = int(n)
    if n <= 3:
        return "1–3"
    if n <= 6:
        return "4–6"
    return "7+"


def band_rate(r) -> str | None:
    if r is None:
        return None
    r = Decimal(str(r))
    lo = r.to_integral_value(rounding="ROUND_FLOOR")
    return f"{_fmt_num(lo)}–{_fmt_num(lo + 1)}%"


def generalize_for_debate(state) -> dict:
    """Coarsen a FinancialState for consumption by debating agents. Full
    precision goes ONLY to the deterministic optimizer, never a debater."""
    payload = state.model_dump(mode="python") if hasattr(state, "model_dump") else dict(state)
    return apply_egress(payload, EgressDestination.DEBATE_AGENT)


# --- Health/family abstraction ---------------------------------------------

_ALLOWED_REASON_CODES = frozenset({"ongoing_care", "dependent_support", "family_commitment", "unclassified"})


def _add_months(d, months: int):
    from calendar import monthrange
    from datetime import date

    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def abstract_health_family(items: list[dict]) -> list[dict]:
    """Reduce STRUCTURED care/family commitments to Layer 1 primitives.

    Input items look like:
      {"amount": 18000, "cadence": "MONTHLY", "duration_months": 14,
       "reason_code": "ongoing_care", "min_liquidity": 200000}
    Output is a list of Primitive-shaped dicts:
      RECURRING_OUTFLOW(amount=18000, cadence=MONTHLY, start=.., end=..)
      RISK_CONSTRAINT(min_liquidity=200000, reason="ongoing_care")

    This function consumes STRUCTURED input only — never natural language.
    "chemotherapy for user's mother, 18k/month, 14 months" -> primitives is
    NL decomposition, i.e. decompose() (app/services/hurdles.py), which is
    explicitly out of scope for this layer (see README, non-goals). Layer
    2's contribution is the GUARANTEE that only primitives cross the
    boundary, not the NL reduction itself.

    `reason` is drawn ONLY from _ALLOWED_REASON_CODES — never free text. A
    free-text reason is a re-identification channel wearing a primitive's
    clothes; RiskConstraint.reason is a plain str in the schema, so this
    allowlist is the only thing enforcing that.
    """
    from datetime import date

    primitives: list[dict] = []
    for item in items:
        amount = item.get("amount")
        cadence = item.get("cadence", "MONTHLY")
        duration_months = item.get("duration_months")
        start = item.get("start") or date.today()
        if isinstance(start, str):
            start = date.fromisoformat(start)

        if amount is not None:
            end = _add_months(start, int(duration_months)) if duration_months else None
            primitives.append(
                {
                    "type": "RECURRING_OUTFLOW",
                    "amount": amount,
                    "cadence": cadence,
                    "start": start.isoformat(),
                    "end": end.isoformat() if end else None,
                }
            )

        min_liquidity = item.get("min_liquidity")
        if min_liquidity is not None:
            reason = item.get("reason_code", "unclassified")
            if reason not in _ALLOWED_REASON_CODES:
                reason = "unclassified"
            primitives.append({"type": "RISK_CONSTRAINT", "min_liquidity": min_liquidity, "reason": reason})

    return primitives


# --- EgressPolicy ------------------------------------------------------------


def _detokenize_leaf(value: str, *, db: "Session", caller: str, purpose: str) -> str:
    from app.services.vault import detokenize  # deferred: keeps this module DB-import-free by default

    return detokenize(db, value, caller=caller, purpose=purpose)


def _project_leaf(value, *, kind: FieldKind, destination: EgressDestination, db, caller, purpose):
    if destination == EgressDestination.USER_FACING:
        if isinstance(value, str) and is_token(value):
            return _detokenize_leaf(value, db=db, caller=caller, purpose=purpose)
        return value

    if kind == FieldKind.IDENTITY:
        return value  # already a vault token — safe to pass through unchanged
    if kind == FieldKind.STRUCTURAL:
        return value
    if kind == FieldKind.MAGNITUDE:
        if destination == EgressDestination.DEBATE_AGENT:
            return band_amount(value)
        return value  # OPTIMIZER: exact precision
    return _DROP  # FREEFORM / TIER_HEALTH_FAMILY / default-deny


_DROP = object()


def _project(payload: Any, *, parent_kind: FieldKind | None, destination, db, caller, purpose):
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            classification = classify(key)
            if isinstance(value, (dict, list)):
                if destination != EgressDestination.USER_FACING and classification.kind == FieldKind.FREEFORM:
                    continue  # whole nested structure is TIER_HEALTH_FAMILY/default-deny -> dropped wholesale
                projected = _project(value, parent_kind=classification.kind, destination=destination, db=db, caller=caller, purpose=purpose)
                out[key] = projected
            else:
                projected = _project_leaf(value, kind=classification.kind, destination=destination, db=db, caller=caller, purpose=purpose)
                if projected is _DROP:
                    continue
                out[key] = projected
        return out
    if isinstance(payload, list):
        kind = parent_kind if parent_kind is not None else FieldKind.FREEFORM
        if destination != EgressDestination.USER_FACING and kind == FieldKind.FREEFORM:
            return []
        result = []
        for item in payload:
            if isinstance(item, (dict, list)):
                result.append(_project(item, parent_kind=kind, destination=destination, db=db, caller=caller, purpose=purpose))
            else:
                projected = _project_leaf(item, kind=kind, destination=destination, db=db, caller=caller, purpose=purpose)
                if projected is not _DROP:
                    result.append(projected)
        return result
    return payload


class EgressPolicy:
    """Destination-dependent projection. One class, one table, no scattered
    ifs — see module docstring."""

    @staticmethod
    def apply(
        payload: dict,
        destination: EgressDestination,
        *,
        db: "Session | None" = None,
        caller: str | None = None,
        purpose: str | None = None,
    ) -> dict:
        if destination == EgressDestination.USER_FACING and (db is None or not caller or not purpose):
            raise ValueError("USER_FACING egress requires db, caller, and purpose (it is always audited).")
        return _project(payload, parent_kind=None, destination=destination, db=db, caller=caller, purpose=purpose)


def apply_egress(payload: dict, destination: EgressDestination, **kwargs) -> dict:
    return EgressPolicy.apply(payload, destination, **kwargs)
