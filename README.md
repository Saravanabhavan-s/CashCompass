# CashCompass — Layer 1: Decision Ledger + Hurdle Primitive Model

## Why decision-aware, not behavior-predictive

Most personal finance tools track *what you did* (transactions, habits, spending
patterns) and use that history to predict future behavior or nudge you toward
different behavior. This creates two fundamental problems:

1. **It doesn't generalize to life events.** Spending patterns break at every
   significant life event — a wedding, a new child, a job loss, a health crisis.
   A prediction model trained on your "normal" behavior is useless the moment
   normal ends.

2. **It treats you as a subject, not an agent.** Pattern prediction implicitly
   models you as a deterministic system to be optimized. It ignores the fact
   that your financial trajectory is shaped by *decisions you make* — discrete,
   reasoned commitments — not by statistical inertia.

CashCompass instead records **decisions and commitments as first-class facts**.
Every entry in the ledger is something you decided or committed to. The system
reasons over those decisions — what they imply, what constraints they impose,
what they make possible — rather than trying to predict what you'll do based on
what you've done.

The result: a system that stays accurate *after* a life event, not just before.

## Why every life event is a primitive composition

Systems that enumerate life events ("wedding mode", "new baby mode", "job loss
mode") break in two ways: they miss events that weren't anticipated, and they
bake assumptions about what each event means financially.

A wedding doesn't "mean" anything universally — it might mean a ₹2L ceremony
for one person and a ₹50L destination event for another. A new child might
mean school fees, or it might mean a dependent who needs medical coverage, or
both, or neither.

Instead, CashCompass defines a small set of **universal financial primitives**
that describe what *changes* in your financial landscape:

| Primitive | What it models |
|-----------|---------------|
| `ONE_TIME_OUTFLOW` | A single future expense |
| `RECURRING_OUTFLOW` | A repeating expense (EMI, school fees, care costs) |
| `ONE_TIME_INFLOW` | A single future receipt (bonus, gift, sale) |
| `INFLOW_CHANGE` | A permanent change to income (raise, job loss, rental) |
| `HORIZON_SHIFT` | Moving a goal's deadline |
| `RISK_CONSTRAINT` | A floor on liquid reserves |
| `GOAL` | A target amount by a deadline with a priority |

Any life event decomposes into a short list of these primitives. A wedding:

```
ONE_TIME_OUTFLOW(amount=₹5L, due=2025-02-14)  ← the expense
GOAL(target=₹5L, deadline=2025-02-14, priority=1)  ← the savings target
RISK_CONSTRAINT(min_liquidity=₹1L, reason="wedding surprises")  ← buffer
```

No "wedding type". No assumptions about scale. The same three-primitive pattern
handles a ₹2L registration or a ₹50L destination event — only the amounts differ.

This means the same reasoning engine handles *everything* from birth to death
without enumeration, without hardcoded cases, and without breaking when an event
doesn't fit a predefined category.

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env  # edit DATABASE_URL
alembic upgrade head
python scripts/seed.py
uvicorn app.main:app --reload
```

## API

- `POST /hurdles` — create a hurdle (with manual primitives for now)
- `GET  /hurdles/{id}`
- `POST /ledger` — append a decision/commitment (enforced append-only)
- `GET  /ledger` — paginated list
- `GET  /state` — rebuilt current financial state
- `POST /profile/sensitive` — upsert sensitive profile (not yet vault-protected)

## Tests

```bash
# With a running Postgres:
TEST_DATABASE_URL=postgresql://... pytest

# With Docker (testcontainers auto-provision):
pytest
```

## Architecture notes

- **Ledger** is append-only at two layers: an ORM guard (`before_flush`)
  and a Postgres `BEFORE UPDATE OR DELETE` trigger. Both must be bypassed
  to corrupt history; raw SQL alone cannot do it.
- **Financial state** is a projection: `rebuild_state_from_ledger()` folds
  the ledger into a `FinancialState` snapshot. State is never edited directly.
- **Sensitive profile** is tier `TIER_HEALTH_FAMILY` per India's DPDP Act 2023.
  Structurally separate, excluded from default serializers. Layer 2 vault will
  tokenize identity fields and gate access.
- `decompose(raw_description)` is a stub — signature defined, raises
  `NotImplementedError`. Layer 2+ (LLM layer) will implement NL decomposition.
