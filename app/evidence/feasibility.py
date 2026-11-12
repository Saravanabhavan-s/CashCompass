"""Feasibility pre-check -- STUB ONLY.

Whether a hurdle's GOAL is numerically reachable needs the same arithmetic
the deterministic optimizer (Layer 3d) already has to do, so real
infeasibility detection lives there. This stub exists so the orchestration
seam is in place; it always lets the pipeline proceed. Real logic is
explicitly out of scope for Layer 3a.
"""
from __future__ import annotations

from typing import Any


def quick_feasibility_check(state: Any, primitives: list[Any]) -> bool:
    return True  # always proceed; real logic arrives in Layer 3d
