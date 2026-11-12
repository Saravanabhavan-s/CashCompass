"""LOCKED persona archetypes and debate group assignment (Layer 3b imports
these; do not re-derive).

The skeptic is duplicated in both groups so neither group is structurally
groupthink-prone: G-DMAD's own failure case is a group with no adversarial
role converging confidently on a shared wrong answer.
"""
from enum import Enum


class Persona(str, Enum):
    SAVER = "saver"
    LIQUIDITY_HAWK = "liquidity_hawk"
    SKEPTIC = "skeptic"
    OPPORTUNITY_SEEKER = "opportunity_seeker"
    CONTRARIAN = "contrarian"


DEBATE_GROUPS: dict[str, tuple[Persona, Persona, Persona]] = {
    "group_1": (Persona.SKEPTIC, Persona.SAVER, Persona.LIQUIDITY_HAWK),
    "group_2": (Persona.SKEPTIC, Persona.OPPORTUNITY_SEEKER, Persona.CONTRARIAN),
}
