import json
from pathlib import Path

import httpx
import pytest

from app.evidence.adapters import (
    AlphaVantageAdapter,
    CachedAdapter,
    FixtureReplayAdapter,
    FREDAdapter,
    LocalRegulatoryCorpus,
    fixture_path,
)
from app.evidence.cache import ResponseCache
from app.evidence.lenses import ALL_LENSES, QueryStub, dedupe_merge_tags
from app.evidence.personas import Persona
from app.evidence.questions import EvidenceQuestion, EvidenceTopic
from app.evidence.sources import EvidenceSource
from app.exceptions import EvidenceSourceError

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"


def _stub(source, query):
    return QueryStub(query=query, source=source, lens_tags=frozenset({Persona.SKEPTIC}))


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("network access attempted")

    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr("socket.socket.connect", boom)
    return calls


def test_fixture_replay_returns_recorded_content_with_zero_network(no_network):
    stub = _stub(EvidenceSource.FRED, "series:CPIAUCSL")
    rec = json.loads(fixture_path(FIXTURES, stub.source, stub.query).read_text(encoding="utf-8"))
    items = FixtureReplayAdapter(EvidenceSource.FRED, FIXTURES).fetch(stub)
    assert [i.content for i in items] == [r["content"] for r in rec["items"]]
    assert items[0].lens_tags == stub.lens_tags
    assert no_network == []


def test_fixture_miss_is_recorded_not_raised():
    a = FixtureReplayAdapter(EvidenceSource.FRED, FIXTURES)
    assert a.fetch(_stub(EvidenceSource.FRED, "series:NOPE")) == []
    assert a.misses == ["series:NOPE"]


def test_every_lens_stub_has_a_fixture():
    missing = []
    for topic in EvidenceTopic:
        q = EvidenceQuestion(topic=topic, text="")
        for s in dedupe_merge_tags([s for lens in ALL_LENSES for s in lens(q)]):
            if not fixture_path(FIXTURES, s.source, s.query).exists():
                missing.append((s.source.value, s.query))
    assert not missing


def test_local_corpus_deterministic_and_cited():
    c = LocalRegulatoryCorpus()
    stub = _stub(EvidenceSource.LOCAL_REGULATORY, "DICGC deposit insurance limit")
    a, b = c.fetch(stub), c.fetch(stub)
    assert [(i.content, i.citation) for i in a] == [(i.content, i.citation) for i in b]
    assert a[0].citation.startswith("local:dicgc_deposit_insurance@")


def test_fred_adapter_parses_mock_transport_and_hides_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"observations": [{"date": "2026-01-01", "value": "3.1"}]})

    a = FREDAdapter("SECRETKEY", client=httpx.Client(transport=httpx.MockTransport(handler)))
    (item,) = a.fetch(_stub(EvidenceSource.FRED, "series:CPIAUCSL"))
    assert "2026-01-01=3.1" in item.content
    assert "SECRETKEY" not in item.citation


def test_alpha_vantage_adapter_parses_mock_and_handles_rate_limit():
    ok = {
        "Meta Data": {"2. Symbol": "SPY"},
        "Monthly Time Series": {"2026-01-30": {"4. close": "500.0"}},
    }
    responses = iter([httpx.Response(200, json=ok), httpx.Response(200, json={"Note": "rate limit"})])
    a = AlphaVantageAdapter("SECRETKEY", client=httpx.Client(transport=httpx.MockTransport(lambda r: next(responses))))
    stub = _stub(EvidenceSource.ALPHA_VANTAGE, "function=TIME_SERIES_MONTHLY&symbol=SPY")
    (item,) = a.fetch(stub)
    assert "SPY" in item.content and "SECRETKEY" not in item.citation
    with pytest.raises(EvidenceSourceError, match="Note"):
        a.fetch(stub)  # throttle payload must fail loudly, not return []


def test_cached_adapter_hits_inner_once(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"observations": [{"date": "2026-01-01", "value": "1"}]})

    inner = FREDAdapter("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    cached = CachedAdapter(inner, ResponseCache(tmp_path / "c.sqlite"))
    stub = _stub(EvidenceSource.FRED, "series:UNRATE")
    first, second = cached.fetch(stub), cached.fetch(stub)
    assert len(calls) == 1
    assert first[0].content == second[0].content


def _mock_client(response: httpx.Response) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda r: response))


@pytest.mark.parametrize("field", ["Note", "Information", "Error Message"])
def test_alpha_vantage_error_payloads_raise_never_empty(field):
    a = AlphaVantageAdapter("SECRETKEY", client=_mock_client(httpx.Response(200, json={field: "nope"})))
    with pytest.raises(EvidenceSourceError):
        a.fetch(_stub(EvidenceSource.ALPHA_VANTAGE, "function=TIME_SERIES_MONTHLY&symbol=SPY"))


def test_alpha_vantage_payload_without_series_raises():
    a = AlphaVantageAdapter("SECRETKEY", client=_mock_client(httpx.Response(200, json={"Meta Data": {}})))
    with pytest.raises(EvidenceSourceError, match="no monthly series"):
        a.fetch(_stub(EvidenceSource.ALPHA_VANTAGE, "function=TIME_SERIES_MONTHLY&symbol=SPY"))


def test_fred_empty_observations_raises():
    a = FREDAdapter("SECRETKEY", client=_mock_client(httpx.Response(200, json={"observations": []})))
    with pytest.raises(EvidenceSourceError, match="no observations"):
        a.fetch(_stub(EvidenceSource.FRED, "series:CPIAUCSL"))


@pytest.mark.parametrize("adapter_cls,source,query", [
    (AlphaVantageAdapter, EvidenceSource.ALPHA_VANTAGE, "function=TIME_SERIES_MONTHLY&symbol=SPY"),
    (FREDAdapter, EvidenceSource.FRED, "series:CPIAUCSL"),
])
def test_http_errors_do_not_leak_api_key(adapter_cls, source, query):
    a = adapter_cls("SECRETKEY", client=_mock_client(httpx.Response(403)))
    with pytest.raises(EvidenceSourceError) as exc:
        a.fetch(_stub(source, query))
    assert "SECRETKEY" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
