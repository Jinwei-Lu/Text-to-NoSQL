"""Release validation — the publish gate (Session A; see COORDINATION.md).

Session B's :mod:`tend.dataset` *writes* the Tier-1 release; this package *checks* it against the
02 contracts before it is trusted: per-record constraints C1-C9, JSON-Schema conformance, and the
test-composition hard constraints (L4 ≥ 30%, L0 ≤ 5%, schema_flex / structural_schema_flex shares
with supply-relax). Pure/deterministic; an optional MongoExecutor enables the executable C5 check.
"""
from __future__ import annotations

from .validate import (
    CompositionReport,
    ReleaseReport,
    validate_composition,
    validate_record,
    validate_record_jsonschema,
    validate_release,
)

__all__ = [
    "validate_record",
    "validate_record_jsonschema",
    "validate_composition",
    "validate_release",
    "CompositionReport",
    "ReleaseReport",
]
