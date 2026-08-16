"""
Test infrastructure.

DB strategy:
  - If TEST_DATABASE_URL is set, use it directly.
  - Otherwise, spin up a temporary Postgres container via testcontainers.

Migrations run once per session via Alembic. Tables are TRUNCATED between tests
using a separate connection — TRUNCATE bypasses Postgres row-level triggers
(which fire on DML, not DDL), so it works even on the append-only ledger_entry.

The ORM append-only guard (before_flush) is registered when app.models is imported.
"""
import os
import sys

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app.models  # noqa: F401 — registers ORM append-only guard


def _alembic_upgrade(db_url: str) -> None:
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
    TRUNCATE bypasses Postgres row-level triggers (which fire on DML, not DDL),
    so this works even on the append-only ledger_entry table.
    Not autouse — only activated when db_session is requested, so pure-Python
    tests (test_state_rebuild, test_decompose) never touch the DB.
    """
    yield
    with db_engine.connect() as conn:
        conn.execute(
            text("TRUNCATE TABLE sensitive_profile, ledger_entry, hurdle CASCADE")
        )
        conn.commit()


@pytest.fixture
def db_session(db_engine, clean_tables):
    with Session(db_engine) as session:
        yield session
