"""Query-bearing heterogeneity supply census + the Coverage Controller (deterministic).

Two jobs, both zero-LLM:

* :func:`run_census` scans all 11 BIRD dbs via :class:`BirdSource` + the mechanism detectors and
  reports, per db and per mechanism, how much *query-bearing* heterogeneity the source can yield —
  which bounds the achievable L4 / structural_schema_flex share (02 §02-4 supply-relax) and
  replaces a-priori composition targets with census-derived ones. Mirrors the standalone
  ``proposals/scripts/census_supply.py``, packaged on the shared loader.

* :func:`plan_coverage_slots` is the Coverage Controller: from the census it produces a
  deterministic, seeded list of ``(db_id, mechanism, archetype)`` coverage requests sized to a
  record target, biased to satisfy the test-composition constraints (L4 ≥ 30%, L0 ≤ 5%) up to the
  available supply. Session B's scheduler can map these onto its ``CoverageSlot`` (assigning
  ``record_id``); see COORDINATION.md.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from ..errors import SourceError
from ..mechanisms import ARCHETYPES, MechanismInstance, archetypes_for, detect_mechanisms
from .bird import BirdSource

L4_TARGET_RATIO = 0.30          # H5 ideal (02 §02-4-3)
L0_MAX_RATIO = 0.05             # H8
MIN_FLEX_DB_RATIO = 0.30        # below this -> supply-relax (02 §02-4-3 / 03 §03-5-3)


@dataclass
class DbCensus:
    db_id: str
    domain: str
    table_count: int
    query_count: int
    mechanisms: list[dict[str, Any]]
    query_bearing_counts: dict[str, int]
    l4_supply_cells: int             # (query-bearing mech instance × L4 archetype) cells
    ssf_supply_cells: int            # structural subset of the above
    flex_eligible: bool              # has >=1 query-bearing structural mechanism

    def reachable_archetypes(self) -> list[tuple[str, str]]:
        """(mechanism, archetype_id) pairs reachable from this db's query-bearing mechanisms."""
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str]] = []
        for m in self.mechanisms:
            if not m.get("query_bearing"):
                continue
            for arch in archetypes_for(m["mechanism"]):
                pair = (m["mechanism"], arch.id)
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
        return out


@dataclass
class Census:
    databases: dict[str, DbCensus]
    flex_eligible_db_ratio: float
    supply_relax: bool
    l4_supply_cells: int
    ssf_supply_cells: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "global": {
                "dbs": len(self.databases),
                "flex_eligible_dbs": sum(1 for d in self.databases.values() if d.flex_eligible),
                "flex_eligible_db_ratio": round(self.flex_eligible_db_ratio, 3),
                "supply_relax": self.supply_relax,
                "l4_supply_cells": self.l4_supply_cells,
                "structural_schema_flex_supply_cells": self.ssf_supply_cells,
            },
            "databases": {k: asdict(v) for k, v in self.databases.items()},
        }


def run_census(source: BirdSource, *, db_ids: list[str] | None = None) -> Census:
    """Scan the BIRD dbs and produce the supply census."""
    db_ids = db_ids or list(source.db_ids)
    root = source.root
    dbs: dict[str, DbCensus] = {}
    for db_id in db_ids:
        try:
            dbs[db_id] = _census_one_db(root, db_id)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(
                f"census failed for db '{db_id}'", context={"db_id": db_id}
            ) from exc
    n = len(dbs) or 1
    flex_ratio = sum(1 for d in dbs.values() if d.flex_eligible) / n
    return Census(
        databases=dbs, flex_eligible_db_ratio=flex_ratio,
        supply_relax=flex_ratio < MIN_FLEX_DB_RATIO,
        l4_supply_cells=sum(d.l4_supply_cells for d in dbs.values()),
        ssf_supply_cells=sum(d.ssf_supply_cells for d in dbs.values()),
    )


def _census_one_db(root: Any, db_id: str) -> DbCensus:
    worker_source = BirdSource(root)
    try:
        schema = worker_source.schema(db_id)
        mechs = detect_mechanisms(worker_source, db_id)
        qb = [m for m in mechs if m.query_bearing]
        l4_cells = ssf_cells = 0
        for m in qb:
            for arch in archetypes_for(m.mechanism):
                if arch.difficulty == "L4":
                    l4_cells += 1
                    if m.structural:
                        ssf_cells += 1
        counts: dict[str, int] = {}
        for m in qb:
            counts[m.mechanism] = counts.get(m.mechanism, 0) + 1
        return DbCensus(
            db_id=db_id,
            domain=schema.domain,
            table_count=schema.table_count,
            query_count=len(worker_source.workload(db_id)),
            mechanisms=[m.to_dict() for m in mechs],
            query_bearing_counts=counts,
            l4_supply_cells=l4_cells,
            ssf_supply_cells=ssf_cells,
            flex_eligible=any(m.structural for m in qb),
        )
    finally:
        worker_source.close()


# --------------------------------------------------------------------------- #
# Coverage Controller
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CoverageRequest:
    """One (db, mechanism, archetype) cell the controller wants a record for."""

    db_id: str
    mechanism: str
    archetype: str
    target_difficulty: str
    sql_infeasibility_class: str
    shape_policy: str


def _cell_key(seed: int, db_id: str, mech: str, arch: str, i: int) -> str:
    return hashlib.sha256(f"{seed}|{db_id}|{mech}|{arch}|{i}".encode()).hexdigest()


_MECHANISM_PRIORITY = {
    "sparse_embed": 0,
    "dynamic_key": 1,
    "versioning": 2,
    "polymorphic": 3,
    "sparse_scalar": 4,
    "none": 5,
}

_ARCHETYPE_PRIORITY = {
    "present_missing_projection": 0,
    "has_vs_absent_compare": 1,
    "per_subtype_agg": 2,
    "subtype_cond_projection": 3,
    "cross_keyset_value": 4,
    "dynamic_key_fold": 5,
}


def _archetype_meta(arch_id: str):
    from ..mechanisms import get_archetype

    a = get_archetype(arch_id)
    return a.difficulty, a.sql_infeasibility_class, a.shape_policy


def plan_coverage_slots(
    census: Census, *, n_records: int, seed: int = 0
) -> list[CoverageRequest]:
    """Deterministically schedule ``n_records`` coverage requests from census supply.

    Biases toward L4 (≥30% target, capped by structural supply) and caps L0 (≤5%). Cells are
    cycled deterministically (seeded) so the same census + seed yields the same plan.
    """
    # build cell pools by tier from each db's reachable archetypes
    l4_cells: list[tuple[str, str, str]] = []
    mid_cells: list[tuple[str, str, str]] = []     # L2/L3
    low_cells: list[tuple[str, str, str]] = []      # L1
    l0_cells: list[tuple[str, str, str]] = []
    for db_id, dbc in census.databases.items():
        # query-bearing mechanism cells
        for mech, arch in dbc.reachable_archetypes():
            diff = _archetype_meta(arch)[0]
            bucket = {"L4": l4_cells, "L3": mid_cells, "L2": mid_cells,
                      "L1": low_cells, "L0": l0_cells}[diff]
            bucket.append((db_id, mech, arch))
        # baseline 'none' cells are available on every db (domain/workload baseline)
        for arch in ARCHETYPES["none"]:
            diff = arch.difficulty
            bucket = {"L2": mid_cells, "L1": low_cells, "L0": l0_cells}[diff]
            bucket.append((db_id, "none", arch.id))

    def _ordered(cells: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        return sorted(cells, key=lambda c: (
            _MECHANISM_PRIORITY.get(c[1], 99),
            _ARCHETYPE_PRIORITY.get(c[2], 99),
            _mechanism_quality_rank(census.databases[c[0]], c[1]),
            _cell_key(seed, c[0], c[1], c[2], 0),
        ))

    l4_cells, mid_cells = _ordered(l4_cells), _ordered(mid_cells)
    low_cells, l0_cells = _ordered(low_cells), _ordered(l0_cells)

    # tier budgets (capped by supply availability)
    n_l4 = min(math.ceil(L4_TARGET_RATIO * n_records), _supply_cap(l4_cells, n_records))
    n_l0 = min(math.floor(L0_MAX_RATIO * n_records), len(l0_cells)) if l0_cells else 0
    n_rest = max(0, n_records - n_l4 - n_l0)
    n_mid = n_rest if mid_cells else 0
    n_low = max(0, n_rest - n_mid)
    if not mid_cells:
        n_low = n_rest

    plan: list[CoverageRequest] = []
    plan += _cycle(l4_cells, n_l4)
    plan += _cycle(mid_cells, n_mid)
    plan += _cycle(low_cells or mid_cells, n_low)
    plan += _cycle(l0_cells, n_l0)
    # if supply was short anywhere, top up from whatever non-L0 cells exist
    fallback = l4_cells + mid_cells + low_cells
    while len(plan) < n_records and fallback:
        plan += _cycle(fallback, n_records - len(plan))
    plan = plan[:n_records]

    return [
        CoverageRequest(db_id=c[0], mechanism=c[1], archetype=c[2],
                        target_difficulty=_archetype_meta(c[2])[0],
                        sql_infeasibility_class=_archetype_meta(c[2])[1],
                        shape_policy=_archetype_meta(c[2])[2])
        for c in plan
    ]


def _supply_cap(cells: list[tuple[str, str, str]], n_records: int) -> int:
    """Cap a tier's record count by its cell supply (a cell can back a few records)."""
    return min(n_records, max(0, len(cells) * 4))  # allow ~4 records per cell before saturating


def _cycle(cells: list[tuple[str, str, str]], k: int) -> list[tuple[str, str, str]]:
    if not cells or k <= 0:
        return []
    return [cells[i % len(cells)] for i in range(k)]


def _mechanism_quality_rank(dbc: DbCensus, mechanism: str) -> float:
    """Lower is better. Penalize sparse embeds whose present branch is nearly absent."""
    if mechanism != "sparse_embed":
        return 0.0
    scores: list[float] = []
    for mech in dbc.mechanisms:
        if mech.get("mechanism") != mechanism or not mech.get("query_bearing"):
            continue
        detail = mech.get("detail") if isinstance(mech.get("detail"), dict) else {}
        coverage = detail.get("coverage")
        if not isinstance(coverage, (int, float)):
            continue
        # A good present/missing benchmark has both branches visible. Extremely sparse
        # candidates remain supply, but should not lead a small smoke run.
        penalty = 0.0
        if coverage < 0.10 or coverage > 0.80:
            penalty = 1.0
        scores.append(abs(float(coverage) - 0.25) + penalty)
    return min(scores) if scores else 0.0


def write_census(census: Census, path: Any) -> None:
    import json
    from pathlib import Path

    Path(path).write_text(json.dumps(census.to_dict(), ensure_ascii=False, indent=2),
                          encoding="utf-8")
