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
        try:
            return [
                d
                for d in self._db[collection].aggregate([{"$limit": int(n)}], allowDiskUse=True)
                if isinstance(d, dict)
            ]
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
