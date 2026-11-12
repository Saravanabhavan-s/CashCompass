"""Canonicalization + hashing of the evidence item set.

Two runs with the same inputs but different fetch ordering MUST hash
identically, so items are sorted by a stable key (source, query, content
hash) -- never by fetch order. `fetched_at` is deliberately EXCLUDED: a
wall-clock field in the hash would make reproducibility impossible by
construction.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from app.evidence.adapters import EvidenceItemData


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def canonicalize(items: Iterable[EvidenceItemData]) -> bytes:
    records = [
        {
            "source": i.source.value,
            "query": i.query,
            "lens_tags": sorted(t.value for t in i.lens_tags),
            "content": i.content,
            "citation": i.citation,
        }
        for i in items
    ]
    records.sort(key=lambda r: (r["source"], r["query"], _content_hash(r["content"]), r["citation"]))
    return json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def snapshot_hash(items: Iterable[EvidenceItemData]) -> str:
    return hashlib.sha256(canonicalize(items)).hexdigest()
