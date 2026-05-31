"""Build ``bird_db_catalog.json`` — the global BIRD 11-db inventory (02 §02-II-2).

test-only: there is no selection gate — all 11 BIRD dbs load and enter test. This module only
materializes the catalog (domain, table/query counts) and the query-bearing supply markers
(``flex_eligible`` / ``query_bearing``) that drive the H7/H9 supply-relax. Fail-fast if the
source does not present exactly the 11 expected dbs (test-only needs all of them).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import SourceError
from .bird import BIRD_DBS, BirdSource
from .census import Census, run_census


def build_catalog(source: BirdSource, *, census: Census | None = None) -> dict[str, Any]:
    """Assemble the catalog dict (does not write). Computes the census if not supplied."""
    census = census or run_census(source)
    db_ids = list(source.db_ids)
    if set(db_ids) != set(BIRD_DBS):
        raise SourceError(
            "BIRD source does not present the 11 expected dbs",
            context={"got": sorted(db_ids), "expected": sorted(BIRD_DBS)},
        )

    databases: list[dict[str, Any]] = []
    for db_id in sorted(db_ids):
        dbc = census.databases[db_id]
        qb_mechs = sorted({m["mechanism"] for m in dbc.mechanisms if m.get("query_bearing")})
        note = ("query-bearing: " + ", ".join(qb_mechs)) if qb_mechs else \
               "no query-bearing heterogeneity (baseline only)"
        databases.append({
            "db_id": db_id,
            "domain_id": dbc.domain,
            "table_count": dbc.table_count,
            "query_count": dbc.query_count,
            "flex_eligible": dbc.flex_eligible,
            "query_bearing": dbc.flex_eligible,   # >=1 query-bearing structural mechanism
            "selected": True,                      # test-only: all 11 always selected
            "load_note": f"loaded into test; {note}",
        })

    return {
        "source": "BIRD mini-dev (minidev/MINIDEV)",
        "db_count": len(databases),
        "flex_eligible_db_ratio": round(census.flex_eligible_db_ratio, 3),
        "supply_relax_active": census.supply_relax,
        "databases": databases,
    }


def write_catalog(catalog: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
