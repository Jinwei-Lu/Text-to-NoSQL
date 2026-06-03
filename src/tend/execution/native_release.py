"""Native release helpers for exact-name MongoDB databases.

These helpers are intentionally separate from :mod:`tend.execution.mongo`: the normal
executor uses run-scoped database names, while native DataWorld release checks need MongoDB
databases whose names exactly match the BIRD mini-dev ``db_id`` values.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from bson import json_util

from ..errors import ExecutionError
from .ast_check import parse_pipeline

try:  # Import at module scope so tests can monkeypatch ``MongoClient`` directly.
    from pymongo import MongoClient
except Exception:  # pragma: no cover - only hit in broken optional environments
    MongoClient = None  # type: ignore[assignment]


_BATCH_SIZE = 1_000
_PROTECTED_DATABASES = frozenset({"admin", "config", "local"})


def load_dataset_exact_databases(
    dataset_dir: Path,
    mongo_uri: str,
    db_ids: Iterable[str] | None = None,
    drop_tend_prefixed: bool = False,
    logger: Any = None,
) -> dict[str, Any]:
    """Load ``mongodb_data/<db_id>.json`` into exact-name MongoDB databases.

    Each target database is dropped and rebuilt under the exact ``db_id`` name. System
    databases are refused, and stale ``tend_`` prefixed databases are only removed when
    ``drop_tend_prefixed`` is explicitly true.
    """
    dataset_dir = Path(dataset_dir)
    selected_db_ids = _resolve_db_ids(dataset_dir, db_ids)
    _refuse_protected_databases(selected_db_ids)

    client = _connect(mongo_uri)
    try:
        existing_databases = set(_list_database_names(client))
        dropped_tend = _drop_tend_prefixed_databases(client, existing_databases, drop_tend_prefixed)
        result: dict[str, Any] = {
            "dataset_dir": str(dataset_dir),
            "db_ids": selected_db_ids,
            "databases": {},
            "dropped_exact_databases": [],
            "dropped_tend_prefixed_databases": dropped_tend,
            "total_documents": 0,
        }

        for db_id in selected_db_ids:
            collections = _read_database_file(dataset_dir, db_id)
            if db_id in existing_databases:
                result["dropped_exact_databases"].append(db_id)
            client.drop_database(db_id)
            db = client[db_id]

            collection_counts: dict[str, int] = {}
            indexes: dict[str, list[str]] = {}
            document_count = 0
            for collection_name, docs in collections.items():
                collection_counts[collection_name] = len(docs)
                document_count += len(docs)
                if not docs:
                    continue
                collection = db[collection_name]
                _insert_many_batched(collection, docs)
                created = _create_top_level_id_indexes(collection, docs)
                if created:
                    indexes[collection_name] = created

            result["databases"][db_id] = {
                "path": str(_database_json_path(dataset_dir, db_id)),
                "collections": collection_counts,
                "documents": document_count,
                "indexes": indexes,
            }
            result["total_documents"] += document_count
            _log_info(
                logger,
                "native_exact_database_loaded",
                db_id=db_id,
                collections=collection_counts,
                indexes=indexes,
            )

        return result
    finally:
        _close_client(client)


def execute_gold_records_exact(
    dataset_dir: Path,
    mongo_uri: str,
    records: list[dict[str, Any]] | None = None,
    max_time_ms: int = 30_000,
) -> dict[str, Any]:
    """Execute native release gold MQL against exact-name MongoDB databases.

    The return value is a compact validation summary: it records counts and small per-record
    metadata, but never includes result documents.
    """
    dataset_dir = Path(dataset_dir)
    gold_records = _load_gold_records(dataset_dir) if records is None else records
    if not isinstance(gold_records, list):
        raise ValueError("records must be a list of dicts")

    summary: dict[str, Any] = {
        "total": len(gold_records),
        "executed": 0,
        "failures": 0,
        "empty": 0,
        "field_missing": 0,
        "records": [],
        "failure_records": [],
        "empty_records": [],
        "field_missing_records": [],
        "by_db": {},
    }

    client = _connect(mongo_uri)
    try:
        for index, record in enumerate(gold_records):
            if not isinstance(record, dict):
                _record_failure(
                    summary,
                    record={},
                    index=index,
                    stage="record",
                    error="record is not a dict",
                    missing_fields=["record"],
                )
                continue

            db_id = record.get("db_id")
            mql = record.get("MQL")
            missing_fields = [
                field
                for field, value in (("db_id", db_id), ("MQL", mql))
                if not isinstance(value, str) or not value.strip()
            ]
            if missing_fields:
                _record_failure(
                    summary,
                    record=record,
                    index=index,
                    stage="record",
                    error="record missing required fields",
                    missing_fields=missing_fields,
                )
                continue

            _db_bucket(summary, db_id)["total"] += 1
            try:
                collection, pipeline = parse_pipeline(mql)
            except Exception as exc:  # noqa: BLE001 - parser raises typed Tend errors
                _record_failure(summary, record=record, index=index, stage="parse", error=str(exc))
                continue

            try:
                cursor = client[db_id][collection].aggregate(pipeline, maxTimeMS=max_time_ms)
                result_count = sum(1 for _ in cursor)
            except Exception as exc:  # noqa: BLE001 - PyMongo exposes heterogeneous errors
                _record_failure(
                    summary,
                    record=record,
                    index=index,
                    stage="execute",
                    error=str(exc),
                    collection=collection,
                )
                continue

            summary["executed"] += 1
            _db_bucket(summary, db_id)["executed"] += 1
            record_summary = _record_ref(record, index)
            record_summary.update(
                {
                    "db_id": db_id,
                    "collection": collection,
                    "result_count": result_count,
                    "ok": True,
                }
            )
            summary["records"].append(record_summary)
            if result_count == 0:
                summary["empty"] += 1
                _db_bucket(summary, db_id)["empty"] += 1
                summary["empty_records"].append(record_summary.copy())
        return summary
    finally:
        _close_client(client)


def _resolve_db_ids(dataset_dir: Path, db_ids: Iterable[str] | None) -> list[str]:
    if db_ids is not None:
        return [str(db_id) for db_id in db_ids]
    data_dir = dataset_dir / "mongodb_data"
    if not data_dir.exists():
        raise FileNotFoundError(f"missing native mongodb_data directory: {data_dir}")
    return sorted(path.stem for path in data_dir.glob("*.json"))


def _refuse_protected_databases(db_ids: list[str]) -> None:
    protected = sorted(db_id for db_id in db_ids if db_id in _PROTECTED_DATABASES)
    if protected:
        joined = ", ".join(protected)
        raise ValueError(f"refusing to drop protected MongoDB database(s): {joined}")


def _connect(mongo_uri: str) -> Any:
    if MongoClient is None:
        raise ExecutionError("pymongo is not available", context={"uri_set": bool(mongo_uri)})
    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=4000)
        client.admin.command("ping")
        return client
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed execution anomaly
        raise ExecutionError("cannot connect to MongoDB", context={"uri_set": bool(mongo_uri)}) from exc


def _list_database_names(client: Any) -> list[str]:
    try:
        return list(client.list_database_names())
    except Exception:  # noqa: BLE001 - older/fake clients may not expose this reliably
        return []


def _drop_tend_prefixed_databases(
    client: Any,
    existing_databases: set[str],
    drop_tend_prefixed: bool,
) -> list[str]:
    if not drop_tend_prefixed:
        return []
    dropped: list[str] = []
    for db_name in sorted(existing_databases):
        if db_name in _PROTECTED_DATABASES or not db_name.startswith("tend_"):
            continue
        client.drop_database(db_name)
        dropped.append(db_name)
    return dropped


def _read_database_file(dataset_dir: Path, db_id: str) -> dict[str, list[dict[str, Any]]]:
    path = _database_json_path(dataset_dir, db_id)
    raw = json_util.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"MongoDB data file must be an object: {path}")

    collections: dict[str, list[dict[str, Any]]] = {}
    for collection_name, docs in raw.items():
        if not isinstance(collection_name, str):
            raise ValueError(f"collection name must be a string in {path}")
        if not isinstance(docs, list):
            raise ValueError(f"collection {collection_name!r} must contain a list of documents")
        collections[collection_name] = docs
    return collections


def _database_json_path(dataset_dir: Path, db_id: str) -> Path:
    return dataset_dir / "mongodb_data" / f"{db_id}.json"


def _insert_many_batched(collection: Any, docs: list[dict[str, Any]]) -> None:
    for start in range(0, len(docs), _BATCH_SIZE):
        batch = docs[start : start + _BATCH_SIZE]
        if batch:
            collection.insert_many(batch)


def _create_top_level_id_indexes(collection: Any, docs: list[dict[str, Any]]) -> list[str]:
    fields: set[str] = set()
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, value in doc.items():
            if key == "_id" or not key.endswith("_id") or isinstance(value, (dict, list)):
                continue
            fields.add(key)

    created: list[str] = []
    for field in sorted(fields):
        try:
            collection.create_index(field)
            created.append(field)
        except Exception:  # noqa: BLE001 - indexes are a performance optimization
            continue
    return created


def _load_gold_records(dataset_dir: Path) -> list[dict[str, Any]]:
    path = dataset_dir / "test.json"
    raw = json_util.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"gold records file must contain a list: {path}")
    return raw


def _record_failure(
    summary: dict[str, Any],
    *,
    record: dict[str, Any],
    index: int,
    stage: str,
    error: str,
    missing_fields: list[str] | None = None,
    collection: str | None = None,
) -> None:
    summary["failures"] += 1
    db_id = record.get("db_id")
    if isinstance(db_id, str) and db_id:
        _db_bucket(summary, db_id)["failures"] += 1
    failure = _record_ref(record, index)
    failure.update({"stage": stage, "error": error[:300]})
    if isinstance(db_id, str) and db_id:
        failure["db_id"] = db_id
    if collection:
        failure["collection"] = collection
    if missing_fields:
        summary["field_missing"] += 1
        failure["missing_fields"] = missing_fields
        summary["field_missing_records"].append(failure.copy())
    summary["failure_records"].append(failure)


def _record_ref(record: dict[str, Any], index: int) -> dict[str, Any]:
    ref: dict[str, Any] = {"index": index}
    if "record_id" in record:
        ref["record_id"] = record["record_id"]
    return ref


def _db_bucket(summary: dict[str, Any], db_id: str) -> dict[str, int]:
    by_db = summary["by_db"]
    if db_id not in by_db:
        by_db[db_id] = {"total": 0, "executed": 0, "failures": 0, "empty": 0}
    return by_db[db_id]


def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _log_info(logger: Any, event: str, **fields: Any) -> None:
    if logger is None:
        return
    info = getattr(logger, "info", None)
    if not callable(info):
        return
    try:
        info(event, **fields)
    except TypeError:
        info(f"{event}: {fields}")


__all__ = [
    "load_dataset_exact_databases",
    "execute_gold_records_exact",
]
