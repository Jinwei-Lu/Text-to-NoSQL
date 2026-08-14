"""Data-access seam for the SAG solver.

Everything the solver knows about a database flows through a :class:`WorldAccess`
implementation: the structure lattice is *induced* from sampled documents, value
witnesses are located in the same sample, and probes/executions run against the
same handle. ``MongoWorld`` wraps the run's :class:`~tend.execution.mongo.MongoExecutor`
working database (respecting run-scoped naming / ``TEND_USE_EXISTING_MONGO_DBS``);
``LocalWorld`` wraps the in-memory witness dict so stub/offline runs can still
induce structure without a Mongo connection (``can_execute=False`` — gates that
need live probes fail open, execution-grounded repair is skipped).
"""
from __future__ import annotations

import os as _os

from typing import Any, Protocol, runtime_checkable

from ...errors import ExecutionError


@runtime_checkable
class WorldAccess(Protocol):
    """Read-only access to one database world."""

    db_id: str
    can_execute: bool

    def list_collections(self) -> list[str]: ...

    def sample_docs(self, collection: str, n: int) -> list[dict[str, Any]]: ...

    def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]], *, max_time_ms: int
    ) -> list[dict[str, Any]]: ...

    def find_one(
        self,
        collection: str,
        flt: dict[str, Any],
        projection: dict[str, Any] | None = None,
        *,
        max_time_ms: int,
    ) -> dict[str, Any] | None: ...


SPREAD_SAMPLE = _os.environ.get("TEND_SAG_SPREAD_SAMPLE", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

class MongoWorld:
    """Live world over the executor's working database.

    All calls are synchronous pymongo; async callers must dispatch through
    ``asyncio.to_thread``. pymongo faults surface as :class:`ExecutionError`.
    """

    can_execute = True

    def __init__(self, executor: Any, db_id: str) -> None:
        self.db_id = str(db_id)
        self._db = executor.raw_database(self.db_id)

    def list_collections(self) -> list[str]:
        try:
            return sorted(self._db.list_collection_names())
        except Exception as exc:  # noqa: BLE001 - pymongo faults -> typed anomaly
            raise ExecutionError(
                "cannot list collections", context={"db_id": self.db_id, "error": str(exc)[:200]}
            ) from exc

    def sample_docs(self, collection: str, n: int) -> list[dict[str, Any]]:
        """The sample the whole method is induced from.

        ``$limit n`` takes the FRONT of the collection, and the front is not the collection.
        `f1_actor_profiles` stores 840 drivers, then 208 constructors, then 72 circuits, in
        that order: its first 400 documents are 400 drivers and contain not one constructor or
        circuit. That is the entire cause of "the summary only ever describes one document
        shape" — the other shapes were never read.

        Under ``TEND_SAG_SPREAD_SAMPLE`` the same budget is spread over the collection with a
        stride instead. Measured: of the 70 reference answers that need a field the front
        sample misses, a spread sample shows it on 62. It KEEPS no more documents than the
        budget — but the client-side stride streams the whole collection over the wire to do
        it, with no time bound on this path, which is why the flag is off by default and the
        review recorded it as out of scope for the solver (the baseline's sample is equally
        front-biased, so a one-sided cure is a gift, not a method improvement).

        The stride must be ``ceil(count/n)`` and the result must not then be truncated. Striding
        by ``count // n`` and cutting to ``n`` walks only the first ``n * (count//n)`` documents,
        which for 1,120 documents and a budget of 400 is the first 800 — still entirely inside
        the drivers. That mistake makes the fix look useless.
        """
        try:
            if not SPREAD_SAMPLE:
                return [
                    d
                    for d in self._db[collection].aggregate([{"$limit": int(n)}], allowDiskUse=True)
                    if isinstance(d, dict)
                ]
            total = int(self._db[collection].estimated_document_count() or 0)
            if total <= int(n):
                step = 1
            else:
                step = -(-total // int(n))  # ceil
            out = [
                d
                for i, d in enumerate(self._db[collection].find({}))
                if i % step == 0 and isinstance(d, dict)
            ]
            return out
        except Exception as exc:  # noqa: BLE001
            raise ExecutionError(
                "cannot sample documents",
                context={"db_id": self.db_id, "collection": collection, "error": str(exc)[:200]},
            ) from exc

    def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]], *, max_time_ms: int
    ) -> list[dict[str, Any]]:
        try:
            return list(
                self._db[collection].aggregate(
                    pipeline, allowDiskUse=True, maxTimeMS=int(max_time_ms)
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise ExecutionError(
                "aggregate execution failed",
                context={"db_id": self.db_id, "collection": collection, "error": str(exc)[:300]},
            ) from exc

    def find_one(
        self,
        collection: str,
        flt: dict[str, Any],
        projection: dict[str, Any] | None = None,
        *,
        max_time_ms: int,
    ) -> dict[str, Any] | None:
        try:
            return self._db[collection].find_one(flt, projection, max_time_ms=int(max_time_ms))
        except Exception as exc:  # noqa: BLE001
            raise ExecutionError(
                "find_one probe failed",
                context={"db_id": self.db_id, "collection": collection, "error": str(exc)[:200]},
            ) from exc


class LocalWorld:
    """Offline world over the in-memory witness dict ``{collection: [docs]}``.

    Supports induction (listing + sampling) only; ``aggregate``/``find_one``
    raise so callers must consult ``can_execute`` before probing.
    """

    can_execute = False

    def __init__(self, db_id: str, data: dict[str, list[dict[str, Any]]]) -> None:
        self.db_id = str(db_id)
        self._data = {
            str(coll): [d for d in docs if isinstance(d, dict)]
            for coll, docs in (data or {}).items()
            if isinstance(docs, list)
        }
        if not self._data:
            raise ExecutionError(
                "offline world has no witness collections", context={"db_id": self.db_id}
            )

    def list_collections(self) -> list[str]:
        return sorted(self._data)

    def sample_docs(self, collection: str, n: int) -> list[dict[str, Any]]:
        return list(self._data.get(collection, ())[: int(n)])

    def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]], *, max_time_ms: int
    ) -> list[dict[str, Any]]:
        raise ExecutionError(
            "offline world cannot execute pipelines",
            context={"db_id": self.db_id, "collection": collection},
        )

    def find_one(
        self,
        collection: str,
        flt: dict[str, Any],
        projection: dict[str, Any] | None = None,
        *,
        max_time_ms: int,
    ) -> dict[str, Any] | None:
        raise ExecutionError(
            "offline world cannot probe documents",
            context={"db_id": self.db_id, "collection": collection},
        )
