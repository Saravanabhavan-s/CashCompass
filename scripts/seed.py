"""
Demo seed: one user with ₹8L savings, ₹75k/month salary, INFY + RELIANCE stocks,
and a GOAL hurdle "Wedding in ~8 months, ₹5L".

The wedding hurdle deliberately decomposes into three universal primitives:
  - ONE_TIME_OUTFLOW  (the ceremony expense)
  - GOAL             (the savings target)
  - RISK_CONSTRAINT  (a liquidity buffer)

No "wedding" primitive type exists — proving the model handles any life event
from birth to death through composition, without enumeration.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Hurdle, LedgerEntry  # noqa: F401 — registers ORM guard


def seed() -> None:
    wedding_date = date.today() + timedelta(days=240)  # ~8 months

    with Session(engine) as db:
        existing = db.scalar(select(Hurdle).where(Hurdle.label == "Wedding"))
        if existing:
            print("Already seeded. Skipping.")
            return

        # -- Hurdle: Wedding, decomposed into 3 universal primitives -----------
        #
        # A wedding is not a special type. It is:
        #   1. A large one-time outflow on a known date.
        #   2. A savings goal to fund it.
        #   3. A risk constraint to keep liquid reserves intact for surprises.
        #
        # The same three primitives describe a ₹2L registration or a ₹50L
        # destination wedding — only the amounts differ.
        hurdle = Hurdle(
            label="Wedding",
            raw_description="Wedding celebration in ~8 months, target ₹5L",
            primitives=[
                {
                    "type": "ONE_TIME_OUTFLOW",
                    "amount": 500000.0,
                    "due_date": wedding_date.isoformat(),
                },
                {
                    "type": "GOAL",
                    "target_amount": 500000.0,
                    "deadline": wedding_date.isoformat(),
                    "priority": 1,
                },
                {
                    "type": "RISK_CONSTRAINT",
                    "min_liquidity": 100000.0,
                    "reason": (
                        "Wedding liquidity buffer — cover last-minute costs "
                        "without liquidating investments"
                    ),
                },
            ],
        )
        db.add(hurdle)
        db.flush()  # resolve hurdle.id before linking ledger entries

        # -- Ledger: initial state snapshot ------------------------------------
        snapshot = LedgerEntry(
            effective_date=date.today(),
            type="STATE_SNAPSHOT",
            source="USER_STATED",
            description="Initial financial state: savings, salary, INFY and RELIANCE holdings",
            effect=[
                {"field": "savings", "op": "set", "value": 800000.0},
                {"field": "recurring_income", "op": "set", "value": 75000.0},
                {
                    "field": "stocks",
                    "op": "append",
                    "value": {"symbol": "INFY", "units": 50, "current_value": 800000.0},
                },
                {
                    "field": "stocks",
                    "op": "append",
                    "value": {"symbol": "RELIANCE", "units": 20, "current_value": 600000.0},
                },
            ],
        )
        db.add(snapshot)

        # -- Ledger: wedding commitment linked to the hurdle -------------------
        commitment = LedgerEntry(
            effective_date=date.today(),
            type="COMMITMENT",
            source="USER_STATED",
            description=f"Commit to saving ₹5L for wedding by {wedding_date}",
            effect=[
                {
                    "field": "one_time_inflows",
                    "op": "append",
                    "value": {
                        "label": "Wedding fund (planned outflow)",
                        "amount": -500000.0,
                        "date": wedding_date.isoformat(),
                    },
                }
            ],
            linked_hurdle_id=hurdle.id,
        )
        db.add(commitment)
        db.commit()

        hurdle_id = hurdle.id
        primitive_count = len(hurdle.primitives)

    print("Seeded successfully.")
    print(f"  Hurdle: 'Wedding' id={hurdle_id}")
    print(f"  Wedding date: {wedding_date}")
    print(f"  Primitives ({primitive_count}):")
    print(f"    ONE_TIME_OUTFLOW  ₹5,00,000  due {wedding_date}")
    print(f"    GOAL              ₹5,00,000  deadline {wedding_date}  priority 1")
    print(f"    RISK_CONSTRAINT   ₹1,00,000  liquidity buffer")
    print(f"  Ledger: 2 entries (STATE_SNAPSHOT + COMMITMENT)")
    print(f"  State:  ₹8L savings | ₹75k/month | INFY 50u + RELIANCE 20u")


if __name__ == "__main__":
    seed()
