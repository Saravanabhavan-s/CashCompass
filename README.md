# CashCompass — Decision Ledger, Hurdle Primitive Model, and PII Vault (Layer 1 + Layer 2)

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
python scripts/gen_vault_keys.py  # generate VAULT_KEKS / VAULT_HSK / VAULT_ADMIN_KEY, paste into .env
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
- `POST /profile/sensitive` — upsert sensitive profile — vault-protected; identity/health/family
  fields are tokenized before storage
- `POST /profile/sensitive/reveal` — plaintext projection, admin + caller/purpose required, audited
- `POST /vault/tokenize` — admin — tokenize a value (idempotent)
- `POST /vault/detokenize` — reverse a token; requires `caller` + `purpose`; always audited
- `GET  /vault/audit` — paginated, immutable audit trail
- `POST /vault/shred/{token_id}` — admin — crypto-shred a token (irreversible)
- `POST /vault/rotate-kek` — admin — re-wrap all DEKs under a new KEK
- `GET  /egress/preview?destination=` — debug: see exactly what DEBATE_AGENT/OPTIMIZER/USER_FACING
  would receive for the current state, plus a live leak scan

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
  to corrupt history; raw SQL alone cannot do it. (`DetokenizationAudit`,
  Layer 2's reverse-map log, shares the same guard — see
  `app/models/append_only.py`.)
- **Financial state** is a projection: `rebuild_state_from_ledger()` folds
  the ledger into a `FinancialState` snapshot. State is never edited directly.
  It needed **no changes** for Layer 2 — see "The primitive model is the
  privacy boundary" below.
- **Sensitive profile** is tier `TIER_HEALTH_FAMILY` per India's DPDP Act 2023,
  and is now fully vault-backed: `medical_conditions`/`family_commitments`/
  `obligations` hold tokens, `dependents[].notes_token` is a token,
  `relation`/`age` stay cleartext (see "Two-tier egress policy" below for why
  that's safe).
- `decompose(raw_description)` is a stub — signature defined, raises
  `NotImplementedError`. Layer 3 (LLM layer) will implement NL decomposition.
  Layer 2 does not touch this — see "Explicit non-goals" below.

---

# Layer 2 — PII Tokenization Vault

Layer 1 left an explicit seam: `SensitiveProfile` was tagged `TIER_HEALTH_FAMILY`
but stored as plaintext JSON, and nothing was encrypted anywhere. Nothing
sensitive may reach an LLM until that seam is closed.

Two distinct problems, two distinct mechanisms — this layer builds both:
- **Encryption** (envelope encryption with per-record DEKs) protects direct
  identifiers *at rest*.
- **Abstraction** (a destination-aware egress policy) protects against
  *re-identification in the prompt*. Encrypting a name doesn't help when
  "male, marrying in 8 months, ₹8L savings, ₹75k salary, Chennai" is already
  near-unique — that threat has nothing to do with encryption.

## Key hierarchy

```
plaintext ──AES-256-GCM(key=DEK, nonce=N1, aad)──▶ ciphertext + tag
    DEK    ──AES-256-GCM(key=KEK, nonce=N2, wrap-aad)──▶ wrapped_dek + tag
    KEK    resolved from KeyProvider at startup — NEVER stored in the DB
    HSK    ──HMAC-SHA256──▶ blind index (the deterministic token)
```

Three keys, three jobs, no overlap:

| Key | Size | Job | Never does |
|---|---|---|---|
| **DEK** | 256-bit, random, **per record** | encrypts the plaintext | gets stored unwrapped |
| **KEK** | 256-bit | wraps DEKs only | **encrypt data directly** |
| **HSK** | 256-bit | HMAC blind index (tokenization) | provide integrity (GCM's tag already does that) |

## Why the KEK never encrypts data

If the KEK touched plaintext, rotating it would force re-encrypting every row —
O(rows × plaintext size). Wrapping only DEKs makes rotation O(rows × 32 bytes):
unwrap the old DEK, re-wrap under the new KEK, done. `rotate_kek()` touches
`wrapped_dek`/`dek_nonce`/`dek_auth_tag`/`kek_version` and **nothing else** —
`test_vault_rotation.py::test_rotate_kek_leaves_data_columns_unchanged` snapshots
`ciphertext`/`nonce`/`auth_tag`/`key_version` before and after and asserts they
are byte-identical. That test is the guard against the classic envelope-
encryption mistake.

This is also why `vault_entries` carries **two** version columns, not one:
`key_version` is frozen at row creation and bound into the data AAD (see
below); `kek_version` records which KEK currently wraps the DEK and changes on
rotation. Folding them into one column would make the AAD change on every
rotation, forcing a full re-encrypt — the exact bug this split exists to avoid.

## Why deterministic tokens, not random IDs

```python
def tokenize(field_type, plaintext) -> str:
    n = normalize(field_type, plaintext)
    digest = HMAC-SHA256(key=tenant_hsk, msg=f"{field_type}:{n}")
    return f"TKN_{field_type}_{digest.hex()[:16]}"
```

The same person appearing in two ledger entries must produce the **same**
token, or an LLM reading both treats them as two different people and all
cross-entry reasoning breaks. Random per-call IDs would defeat the entire
purpose. Two worked details:

- **Type-prefixed** (`TKN_NAME_…`, never a bare `TKN_…`) — models reason
  measurably better over typed placeholders.
- **`field_type` is baked inside the HMAC message**, not just the string
  prefix, so `"acme corp"` as a `NAME` and `"acme corp"` as an `EMPLOYER`
  are cryptographically distinct, not just cosmetically distinct.

**The honest cost:** a deterministic blind index leaks *equality and
frequency*. An attacker with the database but not the HSK learns which
records share a value and how often each appears, even without ever learning
what the value is. That's the price of cross-entry reasoning, paid knowingly
— see the threat model below.

**Per-tenant derivation:** the formula above uses `tenant_hsk`, an HKDF
derivation of the root HSK salted by `tenant_id`, not the root HSK directly.
Without this, two tenants tokenizing the same PAN would derive the *identical*
`token_id` — a primary-key collision, and the loser's AAD would never match
the winner's stored row (a silent, permanent decryption failure). HKDF keeps
the spec's HMAC formula byte-for-byte unchanged while making tokens
tenant-unique; with the single `default` tenant this prototype ships with, it
degenerates to exactly that formula.

## AAD binding — defeats row-swap attacks

GCM's auth tag stops ciphertext *tampering*, but not an attacker with DB write
access copying row A's ciphertext into row B — both would decrypt fine under
their own DEK. Binding the ciphertext to its identity closes that:

```
data AAD = f"{token_id}|{field_type}|{key_version}|{tenant_id}"
wrap AAD = f"{token_id}|{tenant_id}|dek-wrap"   (deliberately excludes kek_version — see above)
```

`test_vault_aad.py::test_swapping_two_rows_ciphertext_breaks_both` performs
exactly that raw-SQL swap and asserts **both** rows now fail to decrypt.

## Nonce discipline

A fresh 96-bit CSPRNG nonce for **every** encryption operation, never derived
from a counter, timestamp, or row id. Reuse under the same GCM key leaks the
authentication subkey and permits forgery of arbitrary ciphertexts —
catastrophic, not degraded. `crypto.encrypt()` generates its own nonce and
does not accept one as a parameter, so the discipline is enforced
structurally, not by comment. `test_vault_crypto.py::test_nonce_unique_across_10000_encryptions`
generates 10,000 nonces and asserts none collide.

## Two-tier egress policy

The heart of the layer — a single explicit table
(`app/vault/egress.py::EgressPolicy`), not scattered conditionals, projecting
a payload differently per destination:

| kind / tier | `DEBATE_AGENT` | `OPTIMIZER` | `USER_FACING` |
|---|---|---|---|
| Identity (name, PAN, account, employer, phone, email, address) | token, unchanged | token, unchanged | detokenized (audited) |
| Financial magnitude (savings, income, amounts) | **banded string** (`"₹8–9 lakh"`) | **exact `Decimal`** | exact `Decimal` |
| Structural (dates, cadence, type, priority) | pass through | pass through | pass through |
| Health/family (medical, dependents, family commitments) | **dropped entirely** | **dropped entirely** | detokenized (audited) |
| Unclassified field | **dropped** (default-deny) | dropped | pass through |

Financial magnitudes pass through **in cleartext** to `OPTIMIZER` — a
deliberate, documented decision. The LLM cannot plan without them, and
tokenizing a number would break the entire system (there's nothing to look up
"₹75,000" against). But those same magnitudes are **banded** before reaching
`DEBATE_AGENT` — see the next section for why.

Health/family fields are **dropped**, not passed as tokens, to agent
destinations — even though the spec text ("tokenized and abstracted... never
in literal form") would technically permit emitting a token. A
`TKN_MEDICAL_…` string still discloses that a medical condition exists and
that two records share the same one — the blind index's equality-preserving
property working *against* privacy here. Tokens are how health/family data is
*stored*; Layer 1 primitives (`RECURRING_OUTFLOW`, `RISK_CONSTRAINT`) are how
it *egresses*.

`relation`/`age` on a dependent stay cleartext deliberately — tokenizing
`"child"` wouldn't protect it (anyone with the HSK can dictionary-attack a
value that low-entropy); the real protection is that the whole `dependents`
object is `TIER_HEALTH_FAMILY` and therefore never egresses to an agent at
all.

**Default deny**, always: `classify()` treats an unrecognized field name as
`TIER_HEALTH_FAMILY` — the most restrictive class — so a field added upstream
later fails closed, not open. This isn't hypothetical: Layer 1's own
`label`/`description`/`reason`/`raw_description` fields are deliberately left
*unclassified* rather than marked safe, because they routinely carry an
employer or bank name in practice (`"HDFC home loan"`, `"Got a raise at
Acme"`). `test_egress_policy.py::test_default_deny_unknown_field_is_health_family`
guards the mechanism itself; `test_unclassified_field_not_emitted_to_debate_agent`
guards it against a real field, not a synthetic one.

## Generalization for debate agents

`generalize_for_debate()` bands every financial magnitude before it reaches a
debating agent, using one-significant-figure flooring:
`band_width = 10 ** floor(log10(|amount|))`.

- `₹75,000` → floor to the nearest ₹10k → **`"₹70k–80k"`**
- `₹8,00,000` → floor to the nearest ₹1 lakh → **`"₹8–9 lakh"`**
- an 8-month horizon → **`"6–12 months"`**

Encryption is the wrong tool for this: a debate agent that receives an exact
₹8,00,000 savings figure, an exact ₹75,000 income, and a 14-month care
commitment can single out an individual from a population without ever
learning a name. Banding is a k-anonymity-style generalization over
quasi-identifiers — each released value maps to an equivalence class, not a
point.

**Stated rather than hidden:** k-anonymity does not bound *attribute*
disclosure when an equivalence class is homogeneous (l-diversity addresses
that — **not implemented**), nor bound how far a class's attribute
distribution sits from the population's (t-closeness — **not implemented**
either). One-significant-figure banding releases roughly 3.2 bits per value;
across several correlated financial fields that adds up. Widen the bands, or
drop fields, before this faces a real population. Full precision goes
**only** to the deterministic optimizer, never to a debating agent.

## The primitive model is the privacy boundary

This is the layer's most elegant property, and it wasn't planned as a privacy
feature — Layer 1 built it to avoid enumerating life events (see "Why every
life event is a primitive composition" above).

`"chemotherapy for user's mother, ₹18k/month, 14 months"` was already going to
be stored as `RECURRING_OUTFLOW(amount=18000, cadence=MONTHLY, ...)` +
`RISK_CONSTRAINT(min_liquidity=X, reason="ongoing_care")` — a decision made
for modelling reasons, before Layer 2 existed. Privacy wasn't bolted on
afterward; the abstraction already *was* the protection. Layer 2's
`abstract_health_family()` only had to enforce a boundary Layer 1's data model
had already drawn — and to close one gap: `RiskConstraint.reason` is a free
`str` in the schema, so a `reason_code` allowlist (`ongoing_care`,
`dependent_support`, `family_commitment`, `unclassified`) is enforced at
abstraction time, because a free-text reason is a re-identification channel
wearing a primitive's clothes.

One honest limitation: `abstract_health_family()` consumes **structured**
input only (`{amount, cadence, duration_months, reason_code, min_liquidity}`),
never natural language. Reducing raw text like the sentence above to that
structure is `decompose()` — Layer 3's job, explicitly out of scope here (see
"Explicit non-goals"). Layer 2's contribution is the *guarantee* that only
primitives cross the egress boundary, not the NL reduction itself.

## Crypto-shredding: reconciling an immutable ledger with DPDP erasure

An append-only ledger and a legal right to erasure look like a contradiction.
Per-record DEKs resolve it: destroying a DEK makes its ciphertext permanently
unrecoverable — **mathematically**, not by policy or access control.

```
crypto_shred(token_id, reason):
    wrapped_dek, dek_nonce, dek_auth_tag ← NULL
    shredded_at ← now()
    write an audit row (granted=False, denial_reason="SHREDDED: ...")
```

The ledger entry referencing the token, the token itself, and the full audit
trail all survive untouched — only the personal data is gone.
`test_vault_shred.py::test_shredded_value_unrecoverable_with_kek_and_hsk`
holds both real keys after shredding and shows there is no path left from
`(ciphertext, KEK, HSK)` to plaintext: the DEK was never stored anywhere else,
and once `wrapped_dek` is `NULL` there is nothing left to unwrap.

**One gap, stated plainly:** free-text `LedgerEntry.description` fields
written *before* `append_entry_tokenized()` existed (or via the still-present
`append_entry()`) cannot be retroactively tokenized or shredded — the
ledger's append-only invariant forbids editing them. The fix here is
prevention, not erasure: `description` is `DEFAULT_DENY` classified, so even
an untokenized description never reaches an agent destination, but it does
still exist in the database in cleartext until write-time tokenization is
adopted everywhere.

## Threat model — stated honestly

**Defends against:**
- Database compromise or exfiltration (ciphertext is useless without the KEK)
- Backup theft (same reason)
- A DBA with read access to the database (sees ciphertext + tokens, not
  plaintext)
- Row-swap tampering (AAD binding — see above)
- Re-identification via *direct* identifiers (name, PAN, account, etc. are
  tokenized, never emitted to an agent)

**Does NOT defend against:**
- App-server compromise at runtime — the KEK, HSK, and every unwrapped DEK
  exist in process memory during use
- A malicious or compromised LLM provider correlating generalized financial
  magnitudes across many requests
- Re-identification from an unusual combination of quasi-identifiers beyond
  what the generalization bands cover
- The KEK/HSK living in an env var / OS keyring rather than a KMS or HSM (see
  `KmsKeyProvider`'s docstring for the production path)
- A self-asserted `caller` on `/vault/detokenize` — there is no real
  authentication in this prototype, so the audit log records what a caller
  *claimed*, not a verified identity
- Equality/frequency leakage inherent to a deterministic blind index (see
  "Why deterministic tokens" above)
- A dictionary attack on low-entropy `MEDICAL`/`RELATION`/`NOTE` tokens by
  anyone holding the HSK
- Free-text ledger descriptions written before tokenization was adopted
- Everything running under a single `default` tenant in this prototype

**Overclaiming here is worse than the limitation itself.**

## Documented deviations from the spec

- **`key_version` vs `kek_version`** — split into two columns; see "Why the
  KEK never encrypts data" above.
- **Per-tenant HSK via HKDF** — see "Why deterministic tokens" above.
- **Wrap AAD** — the spec's DEK wrap takes no AAD; this implementation adds
  one (`token_id|tenant_id|dek-wrap`) as a documented superset, closing a
  second-order row-swap path on `wrapped_dek` itself.
- **Two extra `VaultFieldType` members** (`MEDICAL`, `RELATION`, `NOTE`) —
  the spec's seven field types are all identity types; `SensitiveProfile`'s
  health/family fields need their own.
- **Health/family tokens dropped, not passed, to agent destinations** — see
  "Two-tier egress policy" above.
- **64-bit token digest** — birthday collision at ~2^32 tokens per field type
  per tenant. Acceptable at prototype scale; widen before production.
- **`TIMESTAMPTZ` on new tables only** — Layer 1 used naive `DateTime`
  (`ledger_entry.created_at`, `hurdle.created_at`); Layer 2's new tables use
  timezone-aware timestamps because an audit trail with an ambiguous
  timezone isn't one. Layer 1's columns are deliberately not retrofitted —
  a separate, riskier migration with no Layer 2 payoff. Never compare an
  aware `detokenization_audit.requested_at` to a naive `ledger_entry.created_at`
  without normalizing first.

## Design anchor

The context-aware, destination-dependent egress policy in this layer follows
the CAPRI framework's approach to privacy in multi-agent generative AI
systems: what a payload contains is not a single fact, it's a function of
*where it's going*.

## Explicit non-goals

No LLM calls. No debate agents or personas. No optimizer/solver. No evidence
retrieval. `decompose()` remains a stub. `EgressPolicy.apply(payload,
DEBATE_AGENT)` is the clean seam Layer 3 will consume — this layer builds to
that interface and stops there.
