"""Derive published record six-axis metadata from MQL and Phase A context."""

from __future__ import annotations

import re
from typing import Any

_STAGE_OP_RE = re.compile(r'\{\s*"\$(\w+)"')


def count_aggregate_stages(mql: str) -> int:
    """Count pipeline stage operators in an aggregate() call."""
    if not mql:
        return 1
    stages = _STAGE_OP_RE.findall(mql)
    return max(len(stages), 1)


def join_depth_from_mql(mql: str) -> int:
    """Count $lookup + $graphLookup operators as join-depth proxy (02 axis buckets)."""
    if not mql:
        return 0
    return mql.count("$lookup") + mql.count("$graphLookup")


def aggregation_depth_from_mql(mql: str) -> str:
    """Map root stage count to shallow / medium / deep per 02 §02-4-1."""
    count = count_aggregate_stages(mql)
    if count <= 4:
        return "shallow"
    if count <= 9:
        return "medium"
    return "deep"


def derive_record_axes(
    mql: str,
    query_plan: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return join_depth, aggregation_depth, schema_pattern for a published record."""
    _ = query_plan
    ctx = context or {}
    return {
        "join_depth": join_depth_from_mql(mql),
        "aggregation_depth": aggregation_depth_from_mql(mql),
        "schema_pattern": ctx.get("schema_pattern", "embed"),
    }
