import random
from datetime import datetime, timedelta, timezone

from app.evidence.adapters import EvidenceItemData
from app.evidence.canonical import canonicalize, snapshot_hash
from app.evidence.personas import Persona
from app.evidence.sources import EvidenceSource

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _items(n=12):
    return [
        EvidenceItemData(
            source=EvidenceSource.FRED if i % 2 else EvidenceSource.LOCAL_REGULATORY,
            query=f"q{i % 4}",
            lens_tags=frozenset({Persona.SKEPTIC, Persona.SAVER} if i % 3 else {Persona.CONTRARIAN}),
            content=f"content {i}",
            citation=f"cite {i}",
            fetched_at=T0,
        )
        for i in range(n)
    ]


def test_shuffled_order_same_hash():
    items = _items()
    base = snapshot_hash(items)
    rng = random.Random(7)
    for _ in range(10):
        shuffled = items[:]
        rng.shuffle(shuffled)
        assert snapshot_hash(shuffled) == base


def test_fetched_at_excluded_from_hash():
    items = _items()
    later = [i.model_copy(update={"fetched_at": T0 + timedelta(days=9)}) for i in items]
    assert snapshot_hash(items) == snapshot_hash(later)


def test_content_change_changes_hash():
    items = _items()
    items[0] = items[0].model_copy(update={"content": "different"})
    assert snapshot_hash(items) != snapshot_hash(_items())


def test_lens_tag_change_changes_hash():
    items = _items()
    items[0] = items[0].model_copy(update={"lens_tags": frozenset({Persona.SKEPTIC})})
    assert snapshot_hash(items) != snapshot_hash(_items())


def test_canonical_is_stable_bytes():
    assert canonicalize(_items()) == canonicalize(_items())
    assert canonicalize([]) == b"[]"
