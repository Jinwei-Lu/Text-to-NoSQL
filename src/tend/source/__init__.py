"""BIRD mini-dev source adapter — data + workload source for Phase A.

BIRD is the *data + query-workload* source (not an MQL oracle): SQLite rows, column
semantics from ``database_description/*.csv`` (incl. ``value_description`` enums), and
the ``(question, evidence, SQL, difficulty)`` workload. WP consumes this; Phase B never
uses BIRD NL/SQL as a gold anchor.
"""
from __future__ import annotations

from .bird import (
    BirdSource,
    ColumnSchema,
    DbSchema,
    ForeignKey,
    WorkloadQuery,
)

__all__ = [
    "BirdSource",
    "ColumnSchema",
    "DbSchema",
    "ForeignKey",
    "WorkloadQuery",
]
