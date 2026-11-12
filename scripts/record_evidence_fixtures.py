"""Record evidence fixtures for tests/fixtures/evidence/.

  python scripts/record_evidence_fixtures.py             # LOCAL from corpus; AV/FRED synthetic
  python scripts/record_evidence_fixtures.py --live      # AV/FRED recorded from live APIs (needs keys)
  python scripts/record_evidence_fixtures.py --check     # list missing fixtures, write nothing

Without API keys, ALPHA_VANTAGE and FRED fixtures are SYNTHETIC: deterministic
placeholder series whose shape mirrors the real adapters' output. They exist
so the suite runs offline; they are not real market data. LOCAL_REGULATORY
fixtures are real (generated from the bundled corpus).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.evidence.adapters import (  # noqa: E402
    AlphaVantageAdapter,
    FREDAdapter,
    LocalRegulatoryCorpus,
    fixture_path,
)
from app.evidence.lenses import ALL_LENSES, QueryStub, dedupe_merge_tags  # noqa: E402
from app.evidence.questions import EvidenceQuestion, EvidenceTopic  # noqa: E402
from app.evidence.sources import EvidenceSource  # noqa: E402
from app.exceptions import EvidenceSourceError  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "evidence"
FIXTURE_VERSION = "2026.10-v1"
RECORDED_AT = "2026-10-21T00:00:00+00:00"  # synthetic runs: fixed so re-recording is diff-stable
AV_MIN_INTERVAL_S = 15.0  # free tier: ~5 calls/min and a 1 call/s burst limit
_last_av_call = [0.0]


def _live_stamp() -> tuple[str, str]:
    """Live runs stamp the REAL UTC date (day granularity, so re-recording on
    the same day is still diff-stable) and a distinct manifest version."""
    today = datetime.now(timezone.utc).date()
    return f"{today.isoformat()}T00:00:00+00:00", f"{today:%Y.%m}-live-v1"


def all_stubs() -> list[QueryStub]:
    stubs: list[QueryStub] = []
    for topic in EvidenceTopic:
        q = EvidenceQuestion(topic=topic, text="")
        for lens in ALL_LENSES:
            stubs += lens(q)
    return dedupe_merge_tags(stubs)


def _synthetic(stub: QueryStub) -> list[dict]:
    seed = int(hashlib.sha256(stub.query.encode()).hexdigest()[:8], 16)
    vals = [round(1 + ((seed >> (i % 16)) % 900) / 100, 2) for i in range(12)]
    months = [f"2025-{m:02d}-28" for m in range(12, 0, -1)]
    series = ", ".join(f"{d}={v}" for d, v in zip(months, vals))
    if stub.source == EvidenceSource.FRED:
        sid = stub.query.removeprefix("series:")
        return [{"content": f"[SYNTHETIC] FRED {sid} (latest 12): {series}", "citation": f"https://fred.stlouisfed.org/series/{sid}"}]
    symbol = stub.query.split("symbol=")[-1]
    return [{"content": f"[SYNTHETIC] {symbol} monthly closes (latest 12): {series}", "citation": f"https://www.alphavantage.co/query?{stub.query}"}]


def _record(stub: QueryStub, live: bool, local: LocalRegulatoryCorpus) -> list[dict]:
    if stub.source == EvidenceSource.LOCAL_REGULATORY:
        items = local.fetch(stub)
    elif live and stub.source == EvidenceSource.FRED:
        items = FREDAdapter(settings.fred_api_key.get_secret_value()).fetch(stub)
    elif live:
        wait = AV_MIN_INTERVAL_S - (time.monotonic() - _last_av_call[0])
        if wait > 0 and _last_av_call[0]:
            time.sleep(wait)
        try:
            items = AlphaVantageAdapter(settings.alpha_vantage_api_key.get_secret_value()).fetch(stub)
        finally:
            _last_av_call[0] = time.monotonic()
    else:
        return _synthetic(stub)
    if live and not items:
        raise EvidenceSourceError(f"{stub.source.value} {stub.query}: live call returned no evidence")
    return [{"content": i.content, "citation": i.citation} for i in items]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    stubs = all_stubs()
    if args.check:
        missing = [s for s in stubs if not fixture_path(FIXTURE_DIR, s.source, s.query).exists()]
        for s in missing:
            print(f"MISSING {s.source.value}: {s.query}")
        print(f"{len(stubs) - len(missing)}/{len(stubs)} fixtures present")
        return 1 if missing else 0

    recorded_at, version = _live_stamp() if args.live else (RECORDED_AT, FIXTURE_VERSION)
    local = LocalRegulatoryCorpus()

    # All-or-nothing: record everything in memory first. A single failed live
    # call aborts BEFORE any file is touched, so the fixture dir can never end
    # up as a mix of fresh, stale, and empty fixtures.
    records: list[tuple[Path, dict]] = []
    failures: list[str] = []
    for s in stubs:
        try:
            items = _record(s, args.live, local)
        except EvidenceSourceError as exc:
            failures.append(str(exc))
            continue
        rec = {"source": s.source.value, "query": s.query, "recorded_at": recorded_at, "items": items}
        records.append((fixture_path(FIXTURE_DIR, s.source, s.query), rec))

    if failures:
        for f in failures:
            print(f"FAILED {f}")
        print(f"{len(failures)} live call(s) failed; NOTHING was written. Wait a minute and re-run.")
        return 1

    for path, rec in records:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (FIXTURE_DIR / "manifest.json").write_text(json.dumps({"version": version}) + "\n", encoding="utf-8")
    print(f"wrote {len(stubs)} fixtures to {FIXTURE_DIR} (version {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
