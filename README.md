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
- `GET  /vault/audit` — paginated, immutable audit trail (admin)
- `POST /vault/shred/{token_id}` — admin — crypto-shred a token (irreversible)
- `POST /vault/rotate-kek` — admin — re-wrap all DEKs under a new KEK
- `GET  /egress/preview?destination=` — debug: see exactly what DEBATE_AGENT/OPTIMIZER/USER_FACING
  would receive for the current state, plus a live leak scan

**For a full manual test pass of every endpoint** — exact request bodies,
expected responses, and pass/fail criteria — see **[test.md](test.md)**.

## Tests

See [test.md](test.md) for a per-endpoint manual testing guide (setup, exact
inputs, expected outputs, pass/fail checks) — useful for a live demo or
debugging a single endpoint by hand. For the automated suite:

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
retrieval (added later in Layer 3a, below). `decompose()` remains a stub. `EgressPolicy.apply(payload,
DEBATE_AGENT)` is the clean seam Layer 3 will consume — this layer builds to
that interface and stops there.

# Layer 3a — Evidence Snapshot Service

Builds the evidence pipeline Layer 3b (the debate) will consume:
primitives -> evidence questions -> persona query lenses -> source adapters ->
canonicalize -> sha256 -> append-only snapshot. No LLM, no debate, no
optimizer.

## Why retrieval freezes before debate

Retrieval fires ONCE per hurdle, BEFORE any debate round, and is then frozen.
Agents in 3b read only from the frozen snapshot; they never call a live source
mid-debate. If retrieval were live during rounds, the debate-ON and debate-OFF
evaluation arms would see different evidence and the hallucination-rate
comparison would be invalid. `snapshot_hash` is the proof both arms saw
identical evidence, and it is what makes a debate replayable months later.
`tests/test_evidence_snapshot.py::test_same_hurdle_same_fixtures_same_hash` is
that proof, running fully offline.

## Persona-conditioned retrieval vs. G-DMAD's reasoning-strategy diversity

G-DMAD diversifies debaters by reasoning strategy (IO / CCoT / DDCoT). Those
axes lean on visual grounding, which a text-only financial domain does not
have. This project substitutes its own axis: persona-conditioned retrieval over
a shared question. Each persona's `QueryLens` (`app/evidence/lenses.py`) turns
the same `EvidenceQuestion` into different queries, so debaters hold genuinely
different evidence, not different tone over the same facts. A test asserts the
five lenses' query sets differ for every topic. Lenses only generate queries;
they never argue.

Locked archetypes: saver, liquidity-hawk, skeptic, opportunity-seeker,
contrarian. Locked group assignment (`DEBATE_GROUPS`):

| Group | Personas |
|-------|----------|
| Group 1 | skeptic, saver, liquidity-hawk |
| Group 2 | skeptic, opportunity-seeker, contrarian |

The skeptic is in both groups so neither is structurally groupthink-prone
(G-DMAD's failure case: a group with no adversarial role converging
confidently on a shared wrong answer). 3b runs 2 groups x 3 personas x 3 rounds.

## Primitive-driven questions

`derive_evidence_questions(primitives)` is pure and maps primitives, never a
hurdle "type", to topics: GOAL + ONE_TIME_OUTFLOW -> savings instruments;
RISK_CONSTRAINT -> emergency fund; RECURRING_OUTFLOW -> cost of living;
INFLOW_CHANGE (-) -> income smoothing, (+) -> investment capacity;
HORIZON_SHIFT -> opportunity cost of delay. Unrecognised primitives are
skipped, never raised on. Questions use primitive types only (never amounts,
dates or free-text reasons), so no user data reaches an outbound query.

## Why evidence snapshots are append-only like the ledger

`evidence_snapshot` and `evidence_item` use the same two-layer guard as
`ledger_entry` (the `@append_only` ORM guard plus a Postgres trigger). A
re-debate on updated evidence is a NEW snapshot (`force_refresh=true`), never a
mutation of the old one; old snapshots stay queryable via
`GET /evidence-snapshot/{id}`. Without `force_refresh`, building is idempotent
and returns the existing snapshot.

## Offline fixtures are mandatory for tests

Every adapter is servable by `FixtureReplayAdapter` from
`tests/fixtures/evidence/`. A free-tier API's rate limit (Alpha Vantage ~5
calls/min) cannot be the dependency a graded test suite relies on, and a frozen
snapshot's hash must be reproducible without the source being live.
`EVIDENCE_MODE=fixture` is the default and is forced in `tests/conftest.py`.
`live` mode adds a same-day SQLite response cache (`.cache/`) so development
runs don't burn quota; it is a convenience, the frozen snapshot is the system
of record. `scripts/record_evidence_fixtures.py [--live] [--check]` regenerates
or audits fixtures.

## The seam 3b will fill

A debating agent's context = `EvidenceSnapshot.items` +
`EgressPolicy.apply(state, DEBATE_AGENT)` (Layer 2's `generalize_for_debate()`).
This layer does NOT implement that combination; it is documented here only.
Evidence is public market/regulatory data, so it never passes through
EgressPolicy and its routes are not admin-gated, unlike the vault routes.

## Documented deviations

- No `AppendOnlyMixin` exists in Layer 2; the `@append_only` decorator and the
  generic `enforce_append_only()` trigger function (migration 002) are reused.
- `fetched_at` is excluded from the hash; a wall-clock field would defeat
  reproducibility by construction.
- Alpha Vantage and FRED fixtures are SYNTHETIC (marked `[SYNTHETIC]`) because
  no API keys were available to record real ones. Their shape mirrors the live
  adapters' output; `record_evidence_fixtures.py --live` replaces them.
  LOCAL_REGULATORY fixtures are real. Corpus excerpts are orientation
  summaries, not legal text; verify rates and rules against primary sources.
- The feasibility pre-check is a stub (`quick_feasibility_check` always returns
  True), called before `build_evidence_snapshot` in `run_evidence_pipeline` so
  the seam exists. Real infeasibility detection belongs to the optimizer
  (Layer 3d).

## Not addressed here

l-diversity / t-closeness-style gaps in the evidence itself (for example one
source's bias) are NOT addressed by this layer. Surfacing and challenging them
is the debate's job in 3b/3c, not retrieval's.

## Non-goals

No debate engine, no persona reasoning, no meta-evaluator, no real feasibility
logic, no combination of evidence with user state, no live-API test dependency.

---

# Layer 3b — Debate Engine (G-DMAD)

`app/debate/` runs a Group-DMAD debate over the frozen 3a evidence snapshot:
2 groups (`DEBATE_GROUPS`) x 3 personas x 3 rounds, temperature 0.3 (all in
`DebateConfig`). The LLM argues strategy; deterministic code computes.

- **Decision space** (`Plan`): integer allocation over CASH_SAVINGS / LIQUID_FUND /
  FIXED_DEPOSIT / EQUITY_INDEX summing to exactly 100, `monthly_saving_rate`
  0-100, `deadline_shift_months` 0-12, `emergency_buffer_months` 0-12. Agents
  also emit 1-6 `FACT`/`INFERENCE` claims (evidence handles + quoted figures),
  up to 4 rebuttals, and a summary.
- **Protocol** (G-DMAD Alg. 1): round 1 independent; round r>1 each agent sees its
  own and its group-mates' previous answers plus the *leader group's* consensus
  plan and top claims (cited evidence is appended to every pack). No direct
  cross-group chat. Persona briefs are frozen across rounds (`PROMPT_VERSION`
  hashes all templates and briefs).
- **Retrieval**: each agent's pack = snapshot items whose `lens_tags` include its
  persona. Handles `E1..En` follow the snapshot's canonical order, so equal
  `snapshot_hash` => identical evidence and handles.
- **Leader selection** is pluggable (`LeaderSelector`), called after every
  round. `UniformSelector` (default) = unweighted consensus, most valid agents
  leads. 3c supplies the evidence-weighted one.
- **Aggregation** (`aggregate_plans`): weighted mean allocation rounded to
  multiples of 5 by largest remainder (sums to exactly 100); weighted (lower)
  median for the other levers; `plan_distance` in 0..1.
- **Privacy**: `build_debate_context` projects state and primitives through
  `apply_egress(DEBATE_AGENT)`, then leak-scans the finished case against the
  exact source values (and label / raw_description / risk reasons as forbidden
  substrings). Any hit raises `DebateInputLeak`; nothing is sent.
- **Fail closed**: invalid JSON gets exactly one repair retry that quotes the
  validation error; still invalid => turn recorded as failed, excluded,
  `result.degraded = True`. A round with zero valid outputs raises
  `DebateFailed`. `LLM_PROVIDER=none` (default) raises `LLMNotConfigured`.
- **Persistence**: `debate_run` / `debate_turn` (migration 004), append-only via
  ORM guard + Postgres trigger. `save_debate_run`, `load_debate_result`.
- **Deviations**: `debate_run.degraded` and `debate_turn.seq` columns added
  (needed for a faithful round-trip); Anthropic/Ollama clients are not covered by
  tests (tests never touch network or a real model).

Run one real debate: set `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`,
optionally `LLM_MODEL`; or `LLM_PROVIDER=ollama`, `OLLAMA_BASE_URL`, `LLM_MODEL`
(see `.env.example`), then `run_debate(ctx, get_llm_client())`.

---

# Layer 3c — Evidence-Quality Meta-Evaluator

`app/debate/evaluator.py`. Fully deterministic -- no LLM judge (a judge model can
itself hallucinate). It scores every claim against the frozen snapshot.

- **FACT claim** is a hallucination (score 0) if it has `no_citation`, an
  `unknown_handle`, or an `ungrounded_figure` (a quoted figure not found
  verbatim, after whitespace/comma normalisation, in any cited item). Otherwise
  score = mean source weight of the distinct cited items (FRED 1.0,
  LOCAL_REGULATORY 1.0, ALPHA_VANTAGE 0.8) x figure-grounding ratio. The ratio is
  always reported; for unflagged claims it is necessarily 1.
- **INFERENCE** claims get a fixed 0.3 and are never hallucinations.
- Agent score = mean claim score; group score = mean agent score (valid agents).
- `hallucination_rate` = flagged FACT / total FACT over all valid turns, with
  per-round and per-persona breakdowns and raw counts (0.0 with no FACT claims).
- `EvidenceQualitySelector` (plugs into `run_debate(..., selector=...)`): weights
  = agent scores, consensus = `aggregate_plans(plans, weights)`, leader =
  highest group score. Tie -> consensus closer (`plan_distance`) to the other
  group(s), then configured order. With exactly two groups that distance is
  symmetric, so a two-group tie is always decided by configured order.
- `evaluate_debate(result, ctx) -> EvaluationReport` (JSON-serialisable): agent and
  group scores, hallucination metrics, flagged claims with reasons, agreement
  (1 - distance between final group consensuses), deduped candidate plans (each
  group's consensus + every valid final-round agent plan) with `support`, and
  `unresolved_dissent`.
- **unresolved_dissent (simple rule)**: a last-round rebuttal by SKEPTIC or
  CONTRARIAN is unresolved if its target persona did not change its plan (any
  lever) since the previous round. Targets are searched in the rebutter's group,
  else in all groups; resolved if any of them moved.

Not built here: optimizer, guardrails, recommendation endpoints, eval harness.

---

# Layer 3d — Deterministic Optimizer and Real Feasibility

`app/optimizer/`. Decimal-only, no LLM: the debate argued strategy, this computes.
Exact amounts reach this layer (OPTIMIZER egress) and the user's own view only.

- **Model** (`projection.py`, module docstring is the spec): monthly steps from
  `as_of`, capped at 120. Opening pool = `savings + liquid_cash` (falls back to
  `cash`; stocks are excluded). surplus = income - fixed outflows; saved =
  `max(surplus, 0) x saving_rate`; a negative surplus drains the pool. One-time
  flows land in their month; a GOAL is met if the pool *before* that month's
  outflows is >= target. `HORIZON_SHIFT` + `plan.deadline_shift_months` move goal
  deadlines and the outflows that share the goal's original date (goals have no
  ids, so a HORIZON_SHIFT applies to all goals). Withdrawals drain CASH, LIQUID,
  FD (3% exit penalty), EQUITY. Liquidity floor = max(RISK_CONSTRAINTs,
  buffer_months x fixed outflow), measured on CASH + LIQUID.
- **Scenarios**: BASE, MARKET_STRESS (EQUITY -30% the month before the first
  deadline), INCOME_STRESS (income x 0.7). `PlanEvaluation.scenarios_feasible` is 0-3.
- **Hard policy** (`policy.py`): EQUITY must be 0 if the first goal is < 12 months
  away and <= 20% if < 36. Violators are rejected (`violates_policy:*`), never ranked.
- **Ranking**: scenarios_feasible desc, goal_shortfall asc, deadline shift asc,
  debate support desc, terminal pool desc, `Plan.key()`.
- **Repair search**: if no debated candidate is base-feasible and policy-clean, the
  lever grid is searched for the least-change feasible plan (`plan_distance`).
- **Feasibility** (`assess_feasibility`): the best-case plan (100% saving, policy-clean,
  12-month shift) decides INFEASIBLE and returns the quantified gap: extra monthly
  saving (whole rupees, no delay) and months of delay needed. `quick_feasibility_check`
  wraps it and returns a bool; `run_evidence_pipeline` still ignores the bool
  (blocking is Layer 3e's job).

## Known limitations (read before trusting any number)

- All rates are **illustrative defaults** (CASH 3%, LIQUID 6%, FD 6.5%, EQUITY 11%):
  replace with verified rates. Every evaluation lists this in `assumptions_used`.
- Without `monthly_living_expense` the surplus is an upper bound (also listed).
- FRED and SPY/INDA evidence is US data; the regulatory corpus is an orientation
  summary not yet verified against primary sources.

---

# Layer 3e — Guardrails, Confidence, Human-Confirm

`POST /hurdles/{id}/recommendation` runs: evidence snapshot -> real feasibility
(`assess_feasibility`) -> debate (`EvidenceQualitySelector`) -> evaluator -> optimizer
ranks the debated candidates (repair search if none is feasible) -> guardrails ->
confidence -> an **immutable** `recommendation` row.

- **Infeasible hurdle**: persisted as `INFEASIBLE` with the quantified gap (extra
  monthly saving, months of delay) and **zero LLM calls**.
- **Leak**: a scanner hit while building the debate case, or in any outgoing model
  request (`_GuardedLLM` scans for exact amounts >= 10,000 and vault tokens), is
  persisted as `BLOCKED` with a `LEAK` finding. A leak raised mid-debate aborts
  the debate; it is never downgraded to a "dead agent".
- **Guardrails** (`app/guardrails.py`): LEAK, INFEASIBLE, POLICY_VIOLATION,
  NO_GROUNDED_EVIDENCE = BLOCK; PROMPT_INJECTION, HIGH_HALLUCINATION (> 0.34),
  INCOMPLETE_INPUT, DEGRADED_DEBATE = WARN; PLAN_REPAIRED = INFO. Every response
  carries "Decision support, not financial advice."
- **Confidence** in [0,1] = 0.30 x robustness (scenarios_feasible/3) + 0.25 x agreement
  + 0.25 x evidence quality of the agents behind the chosen plan + 0.20 x completeness
  (living expense given AND non-empty pool AND no failed turns; binary). The four
  components are returned. LOW < 0.5, HIGH > 0.75.
- **Human confirm**: `POST /recommendations/{id}/confirm {confirmed_by, acknowledge_warnings}`
  or `/reject {rejected_by, reason}`. BLOCK/INFEASIBLE -> 409; WARN findings or LOW
  confidence need `acknowledge_warnings=true` (else 409); a second decision -> 409
  (also a DB unique constraint); no LLM configured -> 503; unknown hurdle -> 404.
  Confirm writes exactly ONE ledger entry (COMMITMENT, `AGENT_SUGGESTED_USER_CONFIRMED`,
  `effect=[]`, evidence = recommendation id, debate run id, snapshot hash,
  citations, confidence) in the same transaction as the decision row, so
  `rebuild_state_from_ledger` is unchanged.
- **Tables** (migration 005): `recommendation`, `recommendation_decision`, both append-only
  (ORM guard + trigger). Status is derived: PENDING until a decision row exists.

Changes to earlier layers (needed for 3e): `run_debate` re-raises
`EgressPolicyViolation`; `build_debate_context` accepts an optional `state` override
(used by the eval harness); `LedgerEntryCreate` gained `evidence` and `append_entry`
a `commit=` flag.

Limitations: see "Layer 3d" (illustrative rates, US-centric FRED/SPY data,
unverified regulatory corpus). Confidence is a heuristic, not a calibrated probability.

---

# Evaluation Harness — debate ON vs OFF

`app/eval/`, `scripts/run_eval.py`, `eval/queries.sample.json`.

- **Query** = hurdle primitives + optional state + optional `monthly_living_expense` +
  labelled expectations `{must_be_feasible, max_equity_pct, min_buffer_months,
  must_shift_deadline}`. Date fields accept `"+Nd"` (as_of + N days) so a dataset never
  goes stale. `load_dataset` rejects malformed queries loudly (never drops one).
- **Two arms, ONE snapshot**: each query builds a single evidence snapshot and one
  leak-scanned `DebateContext`; both arms read it (their `snapshot_hash` is recorded and
  asserted equal).
  - *Debate ON* = the full pipeline (6 agents x 3 rounds, evidence-quality leader,
    evaluator, optimizer, guardrails, confidence).
  - *Debate OFF* = ONE generalist agent that sees **all** snapshot items; same
    `Plan`/claims schema, same evaluator, optimizer, guardrails. Its confidence
    "agreement" term is 1.0 (a lone agent cannot disagree), which flatters OFF.
  - A hurdle the optimizer proves infeasible short-circuits both arms identically with no LLM call.
- **Metrics per arm** (`app/eval/metrics.py`): hallucination_rate (pooled FACT claims,
  3c definition; ON counts every valid turn of every round), plan feasibility rate (best
  proposal feasible without repair), final feasible rate (after repair), policy-violation
  rate (violating proposals / proposals), label agreement, mean confidence, failed
  queries, LLM calls. Output = JSON + a Markdown table.
- **No real LLM, no results**: without a configured provider the runner exits with code 2
  unless `--dry-run`, which uses `ScriptedLLM` and stamps every output
  `DRY RUN - not results`.
- Eval creates hurdle rows labelled `eval:<id>` and evidence snapshots in the connected
  database (debate runs are not persisted).

```
python scripts/run_eval.py --dry-run                                  # plumbing check only
python scripts/run_eval.py --dataset eval/queries.sample.json --out eval/out.json   # real run
```

The 10 sample queries and their labels are **illustrative**; hand-label 30-50 before
reporting any comparison. With so few queries the rates carry wide uncertainty.
