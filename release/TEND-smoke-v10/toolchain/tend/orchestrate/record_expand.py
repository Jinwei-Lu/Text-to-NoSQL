"""Expand fixture-backed records to Pilot/Full scale while preserving schema validity."""

from __future__ import annotations

import copy
from typing import Any


def expand_records(
    records: list[dict[str, Any]],
    *,
    target_total: int,
    id_base: int = 10_000,
) -> list[dict[str, Any]]:
    """Clone records cyclically until ``target_total`` unique ``record_id`` values exist."""
    if not records:
        raise ValueError("expand_records requires at least one seed record")
    if target_total <= len(records):
        return sorted(records[:target_total], key=lambda r: int(r["record_id"]))

    expanded: list[dict[str, Any]] = []
    next_id = id_base
    used: set[int] = {int(r["record_id"]) for r in records}

    idx = 0
    while len(expanded) < target_total:
        seed = records[idx % len(records)]
        idx += 1
        while next_id in used:
            next_id += 1
        clone = copy.deepcopy(seed)
        clone["record_id"] = next_id
        used.add(next_id)
        expanded.append(clone)
        next_id += 1

    return sorted(expanded, key=lambda r: int(r["record_id"]))
