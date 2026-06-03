from __future__ import annotations

from types import SimpleNamespace

from bson.objectid import ObjectId

from tend.execution.mongo import MongoExecutor, _normalize_doc


class CapturingLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def _settings(*, use_existing: bool = False):
    return SimpleNamespace(
        mongo_uri="mongodb://unused",
        mongo_db_prefix="tend_",
        run_id="run123",
        use_existing_mongo_dbs=use_existing,
    )


def test_mongo_executor_defaults_to_run_scoped_database_names() -> None:
    executor = MongoExecutor(_settings(), CapturingLog())

    assert executor._db_name("financial") == "tend_run123_financial"


def test_mongo_executor_can_reuse_existing_database_names_without_reloading_witness() -> None:
    log = CapturingLog()
    executor = MongoExecutor(_settings(use_existing=True), log)

    def fail_connect():
        raise AssertionError("load_witness should not connect when reusing existing dbs")

    executor._connect = fail_connect  # type: ignore[method-assign]

    executor.load_witness("financial", {"account": [{"_id": 1}]})

    assert executor._db_name("financial") == "financial"
    assert log.events == [
        (
            "witness_reuse_existing_db",
            {"db_id": "financial", "db_name": "financial", "collections": {"account": 1}},
        )
    ]


def test_normalize_doc_converts_bson_scalars_to_json_safe_extended_json() -> None:
    normalized = _normalize_doc(
        {"_id": ObjectId("656565656565656565656565"), "nested": [{"score": 1.0}]}
    )

    assert normalized == {
        "_id": {"$oid": "656565656565656565656565"},
        "nested": [{"score": 1}],
    }
