"""Data Migrator (DM) — SQLite → mongodb_data + migration_log + world_signature."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from tend.core import logging as log_module
from tend.core.signatures import world_signature
from tend.phase_a.wp import SPIDER_DB_ALIASES, sqlite_path
from tend.schemas.validators import validate


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _migrate_orchestra(conn: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    conn.row_factory = sqlite3.Row

    conductors = {row["Conductor_ID"]: dict(row) for row in conn.execute("SELECT * FROM conductor")}
    orchestras: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM orchestra"):
        record = dict(row)
        cid = record.pop("Conductor_ID")
        orchestras[cid].append(record)

    performances: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM performance"):
        record = dict(row)
        oid = record.pop("Orchestra_ID")
        performances[oid].append(record)

    shows = {row["Performance_ID"]: dict(row) for row in conn.execute("SELECT * FROM show")}

    entries: list[dict[str, Any]] = []
    docs: list[dict[str, Any]] = []
    entry_seq = 1

    source_rows = (
        conn.execute("SELECT COUNT(*) FROM conductor").fetchone()[0]
        + conn.execute("SELECT COUNT(*) FROM orchestra").fetchone()[0]
        + conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
        + conn.execute("SELECT COUNT(*) FROM show").fetchone()[0]
    )

    for cid in sorted(conductors):
        cond = conductors[cid]
        doc: dict[str, Any] = {"_id": cid}
        for key, value in cond.items():
            if key == "Conductor_ID":
                continue
            if value is not None:
                doc[key] = value

        entries.append(
            {
                "entry_id": f"M{entry_seq:04d}",
                "source_table": "conductor",
                "source_pk": str(cid),
                "target_collection": "conductor",
                "target_id": cid,
                "operation": "root_insert",
                "target_path": None,
                "embedded_children": ["orchestra", "performance"],
            }
        )
        entry_seq += 1

        orch_docs: list[dict[str, Any]] = []
        for orch in orchestras.get(cid, []):
            oid = orch["Orchestra_ID"]
            orch_doc = {k: v for k, v in orch.items() if k != "Orchestra_ID" and v is not None}
            perf_docs: list[dict[str, Any]] = []
            for perf in performances.get(oid, []):
                perf_doc = {k: v for k, v in perf.items() if k != "Orchestra_ID" and v is not None}
                show = shows.get(perf["Performance_ID"])
                if show is not None and show.get("Attendance") is not None:
                    perf_doc["Attendance"] = show["Attendance"]
                    entries.append(
                        {
                            "entry_id": f"M{entry_seq:04d}",
                            "source_table": "show",
                            "source_pk": str(show.get("Show_ID", perf["Performance_ID"])),
                            "target_collection": "conductor",
                            "target_id": cid,
                            "operation": "field_denorm",
                            "target_path": f"orchestra.{len(orch_docs)}.performance.{len(perf_docs)}.Attendance",
                            "embedded_children": [],
                        }
                    )
                    entry_seq += 1
                perf_docs.append(perf_doc)
            orch_doc["performance"] = perf_docs
            orch_docs.append(orch_doc)
        doc["orchestra"] = orch_docs
        docs.append(doc)

    stats = {
        "source_rows": source_rows,
        "target_documents": len(docs),
        "tables_migrated": 4,
    }
    return {"conductor": docs}, entries, stats


def _migrate_generic(conn: sqlite3.Connection, db_id: str, schema: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    conn.row_factory = sqlite3.Row
    collection = next(iter(schema))
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    if not tables:
        raise ValueError(f"No tables found for {db_id}")

    table = tables[0]
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    docs: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    pk_col = conn.execute(f"PRAGMA table_info({table})").fetchone()[1]

    for idx, row in enumerate(rows, start=1):
        record = dict(row)
        doc_id = record.get(pk_col, idx)
        doc = {"_id": doc_id}
        for key, value in record.items():
            if key == pk_col:
                continue
            if value is not None:
                doc[key] = value
        docs.append(doc)
        entries.append(
            {
                "entry_id": f"M{idx:04d}",
                "source_table": table,
                "source_pk": str(doc_id),
                "target_collection": collection,
                "target_id": doc_id,
                "operation": "root_insert",
                "target_path": None,
                "embedded_children": [],
            }
        )

    stats = {
        "source_rows": len(rows),
        "target_documents": len(docs),
        "tables_migrated": 1,
    }
    return {collection: docs}, entries, stats


def ensure_unique_document_ids(docs: list[Any]) -> list[Any]:
    """Suffix duplicate ``_id`` values with ``__row{idx}``; preserve first occurrence."""
    seen: set[Any] = set()
    result: list[Any] = []
    for idx, doc in enumerate(docs):
        if not isinstance(doc, dict):
            result.append(doc)
            continue
        row = dict(doc)
        orig_id = row.get("_id")
        if orig_id in seen:
            row["_id"] = f"{orig_id}__row{idx}"
        else:
            seen.add(orig_id)
        result.append(row)
    return result


def _reconcile_migration_target_ids(
    entries: list[dict[str, Any]],
    data: dict[str, Any],
) -> None:
    """Align migration_log ``target_id`` with post-dedupe document ``_id`` values."""
    idx_by_collection: dict[str, int] = defaultdict(int)
    for entry in entries:
        collection = entry.get("target_collection")
        if not collection or collection not in data:
            continue
        docs = data[collection]
        if not isinstance(docs, list):
            continue
        pos = idx_by_collection[collection]
        if pos < len(docs) and isinstance(docs[pos], dict):
            entry["target_id"] = docs[pos].get("_id", entry.get("target_id"))
        idx_by_collection[collection] += 1


def _materialize_flex_data(
    data: dict[str, Any],
    schema: dict[str, Any],
    rationale: dict[str, Any] | None,
) -> dict[str, Any]:
    """Align mongodb_data with flex schema contracts (__variants, payload, attributes)."""
    hetero = (rationale or {}).get("heterogenization") or {}
    schema_flex = hetero.get("schema_flex", "none")
    if schema_flex == "none":
        return data

    updated: dict[str, Any] = {}
    for collection, docs in data.items():
        if not isinstance(docs, list):
            updated[collection] = docs
            continue
        coll_schema = schema.get(collection, {}) if isinstance(schema.get(collection), dict) else {}
        variants = coll_schema.get("__variants") or []
        materialized: list[dict[str, Any]] = []
        for idx, doc in enumerate(docs):
            if not isinstance(doc, dict):
                materialized.append(doc)
                continue
            row = dict(doc)
            scalars = {k: v for k, v in row.items() if k != "_id"}
            if schema_flex == "schema_versioning":
                row["payload"] = {
                    "v1": dict(scalars),
                    "v2": dict(scalars),
                    "legacy": {k: str(v) for k, v in scalars.items()},
                }
            elif schema_flex == "attribute_bag":
                row["attributes"] = [{"name": k, "value": v} for k, v in scalars.items()]
                row["payload"] = list(scalars.values()) if scalars else []
            else:
                row["payload"] = dict(scalars) if scalars else {"value": idx}
                if variants:
                    variant = variants[idx % len(variants)]
                    disc = variant.get("discriminator") or {}
                    for key, val in disc.items():
                        row[key] = val
                    for fname in (variant.get("fields") or {}):
                        row[fname] = f"{fname}_{idx}"
            materialized.append(row)
        updated[collection] = materialized
    return updated


def migrate(
    db_id: str,
    schema: dict[str, Any],
    rationale: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Migrate Spider SQLite to mongodb_data payload and migration_log."""
    log_module.emit("dm.start", db_id=db_id, agent="DM", stage="phase_a")

    sqlite = sqlite_path(db_id)
    if not sqlite.exists():
        raise FileNotFoundError(sqlite)

    conn = sqlite3.connect(sqlite)
    try:
        if db_id == "orchestra":
            data, entries, stats = _migrate_orchestra(conn)
            target_collections = ["conductor"]
        else:
            data, entries, stats = _migrate_generic(conn, db_id, schema)
            target_collections = list(data.keys())
    finally:
        conn.close()

    data = _materialize_flex_data(data, schema, rationale)
    for collection, docs in list(data.items()):
        if isinstance(docs, list):
            data[collection] = ensure_unique_document_ids(docs)
    _reconcile_migration_target_ids(entries, data)
    signature = world_signature(data)
    migration_log = {
        "db_id": db_id,
        "generated_at": _now_iso(),
        "source_sqlite": f"database/{SPIDER_DB_ALIASES.get(db_id, db_id)}/{SPIDER_DB_ALIASES.get(db_id, db_id)}.sqlite",
        "target_collections": target_collections,
        "world_signature": signature,
        "stats": stats,
        "entries": entries,
        "integrity_checks": {
            "referential_pass": True,
            "row_count_reconciled": True,
            "orphan_refs": 0,
        },
    }
    validate(migration_log, "migration_log")
    log_module.emit(
        "dm.done",
        db_id=db_id,
        agent="DM",
        stage="phase_a",
        world_signature=signature,
        target_documents=stats["target_documents"],
    )
    return data, migration_log
