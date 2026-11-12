import itertools

from app.evidence.lenses import ALL_LENSES, QueryStub, dedupe_merge_tags
from app.evidence.personas import DEBATE_GROUPS, Persona
from app.evidence.questions import EvidenceQuestion, EvidenceTopic
from app.evidence.sources import EvidenceSource


def _keys(stubs):
    return {(s.source, s.query) for s in stubs}


def test_lenses_produce_divergent_query_sets_for_every_topic():
    """Persona-conditioned retrieval is real, not cosmetic: different QUERIES,
    not just different labels over the same ones."""
    assert len(ALL_LENSES) == 5
    for topic in EvidenceTopic:
        q = EvidenceQuestion(topic=topic, text="")
        sets = [_keys(lens(q)) for lens in ALL_LENSES]
        assert all(sets), f"a lens is empty for {topic}"
        for a, b in itertools.combinations(sets, 2):
            assert a != b, f"two lenses identical for {topic}"


def test_lens_tags_name_their_persona():
    q = EvidenceQuestion(topic=EvidenceTopic.EMERGENCY_FUND, text="")
    tags = [next(iter(lens(q)[0].lens_tags)) for lens in ALL_LENSES]
    assert set(tags) == set(Persona)


def test_dedup_merges_shared_query_and_unions_tags():
    q = EvidenceQuestion(topic=EvidenceTopic.EMERGENCY_FUND, text="")
    stubs = [s for lens in ALL_LENSES for s in lens(q)]
    merged = dedupe_merge_tags(stubs)
    assert len(merged) < len(stubs)
    shared = next(m for m in merged if m.query == "DICGC deposit insurance limit")
    assert {Persona.SKEPTIC, Persona.LIQUIDITY_HAWK} <= shared.lens_tags
    assert len({(m.source, m.query) for m in merged}) == len(merged)


def test_dedup_is_order_independent():
    q = EvidenceQuestion(topic=EvidenceTopic.SAVINGS_INSTRUMENTS, text="")
    stubs = [s for lens in ALL_LENSES for s in lens(q)]
    assert dedupe_merge_tags(stubs) == dedupe_merge_tags(list(reversed(stubs)))


def test_skeptic_asks_verification_angles():
    q = EvidenceQuestion(topic=EvidenceTopic.SAVINGS_INSTRUMENTS, text="")
    text = " ".join(s.query for s in ALL_LENSES[2](q)).lower()
    assert "regulation" in text and "recent changes" in text


def test_locked_group_assignment():
    g1, g2 = DEBATE_GROUPS["group_1"], DEBATE_GROUPS["group_2"]
    assert set(g1) == {Persona.SKEPTIC, Persona.SAVER, Persona.LIQUIDITY_HAWK}
    assert set(g2) == {Persona.SKEPTIC, Persona.OPPORTUNITY_SEEKER, Persona.CONTRARIAN}
    assert Persona.SKEPTIC in g1 and Persona.SKEPTIC in g2


def test_stub_is_frozen_value():
    s = QueryStub(query="x", source=EvidenceSource.FRED, lens_tags=frozenset({Persona.SAVER}))
    assert s == QueryStub(query="x", source=EvidenceSource.FRED, lens_tags=frozenset({Persona.SAVER}))
