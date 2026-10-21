# CashCompass — Endpoint Testing Guide

Every endpoint in Layer 1 (ledger, hurdles, state, sensitive profile) and
Layer 2 (vault, egress). For each: what to send, what you get back, and what
counts as pass/fail.

Base URL below: `http://localhost:8000`. Swap for wherever you're running.

---

## 0. Setup — do this before testing anything

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Env file
cp .env.example .env

# 3. Generate Layer 2 vault keys (KEK, HSK, admin key)
python scripts/gen_vault_keys.py
# → paste the printed VAULT_KEK_VERSION / VAULT_KEKS / VAULT_HSK / VAULT_ADMIN_KEY
#   lines into .env

# 4. Add a detokenize caller allowlist entry to .env (comma-separated), e.g.:
echo 'VAULT_DETOKENIZE_ALLOWED_CALLERS=demo-user' >> .env

# 5. Postgres running (either):
docker run -d --name cashcompass-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=cashcompass -p 5432:5432 postgres:16
# ...or point DATABASE_URL in .env at an existing instance.

# 6. Migrate + seed
alembic upgrade head
python scripts/seed.py

# 7. Run
uvicorn app.main:app --reload
```

Sanity check: `curl http://localhost:8000/docs` → 200. If the server refuses
to start, it's almost always the vault keys — see `instructions.md`
Troubleshooting.

Every `curl` example below assumes:
```bash
export BASE=http://localhost:8000
export ADMIN_KEY=<your VAULT_ADMIN_KEY from .env>
```

---

## 1. Automated test suite (the fast way)

```bash
pytest                                    # spins up testcontainers Postgres
TEST_DATABASE_URL=postgresql://... pytest # against an existing Postgres
pytest tests/test_vault_crypto.py -v      # one file
pytest -k "rotation" -v                   # by keyword
```

129 tests total. Pure/offline ones (no DB needed — crypto, normalize,
tokens, egress policy, banding, VaultProtected validation) run in
milliseconds; DB-backed ones need Postgres per step 0.5 above.

Everything below is the manual/curl equivalent of what those tests already
assert — use it to demo the system live, or to debug a specific endpoint by
hand.

---

## 2. Layer 1 — Hurdles

### `POST /hurdles`
Create a hurdle with manually-specified primitives.

**Input** (`HurdleCreate`):
```json
{
  "label": "Wedding",
  "raw_description": "Wedding in ~8 months, target ₹5L",
  "primitives": [
    {"type": "ONE_TIME_OUTFLOW", "amount": 500000, "due_date": "2026-12-31"},
    {"type": "GOAL", "target_amount": 500000, "deadline": "2026-12-31", "priority": 1},
    {"type": "RISK_CONSTRAINT", "min_liquidity": 100000, "reason": "wedding buffer"}
  ]
}
```
Each primitive's `type` must be one of `ONE_TIME_OUTFLOW`, `RECURRING_OUTFLOW`,
`ONE_TIME_INFLOW`, `INFLOW_CHANGE`, `HORIZON_SHIFT`, `RISK_CONSTRAINT`, `GOAL`
— the object shape is discriminated by it (see README primitive table for
each type's exact fields).

**Expected output**: `201`, `HurdleRead` — same fields plus `id` (uuid str)
and `created_at` (timestamp).

**Pass/fail**: `201` + non-null `id` = pass. `422` = a primitive's fields
don't match its `type`'s schema (e.g. `GOAL` missing `priority`).

```bash
curl -s -X POST $BASE/hurdles -H "Content-Type: application/json" -d '{...above...}'
```

### `GET /hurdles/{hurdle_id}`
**Input**: path param, the `id` from a prior create.
**Expected output**: `200` + `HurdleRead`. **`404`** with
`{"detail":"Hurdle not found"}` if the id doesn't exist.

```bash
curl -s $BASE/hurdles/<hurdle_id>
curl -s -o /dev/null -w "%{http_code}\n" $BASE/hurdles/not-a-real-id   # expect 404
```

---

## 3. Layer 1 — Ledger (append-only)

### `POST /ledger`
**Input** (`LedgerEntryCreate`):
```json
{
  "effective_date": "2026-06-27",
  "type": "DECISION",
  "source": "USER_STATED",
  "description": "Got a raise: salary now ₹85k/month",
  "effect": [
    {"field": "recurring_income", "op": "add", "value": 10000}
  ],
  "linked_hurdle_id": null
}
```
- `type` ∈ `DECISION | COMMITMENT | STATE_SNAPSHOT`
- `source` ∈ `USER_STATED | AGENT_SUGGESTED_USER_CONFIRMED | SYSTEM_DERIVED`
- `effect[].op` ∈ `set | add | append`; `field` must be a name `_fold_entries`
  recognizes (`cash`, `liquid_cash`, `savings`, `recurring_income`, `stocks`,
  `loans`, `emis`, `debts`, `insurance`, `one_time_inflows`) or it's silently
  ignored by the state projection (not an error — just has no effect).

**Expected output**: `201` + `LedgerEntryRead` (adds `id`, `created_at`).

**Pass/fail**: `201` = pass. There is no update/delete route — the ledger is
append-only by design; to "correct" a prior entry, `POST` a new compensating
entry with an offsetting `op:"add"` value. Confirm immutability directly:
```bash
# This must fail — proves the append-only guard works.
curl -s -X PATCH $BASE/ledger/<id>   # 405 Method Not Allowed (no such route)
```

### `GET /ledger?limit=&offset=`
**Input**: query params, both optional. `limit`: int, `1..200`, default `50`.
`offset`: int, `≥0`, default `0`.
**Expected output**: `200` + `LedgerPage`:
```json
{"items": [ ...LedgerEntryRead... ], "total": 7, "limit": 50, "offset": 0}
```
**Pass/fail**: `total` should never decrease across calls (append-only).
`limit=0` or `limit=500` → `422` (out of allowed range).

```bash
curl -s "$BASE/ledger?limit=10&offset=0"
```

---

## 4. Layer 1 — Financial State

### `GET /state`
**Input**: none.
**Expected output**: `200` + `FinancialState` — the ledger folded into a
snapshot:
```json
{
  "cash": null, "liquid_cash": null, "savings": "800000.0",
  "stocks": [{"symbol":"INFY","units":"50","current_value":"800000.0"}, ...],
  "loans": [], "emis": [], "debts": [], "insurance": [],
  "recurring_income": "75000.0",
  "one_time_inflows": [{"amount": -500000.0, "date": "2027-04-13"}]
}
```
**Pass/fail**: this is a pure projection — the SAME ledger always folds to
the SAME state. Append a `POST /ledger` entry with `{"field":"savings","op":"add","value":100000}`,
then re-`GET /state` — `savings` must increase by exactly 100000, nothing
else changes.

---

## 5. Layer 1 — Sensitive Profile (vault-protected)

### `POST /profile/sensitive`
**Input** (`SensitiveProfileUpsert` — plaintext; server tokenizes it):
```json
{
  "user_id": "user_123",
  "medical_conditions": ["diabetes", "hypertension"],
  "dependents": [
    {"relation": "child", "age": 8, "notes": "school fees ₹2.5L/year"}
  ],
  "family_commitments": ["wedding in 8 months"],
  "obligations": ["mother-in-law care"]
}
```
All four list fields and `dependents[].notes` are optional (`null` if
omitted). `dependents[].relation` is required if `dependents` is present.

**Expected output**: `201` + `SensitiveProfileRead` — **tokens, not
plaintext**:
```json
{
  "id": "...", "user_id": "user_123",
  "sensitivity_tier": "TIER_HEALTH_FAMILY",
  "medical_conditions": ["TKN_MEDICAL_eadcb9ebb3274cba"],
  "dependents": [{"relation": "child", "age": 8, "notes_token": "TKN_NOTE_b79dae8d57adade3"}],
  "family_commitments": ["TKN_NOTE_..."],
  "obligations": ["TKN_NOTE_..."],
  "vault_protected": true
}
```

**Pass/fail — the important check**: none of the strings you sent
(`"diabetes"`, `"school fees..."`, etc.) may appear anywhere in the
response. Only `TKN_<TYPE>_<16 hex chars>` strings. `relation`/`age` DO stay
as plaintext — that's correct, not a bug (see README: they're low-cardinality
and never egress to an agent anyway). Calling this twice with the same
`user_id` **updates** the existing row (upsert), doesn't create a second one.

### `POST /profile/sensitive/reveal`
**Input**:
```json
{"user_id": "user_123", "caller": "demo-user", "purpose": "support ticket #42"}
```
`caller` must be in `VAULT_DETOKENIZE_ALLOWED_CALLERS` (.env). Requires
`X-Vault-Admin-Key` header.

**Expected output**: `200` + `SensitiveProfileRevealed` — the ORIGINAL
plaintext back (`"diabetes"`, `"school fees ₹2.5L/year"`, etc). **`404`** if
no profile exists for `user_id`. **`401`** if the admin header is missing/wrong.

**Pass/fail**: response must exactly match what you upserted. Then check
`GET /vault/audit` — one new row per token revealed (4 tokens above → 4 new
audit rows, all `granted: true`).

```bash
curl -s -X POST $BASE/profile/sensitive/reveal \
  -H "Content-Type: application/json" -H "X-Vault-Admin-Key: $ADMIN_KEY" \
  -d '{"user_id":"user_123","caller":"demo-user","purpose":"test"}'
```

---

## 6. Layer 2 — Vault

### `POST /vault/tokenize`  (admin)
**Input**:
```json
{"field_type": "NAME", "value": "Ramya G", "tenant_id": null}
```
`field_type` ∈ `NAME | PAN | ACCOUNT | EMPLOYER | PHONE | EMAIL | ADDRESS |
MEDICAL | RELATION | NOTE`. `value` non-empty. `tenant_id` optional (defaults
to `"default"`).

**Expected output**: `201`:
```json
{"token_id": "TKN_NAME_70e40750a7943130", "field_type": "NAME", "tenant_id": "default"}
```

**Pass/fail — determinism check**: call it AGAIN with the exact same
`value` → must return the **identical** `token_id`. Call it with
`"  ramya   g "` (different whitespace/case) → **also identical** `token_id`
(normalization). Call it with a different `field_type` for the same string
→ **different** `token_id`.

```bash
curl -s -X POST $BASE/vault/tokenize -H "Content-Type: application/json" \
  -H "X-Vault-Admin-Key: $ADMIN_KEY" -d '{"field_type":"NAME","value":"Ramya G"}'
# no header → expect 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/vault/tokenize \
  -H "Content-Type: application/json" -d '{"field_type":"NAME","value":"x"}'
```

### `POST /vault/detokenize`  (no admin key — caller/purpose gated instead)
**Input**:
```json
{"token_id": "TKN_NAME_70e40750a7943130", "caller": "demo-user", "purpose": "verify identity"}
```
`token_id`, `caller`, `purpose` all required non-empty strings.

**Expected output**: `200`:
```json
{"token_id": "TKN_NAME_70e40750a7943130", "value": "Ramya G"}
```
**`403`** if `caller` isn't on the allowlist. **`404`** if `token_id` doesn't
exist. **`410`** if the token was crypto-shredded. **`422`** if `caller` or
`purpose` is empty string.

**Pass/fail**: `value` must exactly equal the original input to `/tokenize`
(exact case/whitespace — the ORIGINAL is stored, only the LOOKUP is
normalized). Every call here — success or failure — must add exactly one row
to `GET /vault/audit`.

```bash
curl -s -X POST $BASE/vault/detokenize -H "Content-Type: application/json" \
  -d '{"token_id":"TKN_NAME_70e40750a7943130","caller":"demo-user","purpose":"test"}'
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/vault/detokenize \
  -H "Content-Type: application/json" \
  -d '{"token_id":"TKN_NAME_70e40750a7943130","caller":"random-unlisted","purpose":"test"}'
# expect 403
```

### `GET /vault/audit?limit=&offset=&token_id=`  (admin)
**Input**: `X-Vault-Admin-Key` header, plus query params, all optional. `limit` `1..200` default `50`.
`token_id` filters to one token's history.
**Expected output**: `200`:
```json
{"items": [{"id":"...","token_id":"TKN_NAME_...","tenant_id":"default","caller":"demo-user","purpose":"test","requested_at":"2026-08-16T12:52:47Z","granted":true,"denial_reason":null}], "total": 4, "limit": 50, "offset": 0}
```
**Pass/fail**: immutable — you cannot PATCH/DELETE any row (no such routes
exist; the DB itself rejects it at the trigger level). `granted:false` rows
must always carry a non-null `denial_reason`.

### `POST /vault/shred/{token_id}`  (admin)
**Input**: path = `token_id`. Body:
```json
{"reason": "DPDP erasure request"}
```
`reason` required non-empty.

**Expected output**: `200`:
```json
{"token_id": "TKN_NAME_...", "shredded_at": "2026-08-16T12:53:04Z", "reason": "DPDP erasure request"}
```
**`404`** if token doesn't exist. **`422`** if `reason` is empty.

**Pass/fail — this is the headline test**: after shredding, `POST
/vault/detokenize` on that same token MUST return **`410`**, forever — even
with the correct admin key, even by whoever holds the KEK/HSK. Any ledger
entry that referenced this token before shredding must still be readable via
`GET /ledger` (the ledger entry survives; only the token's plaintext is
gone).

```bash
curl -s -X POST $BASE/vault/shred/<token_id> -H "Content-Type: application/json" \
  -H "X-Vault-Admin-Key: $ADMIN_KEY" -d '{"reason":"test erasure"}'
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/vault/detokenize \
  -H "Content-Type: application/json" \
  -d '{"token_id":"<token_id>","caller":"demo-user","purpose":"x"}'   # expect 410
```

### `POST /vault/rotate-kek`  (admin)
**Input**:
```json
{"new_version": 2}
```
Never a raw key — server generates a fresh KEK internally under that version
number.

**Expected output**: `200`:
```json
{"to_version": 2, "rewrapped": 5, "remaining_on_old": 0}
```

**Pass/fail**: after rotation, EVERY previously-tokenized value must still
`/vault/detokenize` correctly (re-run the round-trip test above). Shredded
tokens are skipped (`rewrapped` count excludes them). Calling it again with
the same `new_version` → `rewrapped: 0` (idempotent, already current).

---

## 7. Layer 2 — Egress preview

### `GET /egress/preview?destination=&user_id=&caller=&purpose=`
**Input**: `destination` required, ∈ `DEBATE_AGENT | OPTIMIZER | USER_FACING`.
`user_id` optional (pulls in a `SensitiveProfile` if given). `caller`/`purpose`
required (+ `X-Vault-Admin-Key` header) only when `destination=USER_FACING`.

**Expected output** (`destination=OPTIMIZER`, no admin key needed):
```json
{"destination": "OPTIMIZER", "payload": {"savings": "800000.0", "recurring_income": "75000.0", ...}, "leaks": []}
```

**Expected output** (`destination=DEBATE_AGENT`, no admin key needed):
```json
{"destination": "DEBATE_AGENT", "payload": {"savings": "₹8–9 lakh", "recurring_income": "₹70k–80k", ...}, "leaks": []}
```

**Pass/fail — the whole point of the layer**:
- `leaks` must be `[]` for every destination, every time. A non-empty
  `leaks` array is a failed test, not a warning.
- `OPTIMIZER` shows exact `Decimal` amounts. `DEBATE_AGENT` shows the SAME
  underlying values as banded ranges (`"₹70k–80k"`, never the literal
  `"75000"`).
- If you passed `user_id` for a profile with `medical_conditions` set: those
  keys must be **absent entirely** from the `DEBATE_AGENT`/`OPTIMIZER`
  payload (not tokenized, not banded — dropped).
- `destination=USER_FACING` without admin key → `401`. Without
  `caller`/`purpose` → `422`. With everything → full plaintext, and it shows
  up in `GET /vault/audit`.

```bash
curl -s "$BASE/egress/preview?destination=OPTIMIZER"
curl -s "$BASE/egress/preview?destination=DEBATE_AGENT"
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/egress/preview?destination=USER_FACING"   # expect 401
```

---

## 8. Cross-cutting checks (run these after everything above)

| Check | How |
|---|---|
| Ledger truly immutable | Try any raw SQL `UPDATE`/`DELETE` on `ledger_entry` or `detokenization_audit` → both must raise (Postgres trigger fires even bypassing the API) |
| Vault row never holds plaintext | `SELECT ciphertext FROM vault_entries` — must be binary garbage, never a readable string |
| SensitiveProfile never holds plaintext | `SELECT medical_conditions FROM sensitive_profile` — only `TKN_*` strings |
| Every detokenize is audited | Count `/vault/detokenize` calls you made vs. `GET /vault/audit` row count — must match 1:1 |
| Full suite still green | `pytest` → `129 passed` |

---

## Quick reference — status codes across all endpoints

| Code | Meaning here |
|---|---|
| 200 / 201 | success (201 = created) |
| 401 | missing/wrong `X-Vault-Admin-Key` |
| 403 | `caller` not on the detokenize allowlist |
| 404 | id/token/profile not found |
| 409 | append-only violation (defensive; shouldn't normally trigger via API) |
| 410 | token was crypto-shredded — gone forever |
| 422 | request body failed validation (empty required field, bad enum, out-of-range) |
| 503 | vault keys not configured — check `.env` |
