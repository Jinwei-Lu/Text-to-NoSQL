from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
import datetime as dt
import hashlib

from bson import ObjectId
from pymongo import MongoClient

from .io import load_json
from .models import Record
from .mql import canonical_text, parse_mql_query


class ExecutionBackend:
    def norm_exec(self, record: Record, query: str, witness: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class ReplayExecutionBackend(ExecutionBackend):
    def __init__(self, bundle_root: Path):
        self.bundle_root = bundle_root
        self._oracle_cache: dict[int, dict[str, Any]] = {}

    def _oracle_path(self, record: Record) -> Path:
        return (
            self.bundle_root
            / "audit"
            / record.db_id
            / str(record.record_id)
            / "derived"
            / "oracle.json"
        )

    def _load_oracle(self, record: Record) -> dict[str, Any]:
        cached = self._oracle_cache.get(record.record_id)
        if cached is not None:
            return cached
        oracle = load_json(self._oracle_path(record))
        self._oracle_cache[record.record_id] = oracle
        return oracle

    def norm_exec(self, record: Record, query: str, witness: dict[str, Any]) -> list[dict[str, Any]]:
        oracle = self._load_oracle(record)
        lookup = {
            canonical_text(entry["query"]): entry["result"]
            for entry in oracle.get("query_results", [])
        }
        key = canonical_text(query)
        if key not in lookup:
            raise KeyError(f"Replay oracle does not have a cached result for record {record.record_id}.")
        return lookup[key]


class LocalMongoExecutionBackend(ExecutionBackend):
    def __init__(
        self,
        mongo_uri: str = "mongodb://localhost:27017",
        database_prefix: str = "tend_eval",
        server_selection_timeout_ms: int = 3000,
    ):
        self.mongo_uri = mongo_uri
        self.database_prefix = database_prefix
        self.client = MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=server_selection_timeout_ms,
        )
        self.client.admin.command("ping")

    def norm_exec(self, record: Record, query: str, witness: dict[str, Any]) -> list[dict[str, Any]]:
        collection_name, operation, payload = parse_mql_query(query)
        database_name = self._database_name(record, query)
        db = self.client[database_name]
        try:
            self._load_witness(db, witness)
            if operation == "aggregate":
                cursor = db[collection_name].aggregate(payload)
                return [_normalize_bson(document) for document in cursor]
            if operation == "find":
                cursor = db[collection_name].find(payload["filter"], payload["projection"])
                return [_normalize_bson(document) for document in cursor]
            raise ValueError(f"Unsupported operation: {operation}")
        finally:
            self.client.drop_database(database_name)

    def _database_name(self, record: Record, query: str) -> str:
        digest = hashlib.sha1(canonical_text(query).encode("utf-8")).hexdigest()[:12]
        return f"{self.database_prefix}_{record.db_id}_{record.record_id}_{digest}"

    def _load_witness(self, db: Any, witness: dict[str, Any]) -> None:
        for collection_name, documents in witness.items():
            if not isinstance(documents, list):
                raise TypeError(f"Witness collection '{collection_name}' must be a list of documents.")
            if documents:
                db[collection_name].insert_many(deepcopy(documents))
            else:
                db.create_collection(collection_name)

    def close(self) -> None:
        self.client.close()


def build_execution_backend(
    bundle_root: Path,
    backend_name: str,
    mongo_uri: str = "mongodb://localhost:27017",
) -> ExecutionBackend:
    if backend_name == "replay":
        return ReplayExecutionBackend(bundle_root)
    if backend_name == "local-mongo":
        return LocalMongoExecutionBackend(mongo_uri=mongo_uri)
    raise ValueError(f"Unknown execution backend: {backend_name}")


def _normalize_bson(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_bson(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_bson(item) for item in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return value
