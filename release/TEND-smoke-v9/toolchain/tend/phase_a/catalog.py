"""Spider DB catalog selection per 02-II-2."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tend.config import REPO_ROOT, SPIDER_DATA_ROOT
from tend.core import logging as log_module
from tend.config import force_document_flex
from tend.phase_a.sra import eval_triggers
from tend.phase_a.wp import SPIDER_DB_ALIASES, _load_spider_queries, normalize_db_id, sqlite_path, workload_for_triggers

FIXTURE_DB_IDS = [
    "orchestra",
    "concert_singer",
    "cre_doc_tracking_db",
    "flight_2",
    "student_assessment",
    "world_1",
]

_catalog_cache: dict[str, Any] | None = None
_catalog_lock = threading.Lock()


def clear_catalog_cache() -> None:
    """Invalidate in-process catalog cache (tests / forced refresh)."""
    global _catalog_cache
    with _catalog_lock:
        _catalog_cache = None

DOMAIN_MAP_PATH = REPO_ROOT / "data" / "spider_domain_map.yaml"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_domain_map(path: Path | None = None) -> dict[str, str]:
    path = path or DOMAIN_MAP_PATH
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    domains = payload.get("domains", {})
    if isinstance(domains, dict):
        return {str(k): str(v) for k, v in domains.items()}
    return {}


def resolve_domain_id(db_id: str, domain_map: dict[str, str]) -> str:
    if db_id in domain_map:
        return domain_map[db_id]
    spider_id = SPIDER_DB_ALIASES.get(db_id, db_id)
    if spider_id in domain_map:
        return domain_map[spider_id]
    prefix = db_id.split("_")[0]
    return prefix if prefix else db_id


def _table_count(db_id: str) -> int:
    sqlite = sqlite_path(db_id)
    if not sqlite.exists():
        return 0
    conn = sqlite3.connect(sqlite)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        return int(rows)
    finally:
        conn.close()


def _non_empty_tables(db_id: str) -> bool:
    sqlite = sqlite_path(db_id)
    if not sqlite.exists():
        return False
    conn = sqlite3.connect(sqlite)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count == 0:
                return False
        return bool(tables)
    finally:
        conn.close()


def is_catalog_qualifying(
    db_id: str,
    *,
    min_tables: int = 2,
    min_queries: int = 10,
) -> bool:
    """True when db meets catalog min_tables/min_queries/non-empty table policy."""
    sqlite = sqlite_path(db_id)
    if not sqlite.exists():
        return False
    if _table_count(db_id) < min_tables:
        return False
    if len(_load_spider_queries(db_id)) < min_queries:
        return False
    return _non_empty_tables(db_id)


def _flex_eligible(db_id: str) -> bool:
    try:
        wp = workload_for_triggers(db_id)
    except (FileNotFoundError, ValueError):
        return False
    report = eval_triggers(
        wp,
        db_id=db_id,
        force_document_flex=force_document_flex(),
        qualifying=is_catalog_qualifying(db_id),
    )
    return bool(report["flex_eligible"])


def _discover_db_ids(spider_root: Path) -> list[str]:
    database_dir = spider_root / "database"
    if not database_dir.exists():
        return []
    db_ids: list[str] = []
    for entry in sorted(database_dir.iterdir()):
        if not entry.is_dir():
            continue
        sqlite = entry / f"{entry.name}.sqlite"
        if sqlite.exists():
            db_ids.append(entry.name)
    return db_ids


def select_spider_dbs(
    *,
    spider_root: Path | None = None,
    min_tables: int = 2,
    min_queries: int = 10,
    min_flex_db_ratio: float = 0.30,
    force_selected: list[str] | None = None,
    domain_map_path: Path | None = None,
    auto_select_qualifying: bool = False,
    max_selected: int | None = None,
) -> dict[str, Any]:
    """Build spider_db_catalog.json payload."""
    global _catalog_cache
    with _catalog_lock:
        if _catalog_cache is not None:
            return _catalog_cache
        result = _select_spider_dbs_uncached(
            spider_root=spider_root,
            min_tables=min_tables,
            min_queries=min_queries,
            min_flex_db_ratio=min_flex_db_ratio,
            force_selected=force_selected,
            domain_map_path=domain_map_path,
            auto_select_qualifying=auto_select_qualifying,
            max_selected=max_selected,
        )
        _catalog_cache = result
        return result


def _select_spider_dbs_uncached(
    *,
    spider_root: Path | None = None,
    min_tables: int = 2,
    min_queries: int = 10,
    min_flex_db_ratio: float = 0.30,
    force_selected: list[str] | None = None,
    domain_map_path: Path | None = None,
    auto_select_qualifying: bool = False,
    max_selected: int | None = None,
) -> dict[str, Any]:
    """Scan Spider and build catalog (uncached)."""
    spider_root = spider_root or SPIDER_DATA_ROOT
    domain_map = load_domain_map(domain_map_path)
    warnings: list[dict[str, str]] = []

    discovered = _discover_db_ids(spider_root)
    normalized_ids = sorted({normalize_db_id(db_id) for db_id in discovered})

    force_selected = force_selected or FIXTURE_DB_IDS
    force_set = set(force_selected)
    databases: list[dict[str, Any]] = []
    seen: set[str] = set()
    selected_count = 0

    for db_id in normalized_ids:
        if db_id in seen:
            raise ValueError(f"Duplicate db_id in catalog scan: {db_id}")
        seen.add(db_id)

        table_count = _table_count(db_id)
        query_count = len(_load_spider_queries(db_id))
        domain_id = resolve_domain_id(db_id, domain_map)
        if db_id not in domain_map and SPIDER_DB_ALIASES.get(db_id, db_id) not in domain_map:
            warnings.append({"db_id": db_id, "warning": f"domain fallback used: {domain_id}"})

        reject_reason = None
        selected = False
        selection_reason = None

        sqlite = sqlite_path(db_id)
        if not sqlite.exists():
            reject_reason = "sqlite_open_failed"
        elif table_count < min_tables:
            reject_reason = f"table_count<{min_tables}"
        elif query_count < min_queries:
            reject_reason = f"query_count<{min_queries}"
        elif not _non_empty_tables(db_id):
            reject_reason = "empty_table"
        elif db_id in force_set:
            selected = True
            selection_reason = "fixture_db; meets min_tables/min_queries"
            selected_count += 1
        elif auto_select_qualifying and reject_reason is None:
            if max_selected is None or selected_count < max_selected:
                selected = True
                selection_reason = "spider_pool; meets min_tables/min_queries"
                selected_count += 1

        flex_eligible = _flex_eligible(db_id) if sqlite.exists() else False

        databases.append(
            {
                "db_id": db_id,
                "domain_id": domain_id,
                "sqlite_path": f"database/{SPIDER_DB_ALIASES.get(db_id, db_id)}/{SPIDER_DB_ALIASES.get(db_id, db_id)}.sqlite",
                "table_count": table_count,
                "query_count": query_count,
                "selected": selected,
                "flex_eligible": flex_eligible,
                "selection_reason": selection_reason,
                "reject_reason": reject_reason,
            }
        )

    selected_entries = [entry for entry in databases if entry["selected"]]
    flex_ratio = (
        sum(1 for entry in selected_entries if entry["flex_eligible"]) / max(len(selected_entries), 1)
    )

    catalog = {
        "spider_version": "1.0",
        "generated_at": _now_iso(),
        "spider_root": str(spider_root),
        "selection_policy": {
            "min_tables": min_tables,
            "min_queries": min_queries,
            "require_non_empty_data": True,
            "min_flex_db_ratio": min_flex_db_ratio,
        },
        "databases": databases,
        "selected_flex_ratio": flex_ratio,
        "flex_supply_warning": flex_ratio < min_flex_db_ratio,
    }

    log_module.emit(
        "catalog.write",
        selected=len(selected_entries),
        total=len(databases),
        selected_flex_ratio=flex_ratio,
    )
    return {"catalog": catalog, "domain_map_warnings": warnings}
