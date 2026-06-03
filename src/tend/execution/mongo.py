"""MongoDB execution: NormExec (run an MQL aggregate) and ``equiv_rec`` (result equality).

A :class:`MongoExecutor` loads per-db witness data into an ephemeral working database
(``<prefix><run_id>_<db_id>`` by default, or an existing ``<db_id>`` database when
configured), runs MQL aggregate strings, and normalizes results for the
≡_rec comparison used by MS gold-lock, PV, RTV and the NNC bridges. Connection is lazy so
stub/offline runs that never execute don't require a reachable server.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from ..config import Settings
from ..errors import ExecutionError
from ..observability import RunLogger
from .ast_check import parse_pipeline
from .signature import _canon

_FLOAT_NDIGITS = 12
# Server-side time bound for every aggregate. Indexed gold/round-trip queries finish in
# well under a second; LLM-generated PV mutations can be pathological (e.g. an unindexed
# $lookup that COLLSCANs financial.trans's ~1M rows per parent), which without a cap hangs
# the whole run. A timed-out aggregate raises ExecutionTimeout -> ExecutionError, which PV
# correctly treats as a discriminating (result-changing) mutation.
_EXEC_MAX_TIME_MS = 30_000


class MongoExecutor:
    """Lazy MongoDB client wrapper for witness loading + NormExec."""

    def __init__(self, settings: Settings, logger: RunLogger) -> None:
        self._s = settings
        self._log = logger
        self._client: Any = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _connect(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        from pymongo import MongoClient

                        self._client = MongoClient(self._s.mongo_uri, serverSelectionTimeoutMS=4000)
                        self._client.admin.command("ping")
                    except Exception as exc:  # noqa: BLE001 - surfaced as a typed execution anomaly
                        raise ExecutionError("cannot connect to MongoDB",
                                             context={"uri_set": bool(self._s.mongo_uri)}) from exc
        return self._client

    def available(self) -> bool:
        try:
            self._connect()
            return True
        except ExecutionError:
            return False

    def _db_name(self, db_id: str) -> str:
        if self._s.use_existing_mongo_dbs:
            return db_id
        # run-scoped so concurrent runs / tests never collide on the working database
        return f"{self._s.mongo_db_prefix}{self._s.run_id}_{db_id}"

    def count(self, db_id: str, collection: str) -> int:
        client = self._connect()
        return int(client[self._db_name(db_id)][collection].estimated_document_count())

    def sample_fields(self, db_id: str, collection: str, n: int = 200) -> set[str]:
        """Union of top-level field names across the first ``n`` docs (for new-field diffing)."""
        client = self._connect()
        fields: set[str] = set()
        for doc in client[self._db_name(db_id)][collection].find({}, limit=n):
            fields.update(doc.keys())
        return fields

    def snapshot_database(self, db_id: str, sample_size: int) -> dict[str, list[dict[str, Any]]]:
        """Return a bounded JSON-safe sample from every collection in the working DB."""
        from bson import json_util

        client = self._connect()
        db = client[self._db_name(db_id)]
        out: dict[str, list[dict[str, Any]]] = {}
        for collection in sorted(db.list_collection_names()):
            docs = list(db[collection].find({}, limit=max(1, sample_size)))
            out[collection] = [
                json.loads(json_util.dumps(doc, default=str))
                for doc in docs
                if isinstance(doc, dict)
            ]
        return out

    # ------------------------------------------------------------------ #
    def load_witness(self, db_id: str, collections: dict[str, list[dict[str, Any]]]) -> None:
        """Drop and reload the working db for ``db_id`` from ``{collection: [docs]}``.

        Docs may use MongoDB Extended JSON ($oid/$date/...); they are coerced via bson.
        """
        if self._s.use_existing_mongo_dbs:
            self._log.info(
                "witness_reuse_existing_db",
                db_id=db_id,
                db_name=self._db_name(db_id),
                collections={c: len(d) for c, d in collections.items()},
            )
            return
        from bson import json_util

        client = self._connect()
        db = client[self._db_name(db_id)]
        client.drop_database(self._db_name(db_id))
        indexes: dict[str, list[str]] = {}
        for coll, docs in collections.items():
            if not docs:
                continue
            coerced = json_util.loads(json.dumps(docs, default=str))
            db[coll].insert_many(coerced)
            created = self._index_join_fields(db[coll], coerced)
            if created:
                indexes[coll] = created
        self._log.info("witness_loaded", db_id=db_id,
                       collections={c: len(d) for c, d in collections.items()},
                       indexes=indexes)

    @staticmethod
    def _index_join_fields(collection: Any, docs: list[Any], *, sample: int = 200) -> list[str]:
        """Index top-level ``*_id`` join keys so ``$lookup`` foreignField scans avoid O(n·m).

        The gold-lock for join/ratio archetypes (e.g. present_missing_projection) joins a
        parent onto a large fact collection on its foreign key; on financial.trans (~1M
        rows) an unindexed foreignField turns each record's lock into a full scan. Indexes
        are a pure performance optimization — never correctness-affecting and never fatal.
        """
        candidates: set[str] = set()
        for doc in docs[:sample]:
            if not isinstance(doc, dict):
                continue
            for key, value in doc.items():
                if key != "_id" and key.endswith("_id") and not isinstance(value, (dict, list)):
                    candidates.add(key)
        created: list[str] = []
        for field in sorted(candidates):
            try:
                collection.create_index(field)
                created.append(field)
            except Exception:  # noqa: BLE001 - index build is best-effort, never fatal
                pass
        return created

    def norm_exec(self, db_id: str, mql: str) -> list[dict[str, Any]]:
        """Execute an MQL aggregate and return the *normalized* result documents."""
        collection, pipeline = parse_pipeline(mql)
        client = self._connect()
        db = client[self._db_name(db_id)]
        try:
            raw = list(db[collection].aggregate(pipeline, maxTimeMS=_EXEC_MAX_TIME_MS))
        except Exception as exc:  # noqa: BLE001 - pymongo/operator errors -> typed anomaly
            raise ExecutionError("aggregate execution failed",
                                 context={"db_id": db_id, "collection": collection,
                                          "error": str(exc)[:300]}) from exc
        return [_normalize_doc(d) for d in raw]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _normalize_doc(doc: Any) -> Any:
    """Norm layers (01 §01-4): recurse, unify numerics, keep null-vs-missing intact.

    ``_id`` is **kept** — it carries meaning as a semantic ``$group`` key (per-subtype agg,
    polymorphic dispatch) and as the preserved root key; dropping it would mis-compare those
    records. BSON scalars from existing MongoDB databases are converted into deterministic
    Extended JSON values so evaluation hashing/reporting remains JSON-safe.
    """
    if isinstance(doc, dict):
        return {str(k): _normalize_doc(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_normalize_doc(v) for v in doc]
    unified = _unify_number(doc)
    if unified is None or isinstance(unified, (str, int, bool)):
        return unified
    return _canon(unified)


def _unify_number(v: Any) -> Any:
    """Collapse integral floats to int (gold ``1`` == predicted ``1.0``); round others."""
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        if v.is_integer():
            return int(v)
        return round(v, _FLOAT_NDIGITS)
    return v


def _doc_key(doc: Any) -> str:
    return json.dumps(_canon(doc), ensure_ascii=False, sort_keys=True, default=str)


def equiv_rec(left: list[dict[str, Any]], right: list[dict[str, Any]], *,
              order_sensitive: bool) -> bool:
    """≡_rec: record-set equivalence. Order-insensitive compares as a multiset."""
    if left is None or right is None:
        return False
    if len(left) != len(right):
        return False
    if order_sensitive:
        return all(_doc_key(a) == _doc_key(b) for a, b in zip(left, right))
    return sorted(_doc_key(d) for d in left) == sorted(_doc_key(d) for d in right)
