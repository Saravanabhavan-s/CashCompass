"""Pluggable evidence source adapters.

Every adapter is servable offline through FixtureReplayAdapter. Tests and the
reproducibility proof run ONLY against fixtures: a free-tier API's rate limit
(Alpha Vantage ~5 calls/min) cannot be the dependency a graded test suite
relies on, and a frozen snapshot's hash must be reproducible without the
source being live.

All evidence here is public market/regulatory data -- never the user's data --
so none of it passes through Layer 2's EgressPolicy. Outbound queries are
built from primitive types only (see questions.py), so no user data leaves
either.
"""
from __future__ import annotations

import abc
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from app.evidence.cache import ResponseCache
from app.exceptions import EvidenceSourceError
from app.evidence.lenses import QueryStub
from app.evidence.personas import Persona
from app.evidence.sources import EvidenceSource

CORPUS_DIR = Path(__file__).parent / "corpus"


class EvidenceItemData(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: EvidenceSource
    query: str
    lens_tags: frozenset[Persona]
    content: str
    citation: str
    fetched_at: datetime


class SourceAdapter(abc.ABC):
    source: EvidenceSource

    @property
    @abc.abstractmethod
    def version(self) -> str: ...

    @abc.abstractmethod
    def fetch(self, query: QueryStub) -> list[EvidenceItemData]: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _item(stub: QueryStub, content: str, citation: str, fetched_at: datetime | None = None) -> EvidenceItemData:
    return EvidenceItemData(
        source=stub.source,
        query=stub.query,
        lens_tags=stub.lens_tags,
        content=content,
        citation=citation,
        fetched_at=fetched_at or _now(),
    )


# --- Local regulatory corpus -------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"the", "a", "of", "and", "or", "to", "in", "for", "is", "are", "on", "by", "after", "before", "still"})


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOP}


class LocalRegulatoryCorpus(SourceAdapter):
    """Small bundled, versioned set of static documents searched locally
    (no network). Deterministic: keyword-overlap score, ties broken by doc id."""

    source = EvidenceSource.LOCAL_REGULATORY

    def __init__(self, corpus_dir: Path = CORPUS_DIR, top_k: int = 2):
        manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
        self._corpus_version: str = manifest["version"]
        self._top_k = top_k
        self._docs = [
            (d["id"], d["title"], (corpus_dir / d["file"]).read_text(encoding="utf-8"))
            for d in manifest["docs"]
        ]

    @property
    def version(self) -> str:
        return f"local-corpus@{self._corpus_version}"

    def fetch(self, query: QueryStub) -> list[EvidenceItemData]:
        q = _tokens(query.query)
        scored = []
        for doc_id, _title, text in self._docs:
            score = len(q & _tokens(text))
            if score > 0:
                scored.append((-score, doc_id, text))
        scored.sort()
        return [
            _item(query, text, f"local:{doc_id}@{self._corpus_version}")
            for _neg, doc_id, text in scored[: self._top_k]
        ]


# --- Live API adapters (never used by tests) ---------------------------------


def _checked_json(resp: httpx.Response, source: str) -> dict:
    """HTTP errors are re-raised WITHOUT chaining: httpx's own message embeds
    the full request URL, and both APIs carry the key in it."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        raise EvidenceSourceError(f"{source}: HTTP {resp.status_code}") from None
    return resp.json()


class AlphaVantageAdapter(SourceAdapter):
    """Free tier: ~5 calls/min, 25/day. Query grammar: 'function=F&symbol=S'."""

    source = EvidenceSource.ALPHA_VANTAGE
    BASE_URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self._key = api_key
        self._client = client or httpx.Client(timeout=15.0)

    @property
    def version(self) -> str:
        return "alphavantage-live-v1"

    def fetch(self, query: QueryStub) -> list[EvidenceItemData]:
        resp = self._client.get(f"{self.BASE_URL}?{query.query}&apikey={self._key}")
        data = _checked_json(resp, "ALPHA_VANTAGE")
        # Alpha Vantage reports throttling and errors as HTTP 200 + a message
        # field. That must fail loudly, never come back as an empty result.
        for field in ("Note", "Information", "Error Message"):
            if field in data:
                msg = str(data[field])
                if self._key:
                    msg = msg.replace(self._key, "***")
                raise EvidenceSourceError(f"ALPHA_VANTAGE {query.query}: {field}: {msg[:200]}")
        series_key = next((k for k in data if k.startswith("Monthly")), None)
        if not series_key:
            raise EvidenceSourceError(
                f"ALPHA_VANTAGE {query.query}: no monthly series in response (keys: {sorted(data)})"
            )
        rows = sorted(data[series_key].items(), reverse=True)[:12]
        symbol = data.get("Meta Data", {}).get("2. Symbol", "?")
        content = f"{symbol} monthly closes (latest 12): " + ", ".join(
            f"{d}={v['4. close']}" for d, v in rows
        )
        return [_item(query, content, f"{self.BASE_URL}?{query.query}")]  # citation never carries the key


class FREDAdapter(SourceAdapter):
    """Free tier. Query grammar: 'series:<ID>'."""

    source = EvidenceSource.FRED
    BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self._key = api_key
        self._client = client or httpx.Client(timeout=15.0)

    @property
    def version(self) -> str:
        return "fred-live-v1"

    def fetch(self, query: QueryStub) -> list[EvidenceItemData]:
        series_id = query.query.removeprefix("series:")
        params = {"series_id": series_id, "file_type": "json", "sort_order": "desc", "limit": 12}
        resp = self._client.get(self.BASE_URL, params={**params, "api_key": self._key})
        obs = _checked_json(resp, "FRED").get("observations", [])
        if not obs:
            raise EvidenceSourceError(f"FRED {series_id}: no observations returned")
        content = f"FRED {series_id} (latest 12): " + ", ".join(f"{o['date']}={o['value']}" for o in obs)
        return [_item(query, content, f"https://fred.stlouisfed.org/series/{series_id}")]


class CachedAdapter(SourceAdapter):
    """Dev-only wrapper: serves a same-day cached response instead of
    re-hitting a rate-limited API."""

    def __init__(self, inner: SourceAdapter, cache: ResponseCache):
        self._inner = inner
        self._cache = cache
        self.source = inner.source

    @property
    def version(self) -> str:
        return self._inner.version

    def fetch(self, query: QueryStub) -> list[EvidenceItemData]:
        hit = self._cache.get(self.source.value, query.query)
        if hit is not None:
            return [_item(query, r["content"], r["citation"]) for r in hit]
        items = self._inner.fetch(query)
        self._cache.put(self.source.value, query.query, [{"content": i.content, "citation": i.citation} for i in items])
        return items


# --- Fixture replay ------------------------------------------------------------


def fixture_path(fixture_dir: Path, source: EvidenceSource, query: str) -> Path:
    key = hashlib.sha256(f"{source.value}|{query}".encode("utf-8")).hexdigest()[:16]
    return Path(fixture_dir) / source.value / f"{key}.json"


class FixtureReplayAdapter(SourceAdapter):
    """Serves pre-recorded responses from tests/fixtures/evidence/ -- ZERO
    network calls, by construction (it never imports a client). A missing
    fixture returns [] and is recorded in `misses` so the snapshot's
    source_versions can surface it instead of silently shrinking."""

    def __init__(self, source: EvidenceSource, fixture_dir: str | Path):
        self.source = source
        self._dir = Path(fixture_dir)
        self.misses: list[str] = []
        manifest = self._dir / "manifest.json"
        self._fixture_version = (
            json.loads(manifest.read_text(encoding="utf-8")).get("version", "unversioned")
            if manifest.exists()
            else "unversioned"
        )

    @property
    def version(self) -> str:
        return f"fixture@{self._fixture_version}"

    def fetch(self, query: QueryStub) -> list[EvidenceItemData]:
        path = fixture_path(self._dir, query.source, query.query)
        if not path.exists():
            self.misses.append(query.query)
            return []
        rec = json.loads(path.read_text(encoding="utf-8"))
        recorded_at = datetime.fromisoformat(rec["recorded_at"])
        return [_item(query, r["content"], r["citation"], recorded_at) for r in rec["items"]]


def adapter_for(source: EvidenceSource, mode: str, *, fixture_dir: str | Path, api_keys: dict[str, str] | None = None, cache_path: str | Path | None = None) -> SourceAdapter:
    if mode == "fixture":
        return FixtureReplayAdapter(source, fixture_dir)
    keys = api_keys or {}
    if source == EvidenceSource.LOCAL_REGULATORY:
        return LocalRegulatoryCorpus()
    inner: SourceAdapter
    if source == EvidenceSource.ALPHA_VANTAGE:
        inner = AlphaVantageAdapter(keys.get("alpha_vantage", ""))
    else:
        inner = FREDAdapter(keys.get("fred", ""))
    return CachedAdapter(inner, ResponseCache(cache_path)) if cache_path else inner
