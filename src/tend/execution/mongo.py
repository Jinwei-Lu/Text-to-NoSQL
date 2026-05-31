"""MongoDB execution: NormExec (run an MQL aggregate) and ``equiv_rec`` (result equality).

A :class:`MongoExecutor` loads per-db witness data into an ephemeral working database
(``<prefix><db_id>``), runs MQL aggregate strings, and normalizes results for the
≡_rec comparison used by MS gold-lock, PV, RTV and the NNC bridges. Connection is lazy so
stub/offline runs that never execute don't require a reachable server.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import Settings
from ..errors import ExecutionError
from ..observability import RunLogger
from .ast_check import parse_pipeline
from .signature import _canon

_FLOAT_NDIGITS = 12


class MongoExecutor:
    """Lazy MongoDB client wrapper for witness loading + NormExec."""

    def __init__(self, settings: Settings, logger: RunLogger) -> None:
        self._s = settings
        self._log = logger
        self._client: Any = None

    # ------------------------------------------------------------------ #
    def _connect(self) -> Any:
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

    # ------------------------------------------------------------------ #
    def load_witness(self, db_id: str, collections: dict[str, list[dict[str, Any]]]) -> None:
        """Drop and reload the working db for ``db_id`` from ``{collection: [docs]}``.

        Docs may use MongoDB Extended JSON ($oid/$date/...); they are coerced via bson.
        """
        from bson import json_util

        client = self._connect()
        db = client[self._db_name(db_id)]
        client.drop_database(self._db_name(db_id))
        for coll, docs in collections.items():
            if not docs:
                continue
            coerced = json_util.loads(json.dumps(docs, default=str))
            db[coll].insert_many(coerced)
        self._log.info("witness_loaded", db_id=db_id,
                       collections={c: len(d) for c, d in collections.items()})

    def norm_exec(self, db_id: str, mql: str) -> list[dict[str, Any]]:
        """Execute an MQL aggregate and return the *normalized* result documents."""
        collection, pipeline = parse_pipeline(mql)
        client = self._connect()
        db = client[self._db_name(db_id)]
        try:
            raw = list(db[collection].aggregate(pipeline))
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
    records. Witness ``_id``s are deterministic PKs, not volatile ObjectIds.
    """
    if isinstance(doc, dict):
        return {k: _normalize_doc(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_normalize_doc(v) for v in doc]
    return _unify_number(doc)


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
