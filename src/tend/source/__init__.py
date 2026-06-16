"""BIRD mini-dev source adapter for native construction and evaluation.

BIRD is the *data + query-workload* source (not an MQL oracle): SQLite rows, column
semantics from ``database_description/*.csv`` (incl. ``value_description`` enums), and
the ``(question, evidence, SQL, difficulty)`` workload. Native construction uses the
source semantics and workload pressure, but generated TEND records do not use BIRD NL/SQL
as gold anchors.
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
