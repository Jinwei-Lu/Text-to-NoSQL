"""Six-axis slice aggregation for evaluation fingerprints."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from .fingerprint import mean_fingerprint
from .metrics import METRICS

SIX_AXES: dict[str, Callable[[dict[str, Any]], str]] = {
    "domain": lambda record: str(record.get("domain_id", record.get("db_id", "unknown"))),
    "join_depth": lambda record: bucket_join_depth(int(record.get("join_depth", 0))),
    "aggregation_depth": lambda record: str(record.get("aggregation_depth", "unknown")),
    "schema_pattern": lambda record: str(record.get("schema_pattern", "unknown")),
    "schema_flex": lambda record: str(record.get("schema_flex", "none")),
    "difficulty_tier": lambda record: str(record.get("difficulty", record.get("difficulty_tier", "unknown"))),
}

LEADERBOARD_SLICE_AXES = (
    "domain",
    "join_depth",
    "aggregation_depth",
    "schema_pattern",
    "difficulty_tier",
)


def bucket_join_depth(join_depth: int) -> str:
    if join_depth >= 3:
        return "3+"
    return str(join_depth)


def aggregate_slices(
    fingerprints: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    axes: dict[str, Callable[[dict[str, Any]], str]] | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """
    fingerprints: [{record_id, fp: (em,...,qim)}, ...]
    returns: {axis: {slice_value: {metric: mean_float}}}
    """
    axes = axes or SIX_AXES
    rec_by_id = {int(record["record_id"]): record for record in records}
    out: dict[str, dict[str, dict[str, float]]] = {}

    for axis, key_fn in axes.items():
        buckets: dict[str, list[tuple[int, ...]]] = defaultdict(list)
        for row in fingerprints:
            record = rec_by_id[int(row["record_id"])]
            buckets[key_fn(record)].append(tuple(row["fp"]))
        out[axis] = {slice_val: mean_fingerprint(fps) for slice_val, fps in sorted(buckets.items())}
    return out


def leaderboard_slice_aggregates(
    fingerprints: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, float]]]:
    subset = {axis: SIX_AXES[axis] for axis in LEADERBOARD_SLICE_AXES}
    return aggregate_slices(fingerprints, records, axes=subset)
