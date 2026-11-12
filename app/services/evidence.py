"""Evidence snapshot service: retrieve ONCE per hurdle, hash, freeze.

The invariant everything here protects: agents in Layer 3b read ONLY from the
frozen snapshot and never call a live source during debate rounds. If
retrieval were live during rounds, debate-ON and debate-OFF evaluation arms
could see different evidence and the hallucination-rate comparison would be
invalid. snapshot_hash is the proof both arms saw identical evidence.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.evidence.adapters import EvidenceItemData, FixtureReplayAdapter, SourceAdapter, adapter_for
from app.evidence.canonical import snapshot_hash
from app.evidence.feasibility import quick_feasibility_check
from app.evidence.lenses import ALL_LENSES, QueryStub, dedupe_merge_tags
from app.evidence.questions import derive_evidence_questions
from app.evidence.sources import EvidenceSource
from app.exceptions import HurdleNotFound
from app.models.evidence import EvidenceItem, EvidenceSnapshot
from app.models.hurdle import Hurdle
from app.services.state import rebuild_state_from_ledger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_adapters() -> dict[EvidenceSource, SourceAdapter]:
    fixture_dir = Path(settings.evidence_fixture_dir)
    if not fixture_dir.is_absolute():
        fixture_dir = _PROJECT_ROOT / fixture_dir
    keys = {
        "alpha_vantage": settings.alpha_vantage_api_key.get_secret_value(),
        "fred": settings.fred_api_key.get_secret_value(),
    }
    return {
        s: adapter_for(
            s,
            settings.evidence_mode,
            fixture_dir=fixture_dir,
            api_keys=keys,
            cache_path=settings.evidence_cache_path,
        )
        for s in EvidenceSource
    }


def get_latest_snapshot(db: Session, hurdle_id: str) -> EvidenceSnapshot | None:
    return db.scalars(
        select(EvidenceSnapshot)
        .where(EvidenceSnapshot.hurdle_id == hurdle_id)
        .order_by(EvidenceSnapshot.created_at.desc(), EvidenceSnapshot.id.desc())
        .limit(1)
    ).first()


def get_snapshot(db: Session, snapshot_id: str) -> EvidenceSnapshot | None:
    return db.get(EvidenceSnapshot, snapshot_id)


def build_evidence_snapshot(
    db: Session,
    hurdle_id: str,
    *,
    force_refresh: bool = False,
    adapters: dict[EvidenceSource, SourceAdapter] | None = None,
) -> EvidenceSnapshot:
    """Idempotent per hurdle: a second call returns the existing snapshot.
    force_refresh=True INSERTS a new, separately hashed snapshot and leaves
    every older one untouched (append-only, mirroring the ledger's
    compensating-entry philosophy)."""
    hurdle = db.get(Hurdle, hurdle_id)
    if hurdle is None:
        raise HurdleNotFound(f"Hurdle {hurdle_id} not found.")

    # Serialize concurrent builds for one hurdle so idempotency is race-safe.
    # Transaction-scoped: released on commit/rollback.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"evidence:{hurdle_id}"})

    if not force_refresh:
        existing = get_latest_snapshot(db, hurdle_id)
        if existing is not None:
            db.rollback()  # release the advisory lock
            return existing

    mode = "custom" if adapters else settings.evidence_mode
    adapters = adapters or _default_adapters()

    questions = derive_evidence_questions(hurdle.primitives)
    stubs: list[QueryStub] = []
    for lens in ALL_LENSES:
        for q in questions:
            stubs += lens(q)
    stubs = dedupe_merge_tags(stubs)

    items: list[EvidenceItemData] = []
    for stub in stubs:
        items += adapters[stub.source].fetch(stub)

    source_versions: dict = {"mode": mode}
    for source, adapter in adapters.items():
        source_versions[source.value] = adapter.version
    misses = sorted(
        {m for a in adapters.values() if isinstance(a, FixtureReplayAdapter) for m in a.misses}
    )
    if misses:
        source_versions["fixture_misses"] = misses

    snap = EvidenceSnapshot(
        hurdle_id=hurdle_id,
        snapshot_hash=snapshot_hash(items),
        question_count=len(questions),
        item_count=len(items),
        source_versions=source_versions,
    )
    snap.items = [
        EvidenceItem(
            source=i.source.value,
            query=i.query,
            lens_tags=sorted(t.value for t in i.lens_tags),
            content=i.content,
            citation=i.citation,
            fetched_at=i.fetched_at,
        )
        for i in items
    ]
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def run_evidence_pipeline(
    db: Session,
    hurdle_id: str,
    *,
    force_refresh: bool = False,
    adapters: dict[EvidenceSource, SourceAdapter] | None = None,
) -> EvidenceSnapshot:
    """Orchestration path: feasibility pre-check, then build. The check is a
    stub (always True) until the optimizer in Layer 3d provides real
    infeasibility detection; it is called here so the seam exists."""
    hurdle = db.get(Hurdle, hurdle_id)
    if hurdle is None:
        raise HurdleNotFound(f"Hurdle {hurdle_id} not found.")
    state = rebuild_state_from_ledger(db)
    quick_feasibility_check(state, hurdle.primitives)
    return build_evidence_snapshot(db, hurdle_id, force_refresh=force_refresh, adapters=adapters)
