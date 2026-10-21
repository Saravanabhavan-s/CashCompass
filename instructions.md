# CashCompass Layer 1 + Layer 2 — Instructions & Commands

## Quick Start

```bash
# 1. Install dependencies
pip install -e ".[dev]"

# 2. Set up environment
cp .env.example .env
# Edit .env if needed (defaults to localhost Postgres)

# 3. Generate Layer 2 vault keys (KEK, HSK, admin key) and paste into .env
python scripts/gen_vault_keys.py

# 4. Run migrations
alembic upgrade head

# 5. Seed demo data
python scripts/seed.py

# 6. Start server
uvicorn app.main:app --reload
# Server at http://localhost:8000
# Docs at http://localhost:8000/docs
```

The server refuses to start (`get_key_provider()` runs in the FastAPI
`lifespan`) if `VAULT_KEKS`/`VAULT_HSK` aren't configured — fails at boot,
not at the first request.

**For every endpoint's exact request body, expected response, and pass/fail
criteria, see [test.md](test.md)** — the curl examples below are a subset;
test.md covers all of Layer 1 + Layer 2 plus cross-cutting checks (audit
completeness, immutability, no-plaintext-at-rest).

## API Endpoints

Curl examples below. For the full picture per endpoint — input schema,
validation rules, exact expected response shape, and what pass vs. fail
looks like — see [test.md](test.md).

### Hurdles

**Create hurdle** (with manual primitives)
```bash
curl -X POST http://localhost:8000/hurdles \
  -H "Content-Type: application/json" \
  -d '{
    "label": "Wedding",
    "raw_description": "Wedding in ~8 months, target ₹5L",
    "primitives": [
      {
        "type": "ONE_TIME_OUTFLOW",
        "amount": 500000,
        "due_date": "2026-12-31"
      },
      {
        "type": "GOAL",
        "target_amount": 500000,
        "deadline": "2026-12-31",
        "priority": 1
      },
      {
        "type": "RISK_CONSTRAINT",
        "min_liquidity": 100000,
        "reason": "wedding buffer"
      }
    ]
  }'
```

**Get hurdle**
```bash
curl http://localhost:8000/hurdles/{hurdle_id}
```

### Ledger (Append-Only)

**Append entry** (new decision/commitment)
```bash
curl -X POST http://localhost:8000/ledger \
  -H "Content-Type: application/json" \
  -d '{
    "effective_date": "2026-06-27",
    "type": "DECISION",
    "source": "USER_STATED",
    "description": "Got a raise: salary now ₹85k/month",
    "effect": [
      {
        "field": "recurring_income",
        "op": "add",
        "value": 10000
      }
    ],
    "linked_hurdle_id": null
  }'
```

**List ledger** (paginated, immutable)
```bash
curl "http://localhost:8000/ledger?limit=50&offset=0"
```

### Financial State

**Get rebuilt state** (deterministic projection of ledger)
```bash
curl http://localhost:8000/state
```

Response:
```json
{
  "cash": null,
  "liquid_cash": null,
  "savings": 800000,
  "stocks": [
    {"symbol": "INFY", "units": 50, "current_value": 800000},
    {"symbol": "RELIANCE", "units": 20, "current_value": 600000}
  ],
  "loans": [],
  "emis": [],
  "debts": [],
  "insurance": [],
  "recurring_income": 75000,
  "one_time_inflows": [
    {"label": "Wedding fund (planned outflow)", "amount": -500000, "date": "2026-12-31"}
  ]
}
```

### Sensitive Profile (vault-protected)

**Upsert sensitive profile** — server tokenizes identity/health fields before storage
```bash
curl -X POST http://localhost:8000/profile/sensitive \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "medical_conditions": ["diabetes", "hypertension"],
    "dependents": [
      {"relation": "child", "age": 8, "notes": "school fees ₹2.5L/year"},
      {"relation": "parent", "age": 72, "notes": "medical expenses ~₹1L/year"}
    ],
    "family_commitments": ["wedding in 8 months", "parent retirement plan"],
    "obligations": ["mother-in-law care", "sibling college fund"]
  }'
# Response holds TKN_MEDICAL_.../TKN_NOTE_... tokens, not the plaintext above.
```

**Reveal a sensitive profile** (admin + audited — USER_FACING egress destination)
```bash
curl -X POST http://localhost:8000/profile/sensitive/reveal \
  -H "Content-Type: application/json" \
  -H "X-Vault-Admin-Key: $VAULT_ADMIN_KEY" \
  -d '{"user_id": "user_123", "caller": "demo-user", "purpose": "support ticket #42"}'
```

### Vault (Layer 2)

**Tokenize a value** (admin)
```bash
curl -X POST http://localhost:8000/vault/tokenize \
  -H "Content-Type: application/json" \
  -H "X-Vault-Admin-Key: $VAULT_ADMIN_KEY" \
  -d '{"field_type": "NAME", "value": "Ramya G"}'
```

**Detokenize** (requires `caller` + `purpose`; caller must be on
`VAULT_DETOKENIZE_ALLOWED_CALLERS`; every call — granted or denied — is audited)
```bash
curl -X POST http://localhost:8000/vault/detokenize \
  -H "Content-Type: application/json" \
  -d '{"token_id": "TKN_NAME_8f3a1c2b4d5e6f70", "caller": "demo-user", "purpose": "support ticket #42"}'
```

**Audit trail** (admin — paginated, immutable)
```bash
curl "http://localhost:8000/vault/audit?limit=20&offset=0" \
  -H "X-Vault-Admin-Key: $VAULT_ADMIN_KEY"
```

**Crypto-shred a token** (admin — irreversible; the ledger entry referencing it survives, now permanently opaque)
```bash
curl -X POST http://localhost:8000/vault/shred/TKN_NAME_8f3a1c2b4d5e6f70 \
  -H "Content-Type: application/json" \
  -H "X-Vault-Admin-Key: $VAULT_ADMIN_KEY" \
  -d '{"reason": "DPDP erasure request"}'
```

**Rotate the KEK** (admin — re-wraps every DEK; ciphertext is untouched)
```bash
curl -X POST http://localhost:8000/vault/rotate-kek \
  -H "Content-Type: application/json" \
  -H "X-Vault-Admin-Key: $VAULT_ADMIN_KEY" \
  -d '{"new_version": 2}'
```

**Egress preview** (debug — see exactly what each destination receives)
```bash
curl "http://localhost:8000/egress/preview?destination=OPTIMIZER"
curl "http://localhost:8000/egress/preview?destination=DEBATE_AGENT"
# compare: OPTIMIZER shows exact ₹ figures, DEBATE_AGENT shows banded ranges
# and "leaks": [] proves nothing exact/health-related crossed the boundary.
```

## Primitives: Universal Life Event Decomposition

Every life event (wedding, newborn, surgery, job loss, retirement) maps to one or more primitives. NO special types for specific events.

| Primitive | Fields | Use Case |
|-----------|--------|----------|
| **ONE_TIME_OUTFLOW** | `amount`, `due_date` | Wedding, surgery, car, home repair |
| **RECURRING_OUTFLOW** | `amount`, `cadence` (MONTHLY/QUARTERLY/YEARLY), `start`, `end?` | EMI, school fees, parent care, insurance |
| **ONE_TIME_INFLOW** | `amount`, `date` | Bonus, gift, asset sale, inheritance |
| **INFLOW_CHANGE** | `delta`, `cadence`, `start` | Raise, job loss, new rental income, freelance gig |
| **GOAL** | `target_amount`, `deadline`, `priority` (1-10) | Wedding fund, retirement, home down payment |
| **HORIZON_SHIFT** | `goal_ref`, `new_date` | Delay retirement, accelerate wedding, push back education |
| **RISK_CONSTRAINT** | `min_liquidity`, `reason` | Emergency buffer, wedding surprise fund |

### Example: Life Events as Primitive Composition

**Wedding (₹5L, 8 months)**
```json
[
  {"type": "ONE_TIME_OUTFLOW", "amount": 500000, "due_date": "2026-12-31"},
  {"type": "GOAL", "target_amount": 500000, "deadline": "2026-12-31", "priority": 1},
  {"type": "RISK_CONSTRAINT", "min_liquidity": 100000, "reason": "wedding surprises"}
]
```

**New child (impact on budget)**
```json
[
  {"type": "RECURRING_OUTFLOW", "amount": 25000, "cadence": "MONTHLY", "start": "2026-06-27"},
  {"type": "GOAL", "target_amount": 1500000, "deadline": "2044-06-27", "priority": 2},
  {"type": "RISK_CONSTRAINT", "min_liquidity": 300000, "reason": "child medical emergencies"}
]
```

**Job loss (₹60k → 0)**
```json
[
  {"type": "INFLOW_CHANGE", "delta": -60000, "cadence": "MONTHLY", "start": "2026-06-27"},
  {"type": "RISK_CONSTRAINT", "min_liquidity": 600000, "reason": "6-month emergency fund"}
]
```

## Corrections: Compensating Entries, Not Mutations

The ledger is **append-only at two layers**:

1. **ORM guard** (SQLAlchemy before_flush): blocks UPDATE/DELETE in Python
2. **Postgres trigger** (BEFORE UPDATE/DELETE): blocks raw-SQL mutations

**Example: You misstated savings as ₹8L, actual is ₹10L**

Wrong:
```bash
# DON'T: try to UPDATE the original entry
UPDATE ledger_entry SET effect = ... WHERE id = 'orig-id'
# → Postgres trigger raises: "ledger_entry is append-only"
```

Right:
```bash
# DO: append a compensating entry
curl -X POST http://localhost:8000/ledger \
  -H "Content-Type: application/json" \
  -d '{
    "effective_date": "2026-06-27",
    "type": "DECISION",
    "source": "USER_STATED",
    "description": "Correction: actual savings ₹10L (was ₹8L)",
    "effect": [
      {"field": "savings", "op": "add", "value": 200000}
    ]
  }'
```

When you rebuild state, both entries fold in order → correct snapshot.

## Testing

Automated suite below. For manual/curl testing of each endpoint one at a
time — exact request bodies, expected responses, pass/fail criteria — see
[test.md](test.md).

```bash
# All tests (uses testcontainers to spin up Postgres, or TEST_DATABASE_URL env var)
pytest

# Specific test file
pytest tests/test_append_only.py -v

# Specific test
pytest tests/test_append_only.py::test_orm_update_raises -v
```

### Test Layers

Layer 1:
- **test_append_only.py**: ORM guard + Postgres trigger enforcement
- **test_state_rebuild.py**: Pure `_fold_entries()` (no DB required)
- **test_decompose.py**: Primitive round-trip, no hardcoded life-event types

Layer 2 — the crypto core, normalization, tokens, egress policy, banding, and
scanner are all pure and DB-free; only tests exercising the service/API layer
need Postgres:
- **test_vault_crypto.py**: AES-GCM envelope, nonce uniqueness, tamper detection (no DB)
- **test_vault_normalize.py**: per-field_type canonicalization (no DB)
- **test_vault_tokens.py**: blind index determinism, per-tenant HKDF (no DB)
- **test_egress_policy.py**: destination projection, banding, default-deny, abstraction (no DB)
- **test_vault_protected.py**: Pydantic + SQLAlchemy + Postgres cleartext guards
- **test_vault_tokenize.py / test_vault_aad.py / test_vault_audit.py**: service layer round-trips, row-swap, audit completeness
- **test_vault_rotation.py**: KEK rotation, ciphertext-unchanged guard
- **test_vault_shred.py**: crypto-shredding, unrecoverability proof
- **test_sensitive_vault.py**: SensitiveProfile tokenization
- **test_vault_api.py**: HTTP layer — admin auth, allowlist denial, egress preview
- **test_state_rebuild_tokenized.py**: proves the fold is tokenization-invariant (no DB)

## Design Principles

### Decision-Aware, Not Behavior-Predictive

CashCompass does NOT learn spending patterns or predict behavior. It records:
- **Discrete decisions**: "I decided to save ₹5L for a wedding by Dec 2026"
- **Explicit commitments**: "I committed to ₹25k/month care for parent"
- **Stated constraints**: "I need ₹10L emergency liquidity"

This stays valid *across* life events (wedding → newborn → job loss). Prediction systems break at every major life event; decision recording stays true.

### Universal Primitives, No Event Types

No WEDDING, NEWBORN, SURGERY, LAYOFF types. Every event decomposes:

```
wedding ∈ {ONE_TIME_OUTFLOW, GOAL, RISK_CONSTRAINT}
newborn ∈ {RECURRING_OUTFLOW, GOAL, RISK_CONSTRAINT}
layoff  ∈ {INFLOW_CHANGE, RISK_CONSTRAINT}
```

Same ~7 primitives handle ANY life event, past or future, without code changes. System evolves by adding Hurdles (instances), not Primitives (types).

### Append-Only Ledger

Source of truth never mutates. Corrections are *new compensating entries*. Benefits:

1. **Audit trail**: every decision visible, no deletions
2. **Deterministic**: rebuild state by folding ledger = always same result
3. **Dispute resolution**: if stakeholders disagree (you vs. spouse about savings), both positions are logged
4. **ML-safe**: future agents see full decision history, not edited snapshots

## Seeded Demo State

After `python scripts/seed.py`:

- **User**: demo (still no user/auth table — Layer 2 uses a single
  `default_tenant_id` constant rather than adding one; real auth is a Layer 3+ concern)
- **Savings**: ₹8L
- **Monthly income**: ₹75k
- **Stocks**: INFY 50u + RELIANCE 20u
- **Hurdle**: Wedding (₹5L goal, 8 months, no special type — just 3 primitives)
- **Ledger**: 2 entries (STATE_SNAPSHOT + COMMITMENT)

Hit `GET /state` to see the rebuilt snapshot. Hit `GET /ledger` to see all entries.

## Next Layers (Stubs / Seams)

### Layer 2: PII Tokenization Vault — DONE
Envelope encryption (per-record DEKs wrapped by a KEK), deterministic
HMAC blind-index tokenization, AAD row-swap binding, a destination-aware
`EgressPolicy`, and crypto-shredding. See the "Layer 2 — PII Tokenization
Vault" section of README.md for the full design writeup and threat model.

### Layer 3: LLM Decomposition + Optimization / Planning Agent
- `app.services.hurdles.decompose()` still raises `NotImplementedError` —
  untouched by Layer 2 (see README's "Explicit non-goals")
- Will implement NL → primitives (e.g., "wedding in 8 months, budget ₹5L" → 3 primitives)
- Will gate access based on DPDP Act consent
- Given primitives + constraints, find feasible savings/investment plan
- Recommend: "Move wedding 2 months forward, or raise salary ₹15k, or reduce ₹50k outflow"
- Debate constraints: "Minimize risk" vs. "Maximize growth" vs. "Ensure legacy fund"

### Layer 4: Execution + Monitoring
- Track actual outflows/inflows vs. plan
- Alert on deviations (e.g., wedding costs exceed budget)
- Re-plan on life events (new child, promotion, illness)

## Project Structure

```
CashCompass/
  app/
    main.py              ← FastAPI app, router wiring, lifespan key check, exception handlers
    config.py, db.py, enums.py, exceptions.py
    models/              ← SQLAlchemy (Hurdle, LedgerEntry, SensitiveProfile, VaultEntry, DetokenizationAudit)
      append_only.py     ← shared append-only registry + before_flush guard
    schemas/              ← Pydantic v2 (discriminated union for Primitives, vault/egress schemas)
    services/             ← Business logic (_fold_entries, append_entry, vault, egress)
    api/                  ← FastAPI routers (thin handlers)
    vault/                ← Layer 2 core — NO SQLAlchemy/Session imports, fully DB-free
      crypto.py           ← AES-256-GCM envelope primitives
      keys.py              ← KeyProvider (Local prototype + Kms stub)
      normalize.py         ← per-field_type canonicalization
      tokens.py             ← HMAC blind index, AAD builders
      classification.py     ← field → tier/kind registry, default-deny
      egress.py              ← EgressPolicy, banding, health/family abstraction
      scanner.py              ← leak scanner (used by tests + /egress/preview)
      protected.py            ← VaultToken, VaultProtectedModel/Mixin
  alembic/               ← Migrations (tables + append-only + cleartext-guard triggers)
  scripts/seed.py, scripts/gen_vault_keys.py
  tests/
    test_append_only.py, test_state_rebuild.py, test_decompose.py          ← Layer 1
    test_vault_*.py, test_egress_policy.py, test_sensitive_vault.py, ...   ← Layer 2
  pyproject.toml, alembic.ini, .env.example, README.md
```

## Troubleshooting

**`psycopg2.OperationalError: connection to server ... refused`**
→ Postgres not running. Start Docker or local Postgres:
```bash
docker run -d --name cashcompass-pg \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=cashcompass \
  -p 5432:5432 \
  postgres:16
```

**`DetachedInstanceError` after exiting Session**
→ Don't access ORM attributes after `with` block. Capture values inside the session.

**`sqlalchemy.orm.exc.AppendOnlyViolation`**
→ Tried to UPDATE/DELETE a LedgerEntry. Use compensating entries instead.

**Tests fail on Windows (Docker daemon not running)**
→ Set `TEST_DATABASE_URL` to a running Postgres, or start Docker Desktop.

**`KeyConfigurationError: VAULT_KEKS and VAULT_HSK must be configured`**
→ Run `python scripts/gen_vault_keys.py` and paste its output into `.env`.
The server checks this at boot (FastAPI `lifespan`), not at the first request.

**`app.exceptions.CleartextLeakError` on a SensitiveProfile write**
→ Something bypassed `app.services.sensitive.upsert_sensitive_profile()` and
tried to write plaintext into a vault-protected column directly. Go through
the service function — it tokenizes before the ORM object is ever built.

## Money Representation

All amounts are `Decimal(18,2)` → ₹ with paise, no float drift.

```python
from decimal import Decimal
amount = Decimal("500000.50")  # ₹5,00,000.50
```

## Next Steps

1. Run the server: `uvicorn app.main:app --reload`
2. Hit http://localhost:8000/docs → interactive Swagger UI
3. `GET /state` → see the seeded snapshot
4. `GET /ledger?limit=10` → see all decisions
5. `POST /hurdles` → add a new life event (e.g., child care commitment)
6. `POST /ledger` → append a new income/expense decision
7. Run tests: `pytest -v`

All data is persistent in Postgres. Ledger is immutable; state is a live projection.
