from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator
import datetime as dt
import hashlib

from bson import ObjectId
from pymongo import MongoClient

from tend.config import MONGO_URI
from tend.errors import BOT, BOT_EXEC

from .io import load_json
from .mql import canonical_text, parse_mql_query
from .models import CanonicalFormSet, Record


class MongoSession:
    def __init__(
        self,
        db_id: str,
        snapshot: dict[str, Any],
        *,
        mongo_uri: str = MONGO_URI,
        database_prefix: str = "tend_exec",
    ):
        self.db_id = db_id
        self.snapshot = snapshot
        self.mongo_uri = mongo_uri
        self.database_prefix = database_prefix
        self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        self._database_name: str | None = None

    def __enter__(self) -> "MongoSession":
        self.client.admin.command("ping")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._database_name:
            self.client.drop_database(self._database_name)
        self.client.close()

    def _ensure_db(self, query: str) -> Any:
        digest = hashlib.sha1(canonical_text(query).encode("utf-8")).hexdigest()[:12]
        self._database_name = f"{self.database_prefix}_{self.db_id}_{digest}"
        db = self.client[self._database_name]
        for collection_name, documents in self.snapshot.items():
            if not isinstance(documents, list):
                raise TypeError(f"Witness collection '{collection_name}' must be a list.")
            if documents:
                db[collection_name].insert_many(deepcopy(documents))
            else:
                db.create_collection(collection_name)
        return db

    def exec_query(self, query: str) -> list[dict[str, Any]] | BOT_EXEC:
        try:
            collection_name, operation, payload = parse_mql_query(query)
            db = self._ensure_db(query)
            if operation == "aggregate":
                cursor = db[collection_name].aggregate(payload)
                return [_normalize_bson(document) for document in cursor]
            if operation == "find":
                cursor = db[collection_name].find(payload["filter"], payload["projection"])
                return [_normalize_bson(document) for document in cursor]
            return BOT_EXEC(f"unsupported_operation:{operation}")
        except Exception as exc:  # noqa: BLE001
            return BOT_EXEC(str(exc))


@contextmanager
def with_mongo_session(db_id: str, snapshot: dict[str, Any], *, mongo_uri: str = MONGO_URI) -> Iterator[MongoSession]:
    with MongoSession(db_id, snapshot, mongo_uri=mongo_uri) as session:
        yield session


def Exec(ast: dict, snapshot_or_uri: Any) -> list | BOT_EXEC:
    if isinstance(ast, dict) and ast.get("ok") and "raw" in ast:
        query = ast["raw"]
    elif isinstance(ast, str):
        query = ast
    else:
        return BOT_EXEC("invalid_ast")
    if isinstance(snapshot_or_uri, MongoSession):
        result = snapshot_or_uri.exec_query(query)
        return result
    if isinstance(snapshot_or_uri, dict):
        with MongoSession("exec", snapshot_or_uri) as session:
            return session.exec_query(query)
    return BOT_EXEC("invalid_snapshot")


class ReplayExecutionBackend:
    def __init__(self, bundle_root: Path):
        self.bundle_root = bundle_root

    def norm_exec(self, record: Record, query: str, witness: dict[str, Any]) -> list[dict[str, Any]]:
        oracle_path = (
            self.bundle_root
            / "audit"
            / record.db_id
            / str(record.record_id)
            / "derived"
            / "oracle.json"
        )
        if oracle_path.exists():
            oracle = load_json(oracle_path)
            lookup = {canonical_text(entry["query"]): entry["result"] for entry in oracle.get("query_results", [])}
            key = canonical_text(query)
            if key in lookup:
                return lookup[key]
        result = NormExec(query, witness)
        if isinstance(result, (BOT, BOT_EXEC)):
            raise KeyError(str(result))
        return result


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
