"""Leak scanner — production code, not a test helper. Used by the egress
test suite AND by GET /egress/preview as a runtime tripwire: an empty
`leaks` list rendered live is the most persuasive proof this layer works.

`forbidden_values`/`forbidden_substrings` are passed in explicitly by the
caller from the SOURCE payload — the scanner never guesses what is secret.
Guessing would produce false positives (e.g. `priority: 1` colliding with an
unrelated amount of 1) that would make the egress tests unreliable.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

TOKEN_PREFIX = "TKN_"


@dataclass(frozen=True)
class Leak:
    path: str
    kind: str
    detail: str


def _amount_renderings(value) -> set[str]:
    try:
        d = Decimal(str(value))
    except Exception:
        return set()
    renderings = {str(d)}
    if d == d.to_integral_value():
        as_int = int(d)
        renderings.add(str(as_int))
        renderings.add(f"{as_int:,}")  # western grouping: 800,000
        renderings.add(_indian_grouping(as_int))  # indian grouping: 8,00,000
    renderings.add(f"{d:.2f}")
    return renderings


def _indian_grouping(n: int) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    s = str(n)
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts + [tail])


def _walk(payload, path: str = "$"):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for i, value in enumerate(payload):
            yield from _walk(value, f"{path}[{i}]")
    else:
        yield path, payload


def scan(
    payload,
    *,
    forbidden_values: Iterable = (),
    forbidden_substrings: Iterable[str] = (),
    forbid_token_prefixes: Iterable[str] = (),
) -> list[Leak]:
    forbidden_renderings: set[str] = set()
    for v in forbidden_values:
        forbidden_renderings |= _amount_renderings(v)

    forbidden_substrings_cf = [s.casefold() for s in forbidden_substrings if s]
    forbidden_prefixes = [f"{TOKEN_PREFIX}{p}_" for p in forbid_token_prefixes]

    leaks: list[Leak] = []
    for path, leaf in _walk(payload):
        if not isinstance(leaf, str):
            # Decimal/int/float leaves are still checked against forbidden_renderings.
            leaf_str = str(leaf) if isinstance(leaf, (int, float, Decimal)) else None
            if leaf_str is not None and leaf_str in forbidden_renderings:
                leaks.append(Leak(path, "exact_magnitude", f"leaf {leaf!r} matches a forbidden amount"))
            continue

        if leaf in forbidden_renderings:
            leaks.append(Leak(path, "exact_magnitude", f"leaf {leaf!r} matches a forbidden amount"))

        leaf_cf = leaf.casefold()
        for substr in forbidden_substrings_cf:
            if substr in leaf_cf:
                leaks.append(Leak(path, "literal", f"leaf contains forbidden substring {substr!r}"))

        for prefix in forbidden_prefixes:
            if leaf.startswith(prefix):
                leaks.append(Leak(path, "token_prefix", f"leaf {leaf!r} has forbidden token prefix {prefix!r}"))

    return leaks


def assert_clean(payload, **kwargs) -> None:
    from app.exceptions import EgressPolicyViolation

    leaks = scan(payload, **kwargs)
    if leaks:
        raise EgressPolicyViolation(f"egress scanner found {len(leaks)} leak(s): {leaks[0]}")
