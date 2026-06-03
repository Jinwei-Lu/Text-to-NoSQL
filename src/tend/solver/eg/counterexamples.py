"""Deterministic SMART-EG counterexample helpers."""
from __future__ import annotations

from typing import Any


def mine_counterexamples(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Return simple deterministic risks extracted from a candidate pipeline."""
    risks: list[dict[str, Any]] = []
    for index, stage in enumerate(candidate.get("pipeline") or []):
        if not isinstance(stage, dict):
            continue
        if "$unwind" in stage:
            risks.append({"stage_index": index, "risk": "unwind_missing_or_non_array"})
        if "$lookup" in stage:
            risks.append({"stage_index": index, "risk": "relationship_key_mismatch"})
    return risks


__all__ = ["mine_counterexamples"]
