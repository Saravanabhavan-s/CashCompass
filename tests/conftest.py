"""
Test infrastructure.

DB strategy:
  - If TEST_DATABASE_URL is set, use it directly.
  - Otherwise, spin up a temporary Postgres container via testcontainers.

Migrations run once per session via Alembic. Tables are TRUNCATED between tests
using a separate connection — TRUNCATE bypasses Postgres row-level triggers
(which fire on DML, not DDL), so it works even on the append-only ledger_entry
and detokenization_audit tables.

The ORM append-only guard (before_flush) is registered when app.models is
imported.
"""
import base64
import json
import os
import sys

# --- Layer 2 vault env, MUST be set before anything imports app.config -----
# `settings` (app/config.py) is a module-level singleton built at import
# time, so env vars set after the first `import app...` are invisible to it.
# This block must stay ABOVE `import app.models` below — and above any test
# module's own imports, which is why it lives here rather than in a fixture.
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


os.environ.setdefault("VAULT_KEY_SOURCE", "env")
os.environ.setdefault("VAULT_KEKS", json.dumps({"1": _b64(b"\x01" * 32), "2": _b64(b"\x02" * 32)}))
os.environ.setdefault("VAULT_KEK_VERSION", "1")
os.environ.setdefault("VAULT_HSK", _b64(b"\x03" * 32))
os.environ.setdefault("VAULT_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("DEFAULT_TENANT_ID", "default")
os.environ.setdefault("VAULT_DETOKENIZE_ALLOWED_CALLERS", "test-caller,demo-user")

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app.models  # noqa: F401 — registers ORM append-only + VaultProtected guards
from app.vault.keys import reset_key_provider


def _alembic_upgrade(db_url: str) -> None:
    # ALEMBIC_DATABASE_URL takes priority over settings.database_url inside
    # alembic/env.py — necessary because `settings` is a module-level
    # singleton already frozen (from .env) by the time this test DB URL is
    # known, so passing it only via cfg.set_main_option is not enough.
    os.environ["ALEMBIC_DATABASE_URL"] = db_url
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def db_engine():
    test_url = os.environ.get("TEST_DATABASE_URL")
    if test_url:
        engine = create_engine(test_url)
        _alembic_upgrade(test_url)
        yield engine
        engine.dispose()
    else:
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer("postgres:16") as pg:
            url = pg.get_connection_url()
            engine = create_engine(url)
            _alembic_upgrade(url)
            yield engine
            engine.dispose()


@pytest.fixture
def clean_tables(db_engine):
    """
    Truncate all tables after each test.
    TRUNCATE bypasses Postgres row-level triggers (which fire on DML, not
    DDL), so this works even on the append-only ledger_entry and
    detokenization_audit tables.
    Not autouse — only activated when db_session is requested, so pure-Python
    tests (test_state_rebuild, test_decompose, and the vault crypto/normalize/
    egress suites) never touch the DB.
    """
    yield
    with db_engine.connect() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE detokenization_audit, vault_entries, "
                "sensitive_profile, ledger_entry, hurdle CASCADE"
            )
        )
        conn.commit()


@pytest.fixture
def db_session(db_engine, clean_tables):
    with Session(db_engine) as session:
        yield session


@pytest.fixture
def vault_keys():
    """Deterministic test KEK(s)/HSK, matching the env block above. Resets
    the KeyProvider singleton before and after so tests that mutate it
    (rotation) never bleed into other tests."""
    reset_key_provider()
    yield
    reset_key_provider()


@pytest.fixture
def admin_headers():
    return {"X-Vault-Admin-Key": "test-admin-key"}


@pytest.fixture
def client(db_engine, clean_tables, vault_keys):
    """No TestClient fixture existed before Layer 2 — app/db.py builds its
    engine at import time from settings.database_url, but the testcontainer
    URL isn't known until db_engine runs. So the DB dependency must be
    overridden. Import app.main INSIDE the fixture so module import can't
    beat the env block at the top of this file."""
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.main import app

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
