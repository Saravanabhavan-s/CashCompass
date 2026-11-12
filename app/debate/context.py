"""Debate input assembly. User data reaches a model ONLY through Layer 2's
DEBATE_AGENT egress projection; the finished case is then leak-scanned against
the exact source values. Any hit raises DebateInputLeak and nothing is sent."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.enums import EgressDestination, FieldKind
from app.evidence.personas import Persona
from app.exceptions import DebateInputLeak, HurdleNotFound
from app.models.evidence import EvidenceSnapshot
from app.models.hurdle import Hurdle
from app.services.egress import _HEALTH_FAMILY_TOKEN_PREFIXES, preview_egress
from app.services.state import rebuild_state_from_ledger
from app.vault.classification import classify
from app.vault.egress import apply_egress, band_months
from app.vault.scanner import scan


@dataclass(frozen=True)
class EvidenceRef:
    handle: str  # "E1".. in canonical snapshot order
    source: str
    query: str
    lens_tags: frozenset[str]
    content: str
    citation: str


@dataclass(frozen=True)
class DebateContext:
    case: dict
    snapshot_id: str
    snapshot_hash: str
    items: tuple[EvidenceRef, ...]
    handle_map: dict[str, EvidenceRef] = field(default_factory=dict, compare=False)

    def case_json(self) -> str:
        return json.dumps(self.case, sort_keys=True, ensure_ascii=False, default=str, indent=1)

    def items_for(self, persona: Persona) -> list[EvidenceRef]:
        return [i for i in self.items if persona.value in i.lens_tags]


def evidence_refs(snapshot: EvidenceSnapshot) -> tuple[EvidenceRef, ...]:
    """Handles E1..En follow the snapshot's canonical order
    (source, query, sha256(content), citation), so equal snapshot_hash gives
    identical handles regardless of DB row order."""
    ordered = sorted(
        snapshot.items,
        key=lambda i: (i.source, i.query, hashlib.sha256(i.content.encode("utf-8")).hexdigest(), i.citation),
    )
    return tuple(
        EvidenceRef(
            handle=f"E{n}",
            source=i.source,
            query=i.query,
            lens_tags=frozenset(i.lens_tags),
            content=i.content,
            citation=i.citation,
        )
        for n, i in enumerate(ordered, start=1)
    )


def _magnitudes(payload) -> list[Decimal]:
    """Exact non-zero values of MAGNITUDE-classified fields in the SOURCE data."""
    out: list[Decimal] = []

    def walk(node, parent_kind=None):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, classify(k).kind)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, parent_kind)
        elif parent_kind == FieldKind.MAGNITUDE and not isinstance(node, bool) and node is not None:
            try:
                d = Decimal(str(node))
            except (InvalidOperation, ValueError):
                return
            if d != 0:
                out.append(d)

    walk(payload)
    return out


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def build_debate_context(
    db: Session,
    hurdle_id: str,
    snapshot: EvidenceSnapshot,
    as_of: date | None = None,
    user_id: str | None = None,
) -> DebateContext:
    hurdle = db.get(Hurdle, hurdle_id)
    if hurdle is None:
        raise HurdleNotFound(f"Hurdle {hurdle_id} not found.")
    if snapshot.hurdle_id != hurdle_id:
        raise ValueError("snapshot does not belong to this hurdle")
    as_of = as_of or date.today()

    preview = preview_egress(db, EgressDestination.DEBATE_AGENT, user_id=user_id)
    if preview["leaks"]:
        raise DebateInputLeak(f"egress preview leaked: {preview['leaks'][0]}")

    primitives = [apply_egress(p, EgressDestination.DEBATE_AGENT) for p in hurdle.primitives]
    for src, proj in zip(hurdle.primitives, primitives):
        for key in ("due_date", "deadline", "date", "new_date"):
            raw = src.get(key)
            if raw:
                proj["months_from_now"] = band_months(max(_months_between(as_of, date.fromisoformat(raw)), 0))
                break

    case = {"as_of": as_of.isoformat(), "state": preview["payload"], "hurdle": primitives}

    state = rebuild_state_from_ledger(db)
    forbidden_values = _magnitudes(state.model_dump(mode="python")) + _magnitudes(hurdle.primitives)
    forbidden_substrings = [hurdle.label, hurdle.raw_description] + [
        p["reason"] for p in hurdle.primitives if isinstance(p.get("reason"), str)
    ]
    leaks = scan(
        case,
        forbidden_values=forbidden_values,
        forbidden_substrings=forbidden_substrings,
        forbid_token_prefixes=_HEALTH_FAMILY_TOKEN_PREFIXES,
    )
    if leaks:
        raise DebateInputLeak(f"debate case leaked ({len(leaks)}): {leaks[0]}")

    items = evidence_refs(snapshot)
    return DebateContext(
        case=case,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        items=items,
        handle_map={i.handle: i for i in items},
    )
