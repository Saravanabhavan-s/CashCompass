"""DB-backed snapshot tests. All run offline against fixtures (EVIDENCE_MODE is
forced to 'fixture' in conftest); sockets/transports are blocked to prove it."""
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.exceptions import AppendOnlyViolation, HurdleNotFound
from app.models.evidence import EvidenceItem, EvidenceSnapshot
from app.schemas.hurdle import HurdleCreate
from app.schemas.primitives import Goal, OneTimeOutflow, RiskConstraint
from app.services.evidence import build_evidence_snapshot, get_snapshot, run_evidence_pipeline
from app.services.hurdles import create_hurdle


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    # TestClient uses its own in-process transport; real network goes via HTTPTransport.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)


@pytest.fixture
def hurdle_id(db_session):
    d = date.today() + timedelta(days=240)
    h = create_hurdle(
        db_session,
        HurdleCreate(
            label="Wedding",
            raw_description="Wedding in ~8 months",
            primitives=[
                OneTimeOutflow(amount=Decimal("500000"), due_date=d),
                Goal(target_amount=Decimal("500000"), deadline=d, priority=1),
                RiskConstraint(min_liquidity=Decimal("100000"), reason="wedding buffer"),
            ],
        ),
    )
    return h.id


def test_same_hurdle_same_fixtures_same_hash(db_session, hurdle_id):
    """The layer's primary deliverable: proof the debate-ON/OFF arms can see
    byte-identical evidence, with no network."""
    snap1 = build_evidence_snapshot(db_session, hurdle_id, force_refresh=True)
    snap2 = build_evidence_snapshot(db_session, hurdle_id, force_refresh=True)
    assert snap1.snapshot_hash == snap2.snapshot_hash
    assert snap1.id != snap2.id
    assert snap1.item_count > 0 and snap1.item_count == len(snap1.items)


def test_idempotent_without_force_refresh(db_session, hurdle_id):
    a = build_evidence_snapshot(db_session, hurdle_id)
    b = build_evidence_snapshot(db_session, hurdle_id)
    assert a.id == b.id
    assert db_session.query(EvidenceSnapshot).count() == 1


def test_force_refresh_creates_new_and_keeps_old(db_session, hurdle_id):
    old = build_evidence_snapshot(db_session, hurdle_id)
    old_id, old_hash, old_items = old.id, old.snapshot_hash, old.item_count
    new = build_evidence_snapshot(db_session, hurdle_id, force_refresh=True)
    assert new.id != old_id
    again = get_snapshot(db_session, old_id)
    assert again is not None and again.snapshot_hash == old_hash and len(again.items) == old_items
    assert db_session.query(EvidenceSnapshot).count() == 2


def test_lens_tags_and_provenance_persisted(db_session, hurdle_id):
    snap = build_evidence_snapshot(db_session, hurdle_id)
    assert snap.question_count == 2
    assert snap.source_versions["mode"] == "fixture"
    assert "fixture_misses" not in snap.source_versions
    shared = [i for i in snap.items if i.query == "DICGC deposit insurance limit"]
    assert shared and {"skeptic", "liquidity_hawk"} <= set(shared[0].lens_tags)


def test_unrecognized_primitives_give_empty_snapshot(db_session):
    from app.models.hurdle import Hurdle

    h = Hurdle(label="x", raw_description="x", primitives=[{"type": "MYSTERY"}])
    db_session.add(h)
    db_session.commit()
    snap = build_evidence_snapshot(db_session, h.id)
    assert snap.question_count == 0 and snap.item_count == 0


def test_missing_hurdle_raises(db_session):
    with pytest.raises(HurdleNotFound):
        run_evidence_pipeline(db_session, "00000000-0000-0000-0000-000000000000")


def test_pipeline_runs_feasibility_seam(db_session, hurdle_id, monkeypatch):
    calls = []
    import app.services.evidence as svc

    monkeypatch.setattr(svc, "quick_feasibility_check", lambda s, p: calls.append(1) or True)
    run_evidence_pipeline(db_session, hurdle_id)
    assert calls == [1]


@pytest.mark.parametrize("model", [EvidenceSnapshot, EvidenceItem])
def test_orm_update_and_delete_raise(db_session, hurdle_id, model):
    build_evidence_snapshot(db_session, hurdle_id)
    row = db_session.query(model).first()
    if model is EvidenceSnapshot:
        row.snapshot_hash = "tampered"
    else:
        row.content = "tampered"
    with pytest.raises(AppendOnlyViolation, match="append-only"):
        db_session.flush()
    db_session.rollback()

    row = db_session.query(model).first()
    db_session.delete(row)
    with pytest.raises(AppendOnlyViolation, match="append-only"):
        db_session.flush()
    db_session.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE evidence_snapshot SET snapshot_hash = 'x'",
        "DELETE FROM evidence_snapshot",
        "UPDATE evidence_item SET content = 'x'",
        "DELETE FROM evidence_item",
    ],
)
def test_db_trigger_blocks_raw_sql(db_session, db_engine, hurdle_id, sql):
    build_evidence_snapshot(db_session, hurdle_id)
    with pytest.raises(Exception, match="append-only"):
        with db_engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()


def test_api_routes(client, db_engine):
    d = (date.today() + timedelta(days=240)).isoformat()
    body = {
        "label": "Wedding",
        "raw_description": "w",
        "primitives": [
            {"type": "ONE_TIME_OUTFLOW", "amount": "500000", "due_date": d},
            {"type": "GOAL", "target_amount": "500000", "deadline": d, "priority": 1},
        ],
    }
    hid = client.post("/hurdles", json=body).json()["id"]

    assert client.get(f"/hurdles/{hid}/evidence-snapshot").status_code == 404
    r1 = client.post(f"/hurdles/{hid}/evidence-snapshot")
    assert r1.status_code == 201
    r2 = client.post(f"/hurdles/{hid}/evidence-snapshot")
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"]
    r3 = client.post(f"/hurdles/{hid}/evidence-snapshot?force_refresh=true")
    assert r3.status_code == 201
    assert r3.json()["id"] != r1.json()["id"]
    assert r3.json()["snapshot_hash"] == r1.json()["snapshot_hash"]

    assert client.get(f"/hurdles/{hid}/evidence-snapshot").json()["id"] == r3.json()["id"]
    old = client.get(f"/evidence-snapshot/{r1.json()['id']}")
    assert old.status_code == 200 and old.json()["items"]
    assert client.get("/evidence-snapshot/nope").status_code == 404
    assert client.post("/hurdles/nope/evidence-snapshot").status_code == 404
