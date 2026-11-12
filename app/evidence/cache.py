"""Local response cache for LIVE adapters, keyed (source, query, UTC date).

A development convenience so repeated runs don't burn free-tier API quota.
It is NOT the system of record -- the frozen per-hurdle EvidenceSnapshot is.
Never consulted in fixture mode.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class ResponseCache:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                "source TEXT, query TEXT, day TEXT, payload TEXT, "
                "PRIMARY KEY (source, query, day))"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def get(self, source: str, query: str) -> list[dict] | None:
        c = self._conn()
        try:
            row = c.execute(
                "SELECT payload FROM cache WHERE source=? AND query=? AND day=?",
                (source, query, self._today()),
            ).fetchone()
        finally:
            c.close()
        return json.loads(row[0]) if row else None

    def put(self, source: str, query: str, payload: list[dict]) -> None:
        c = self._conn()
        try:
            with c:
                c.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                    (source, query, self._today(), json.dumps(payload)),
                )
        finally:
            c.close()
