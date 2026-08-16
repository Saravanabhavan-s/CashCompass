"""Thin DB-touching wrapper around app/vault/egress.py's pure EgressPolicy —
pulls the current FinancialState and (optionally) a SensitiveProfile, then
projects it for a destination. This is the seam GET /egress/preview exposes,
and the seam Layer 3's debate agents would call in place of reading the DB
directly.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.enums import EgressDestination
from app.services.sensitive import get_sensitive_profile
from app.services.state import rebuild_state_from_ledger
from app.vault.egress import EgressPolicy
from app.vault.scanner import scan

_HEALTH_FAMILY_TOKEN_PREFIXES = ["MEDICAL", "RELATION", "NOTE"]


def preview_egress(
    db: Session,
    destination: EgressDestination,
    *,
    user_id: str | None = None,
    caller: str | None = None,
    purpose: str | None = None,
) -> dict:
    state = rebuild_state_from_ledger(db)
    payload = state.model_dump(mode="python")

    forbidden_values = [v for v in payload.values() if isinstance(v, (int, float)) or hasattr(v, "as_tuple")]

    if user_id is not None:
        profile = get_sensitive_profile(db, user_id)
        if profile is not None:
            payload["medical_conditions"] = profile.medical_conditions
            payload["dependents"] = profile.dependents
            payload["family_commitments"] = profile.family_commitments
            payload["obligations"] = profile.obligations

    projected = EgressPolicy.apply(payload, destination, db=db, caller=caller, purpose=purpose)

    if destination == EgressDestination.DEBATE_AGENT:
        leaks = scan(
            projected,
            forbidden_values=forbidden_values,
            forbid_token_prefixes=_HEALTH_FAMILY_TOKEN_PREFIXES,
        )
    else:
        leaks = []

    return {"destination": destination, "payload": projected, "leaks": [str(leak) for leak in leaks]}
