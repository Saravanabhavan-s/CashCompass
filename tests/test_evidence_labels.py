"""Source labels: complete, shown on every evidence line, and never part of the snapshot."""
import re
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from app.debate import prompts
from app.debate.context import build_debate_context
from app.debate.engine import run_debate
from app.debate.llm import ScriptedLLM
from app.evidence.adapters import EvidenceItemData
from app.evidence.canonical import snapshot_hash
from app.evidence.labels import LOCAL_LABEL, SERIES_LABELS, label_for, label_key
from app.evidence.lenses import _TABLE
from app.evidence.personas import Persona
from app.evidence.sources import EvidenceSource
from app.schemas.hurdle import HurdleCreate
from app.schemas.primitives import Goal, OneTimeOutflow, RiskConstraint
from app.services.evidence import build_evidence_snapshot
from app.services.hurdles import create_hurdle
from tests.debate_helpers import default_responder

# The hash of the fixture snapshot for the standard wedding hurdle, recorded BEFORE labels existed.
# Labels are presentation only, so it must never change because of them.
PINNED_WEDDING_SNAPSHOT_HASH = "9a06e8118e73fcaaf0505b6d24a89e371ccbd3fed73ade9a4e835f096fd5323e"


def _lens_queries():
    return [(s, q) for table in _TABLE.values() for lst in table.values() for s, q in lst]


# ---- the map ----------------------------------------------------------------
def test_every_series_in_lenses_has_a_label_and_there_are_no_orphans():
    used = set()
    for source, query in _lens_queries():
        if source == EvidenceSource.LOCAL_REGULATORY:
            continue
        key = label_key(source.value, query)
        assert key is not None, (source, query)
        assert key in SERIES_LABELS, f"no label for {key}"
        assert key.split(":", 1)[1] in label_for(source.value, query)  # the label names the series/symbol
        used.add(key)
    assert used == set(SERIES_LABELS)  # no stale labels either
    assert len(used) == 13  # 11 FRED series + SPY + INDA


def test_labels_do_not_pass_us_data_off_as_indian():
    for key, label in SERIES_LABELS.items():
        assert "NOT" in label, key  # every label says what the source is not
        assert "US" in label or "US-listed" in label, key
    assert SERIES_LABELS["FRED:DGS1"] == (
        "FRED DGS1 = US 1-year Treasury yield (a US government bond yield, NOT an Indian bank FD rate)"
    )
    assert "US dollars" in SERIES_LABELS["ALPHA_VANTAGE:INDA"]


def test_label_lookup_variants():
    assert label_for("LOCAL_REGULATORY", "DICGC deposit insurance limit") == LOCAL_LABEL
    assert label_key("ALPHA_VANTAGE", "function=TIME_SERIES_MONTHLY&symbol=spy") == "ALPHA_VANTAGE:SPY"
    assert label_key("FRED", "series:DGS1") == "FRED:DGS1" and label_key("FRED", "free text") is None
    unknown = label_for("FRED", "series:NOPE")
    assert "no label registered" in unknown and "do not assume" in unknown  # never invents a description


def test_system_prompt_has_the_rule():
    for persona in Persona:
        assert "Do not describe a source as something it is not" in prompts.system_prompt(persona)


# ---- prompts + snapshot (DB) ------------------------------------------------
@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)


@pytest.fixture
def setup(db_session):
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
    snap = build_evidence_snapshot(db_session, h.id)
    return h.id, snap, build_debate_context(db_session, h.id, snap)


_LINE = re.compile(r"^\[(E\d+)\] source=(\S+) \| (.+?) \| citation=", re.MULTILINE)


def test_every_evidence_line_in_every_prompt_carries_its_label(setup):
    _, _, ctx = setup
    llm = ScriptedLLM(default_responder)
    run_debate(ctx, llm)
    seen_fred_dgs1 = False
    for req in llm.calls:
        lines = _LINE.findall(req.prompt)
        assert lines, req.tag
        assert len(lines) == len(re.findall(r"^\[E\d+\] ", req.prompt, re.MULTILINE))  # no unlabelled evidence line
        for handle, source, label in lines:
            item = ctx.handle_map[handle]
            assert source == item.source
            assert label == label_for(item.source, item.query)
            assert "no label registered" not in label
            seen_fred_dgs1 |= label == SERIES_LABELS["FRED:DGS1"]
    assert seen_fred_dgs1  # the saver's DGS1 series reaches a prompt with its warning label


def test_labels_never_enter_the_snapshot_or_its_hash(db_session, setup):
    hid, snap, ctx = setup
    db_session.expire_all()
    items = list(snap.items)
    # 1. nothing label-like was stored or hashed (see also test_ctx_items_are_the_raw_snapshot_text)
    assert not any("NOT an Indian" in i.content or "no label registered" in i.content for i in items)
    # 2. the stored hash still equals the hash recomputed from the stored items
    data = [
        EvidenceItemData(
            source=EvidenceSource(i.source), query=i.query, lens_tags=frozenset(Persona(t) for t in i.lens_tags),
            content=i.content, citation=i.citation, fetched_at=i.fetched_at,
        )
        for i in items
    ]
    assert snapshot_hash(data) == snap.snapshot_hash
    # 3. and it equals the value recorded before labels existed
    assert snap.snapshot_hash == PINNED_WEDDING_SNAPSHOT_HASH
    # 4. rendering prompts does not change it, and a rebuild reproduces it
    llm = ScriptedLLM(default_responder)
    run_debate(ctx, llm)
    again = build_evidence_snapshot(db_session, hid, force_refresh=True)
    assert again.snapshot_hash == PINNED_WEDDING_SNAPSHOT_HASH


def test_ctx_items_are_the_raw_snapshot_text(setup):
    _, snap, ctx = setup
    raw = {(i.source, i.query, i.citation, i.content) for i in snap.items}
    assert {(r.source, r.query, r.citation, r.content) for r in ctx.items} == raw
