"""Five-mechanism heterogeneity detection from real BIRD signal (03 §03-II-10).

Deterministic, zero-LLM, no synthesis fallback: each mechanism is recovered only from a real
signal in the BIRD source, discriminators are real column names, and a mechanism counts toward
supply only if **query-bearing** (referenced by the real workload SQL). A db that yields nothing
returns ``[]`` (``schema_flex = none``). This is the same logic the standalone supply census
uses, lifted onto :class:`tend.source.BirdSource`.

Mechanisms:
  polymorphic   low-cardinality discriminator column (2..8 distinct) carrying a
                value_description enum, conditioned by >=1 workload SQL    -> structural / L4
  sparse_scalar nullable column, NULL rate in (0.05, 0.95)                -> semantic
  sparse_embed  FK child satellite covering < EMBED_COVER_MAX of parents  -> structural / L4
                (relational INNER JOIN silently drops the absent parents)
  dynamic_key   EAV-shaped (attribute-name + attribute-value) column pair -> structural / L4
  versioning    a time/season column + a rename-pair of columns           -> semantic
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..source import BirdSource, DbSchema
from .archetypes import STRUCTURAL_MECHANISMS

# deterministic thresholds (match proposals/scripts/census_supply.py)
POLY_MIN_DISTINCT, POLY_MAX_DISTINCT = 2, 8
SPARSE_NULL_LO, SPARSE_NULL_HI = 0.05, 0.95
EMBED_COVER_MAX = 0.90

_EAV_NAME_SUFFIXES = ("attribute_name", "attr_name", "property_name", "key", "name")
_EAV_VAL_SUFFIXES = ("attribute_value", "attr_value", "property_value", "value")
_TIME_TOKENS = ("date", "time", "season", "year")
_RENAME_SUFFIX_RE = re.compile(r"(_?(old|new|prev|curr|current|v\d+|\d+))$")

#: best-effort schema_flex mode per mechanism (NNC has final say); see COORDINATION.md
_SCHEMA_FLEX_BY_MECH = {
    "polymorphic": "polymorphic",
    "dynamic_key": "dynamic_key",
    "versioning": "schema_versioning",
}


@dataclass(frozen=True)
class MechanismInstance:
    """One query-bearing (or not) heterogeneity instance recovered from real signal."""

    mechanism: str
    table: str
    detail: dict[str, Any] = field(default_factory=dict)
    query_bearing: bool = False
    source_signal: str = ""

    @property
    def structural(self) -> bool:
        """Maps to structural_schema_flex / L4 when query-bearing."""
        return self.mechanism in STRUCTURAL_MECHANISMS

    @property
    def schema_flex(self) -> str:
        return _SCHEMA_FLEX_BY_MECH.get(self.mechanism, "none")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism, "table": self.table, "detail": self.detail,
            "query_bearing": self.query_bearing, "structural": self.structural,
            "schema_flex": self.schema_flex, "source_signal": self.source_signal,
        }


def _cond_count(sqls: list[str], col: str) -> int:
    """# workload SQLs using ``col`` in a conditional (WHERE/CASE/HAVING comparison)."""
    pat = re.compile(rf"\b{re.escape(col.lower())}\b\s*(=|in\b|like\b|<>|!=|<|>)")
    return sum(1 for s in sqls if pat.search(s.lower()))


def _ref_count(sqls: list[str], token: str) -> int:
    pat = re.compile(rf"\b{re.escape(token.lower())}\b")
    return sum(1 for s in sqls if pat.search(s.lower()))


def _distinct_values(conn: sqlite3.Connection, table: str, col: str, cap: int = 16) -> list[Any]:
    q = lambda n: '"' + n.replace('"', '""') + '"'  # noqa: E731
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {q(col)} FROM {q(table)} WHERE {q(col)} IS NOT NULL LIMIT {cap}"
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def detect_mechanisms(source: BirdSource, db_id: str) -> list[MechanismInstance]:
    """Recover all query-bearing-or-not mechanism instances for one db (deterministic)."""
    schema: DbSchema = source.schema(db_id)
    conn = source.connection(db_id)
    sqls = [w.sql for w in source.workload(db_id) if w.sql]
    out: list[MechanismInstance] = []

    # per-column scans: polymorphic + sparse_scalar
    for col in schema.columns:
        try:
            ndist = source.distinct_count(db_id, col.table, col.name)
            null_rate = source.null_rate(db_id, col.table, col.name)
        except Exception:  # noqa: BLE001 - skip unreadable columns
            continue

        if POLY_MIN_DISTINCT <= ndist <= POLY_MAX_DISTINCT and col.has_enum:
            cc = _cond_count(sqls, col.name)
            if cc >= 1:
                vals = _distinct_values(conn, col.table, col.name, cap=POLY_MAX_DISTINCT)
                out.append(MechanismInstance(
                    mechanism="polymorphic", table=col.table,
                    detail={"discriminator_col": col.name, "distinct": ndist,
                            "subtype_values": vals, "sql_cond_refs": cc},
                    query_bearing=True,
                    source_signal=f"polymorphic: {col.table}.{col.name} "
                                  f"({ndist} values, value_description enum, {cc} SQL refs)",
                ))

        if SPARSE_NULL_LO < null_rate < SPARSE_NULL_HI:
            rc = _ref_count(sqls, col.name)
            out.append(MechanismInstance(
                mechanism="sparse_scalar", table=col.table,
                detail={"col": col.name, "null_rate": round(null_rate, 3), "sql_refs": rc},
                query_bearing=rc >= 1,
                source_signal=f"sparse_scalar: {col.table}.{col.name} "
                              f"(null_rate {null_rate:.2f})",
            ))

    # sparse_embed via FK coverage (child satellite of parent, partial coverage)
    for fk in schema.foreign_keys:
        n_parent = source.row_count(db_id, fk.parent_table)
        n_child = source.row_count(db_id, fk.child_table)
        if n_parent == 0 or n_child > n_parent:
            continue  # large fact table referencing a dimension is not optional-embed
        coverage = source.fk_coverage(db_id, fk)
        if coverage < EMBED_COVER_MAX:
            rc = _ref_count(sqls, fk.child_table)
            out.append(MechanismInstance(
                mechanism="sparse_embed", table=fk.child_table,
                detail={"fk_col": fk.child_col, "parent_table": fk.parent_table,
                        "coverage": round(coverage, 3), "sql_refs_child": rc},
                query_bearing=rc >= 1,
                source_signal=f"sparse_embed: {fk.child_table}->{fk.parent_table} "
                              f"(coverage {coverage:.2f})",
            ))

    # per-table scans: dynamic_key (EAV) + versioning
    cols_by_table: dict[str, list[str]] = {}
    for col in schema.columns:
        cols_by_table.setdefault(col.table, []).append(col.name.lower())
    for table, clist in cols_by_table.items():
        has_name = any(c.endswith(s) for c in clist for s in _EAV_NAME_SUFFIXES)
        has_val = any(c.endswith(s) for c in clist for s in _EAV_VAL_SUFFIXES)
        if has_name and has_val:
            out.append(MechanismInstance(
                mechanism="dynamic_key", table=table,
                detail={"note": "EAV-shaped name+value column pair"},
                query_bearing=_ref_count(sqls, table) >= 1,
                source_signal=f"dynamic_key: {table} (EAV name+value pair)",
            ))
        has_time = any(tok in c for c in clist for tok in _TIME_TOKENS)
        stems: dict[str, list[str]] = {}
        for c in clist:
            stem = _RENAME_SUFFIX_RE.sub("", c)
            stems.setdefault(stem, []).append(c)
        rename_pair = any(len(v) >= 2 for k, v in stems.items() if k)
        if has_time and rename_pair:
            out.append(MechanismInstance(
                mechanism="versioning", table=table,
                detail={"note": "time column + rename-pair"},
                query_bearing=True,
                source_signal=f"versioning: {table} (time column + rename-pair)",
            ))
    return out
