from __future__ import annotations

import pytest
from types import SimpleNamespace
from typing import Any

from bson.objectid import ObjectId

from tend.errors import DisabledOperatorError
from tend.execution.mongo import MongoExecutor, _normalize_doc, equiv_rec
import tend.execution.mongo as _mongo_mod
import tend.execution.signature as _sig_mod


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


# ─── F6: shared _FLOAT_NDIGITS constant ───────────────────────────────────────


def test_float_ndigits_is_identical_in_mongo_and_signature_modules() -> None:
    # mongo.py imports _FLOAT_NDIGITS from signature.py; both must resolve to the same value
    # so rounding in _normalize_doc and canonical_json is always consistent.
    assert _mongo_mod._FLOAT_NDIGITS == _sig_mod._FLOAT_NDIGITS


# ─── F7: equiv_rec None-guard ─────────────────────────────────────────────────


def test_equiv_rec_returns_false_for_none_left() -> None:
    assert equiv_rec(None, [], order_sensitive=False) is False  # type: ignore[arg-type]


def test_equiv_rec_returns_false_for_none_right() -> None:
    assert equiv_rec([], None, order_sensitive=False) is False  # type: ignore[arg-type]


def test_equiv_rec_returns_false_for_both_none() -> None:
    assert equiv_rec(None, None, order_sensitive=False) is False  # type: ignore[arg-type]


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.find_calls: list[dict[str, Any]] = []
        self.aggregate_calls: list[dict[str, Any]] = []

    def estimated_document_count(self) -> int:
        return len(self.docs)

    def find(self, query: dict[str, Any], *, limit: int = 0):
        self.find_calls.append({"query": query, "limit": limit})
        return list(self.docs[:limit]) if limit else list(self.docs)

    def aggregate(self, pipeline: list[dict[str, Any]], *, maxTimeMS: int):
        self.aggregate_calls.append({"pipeline": pipeline, "maxTimeMS": maxTimeMS})
        limit = None
        for stage in pipeline:
            if "$limit" in stage:
                limit = int(stage["$limit"])
        return list(self.docs[:limit]) if limit is not None else list(self.docs)


class _FakeDb:
    def __init__(self, collections: dict[str, _FakeCollection]) -> None:
        self.collections = collections

    def list_collection_names(self) -> list[str]:
        return list(self.collections)

    def __getitem__(self, collection: str) -> _FakeCollection:
        return self.collections[collection]


class _FakeClient:
    def __init__(self, dbs: dict[str, _FakeDb]) -> None:
        self.dbs = dbs

    def __getitem__(self, db_name: str) -> _FakeDb:
        return self.dbs[db_name]


def _executor_with_fake_client(collections: dict[str, list[dict[str, Any]]]) -> MongoExecutor:
    executor = MongoExecutor(_settings(), CapturingLog())
    db = _FakeDb({name: _FakeCollection(docs) for name, docs in collections.items()})
    client = _FakeClient({"tend_run123_financial": db})
    executor._connect = lambda: client  # type: ignore[method-assign]
    return executor


def test_mongo_executor_lists_collections_and_samples_with_forced_bound() -> None:
    executor = _executor_with_fake_client(
        {
            "account": [
                {"_id": ObjectId("656565656565656565656565"), "email": "alice@example.com"},
                {"_id": ObjectId("656565656565656565656566"), "email": "bob@example.com"},
            ],
            "trans": [{"_id": 1}],
        }
    )

    collections = executor.list_collections("financial")
    sample = executor.sample_documents("financial", "account", limit=1)
    fake_account = executor._connect()["tend_run123_financial"]["account"]

    assert collections == [
        {"collection": "account", "estimated_document_count": 2},
        {"collection": "trans", "estimated_document_count": 1},
    ]
    assert sample["sample_count"] == 1
    assert "documents" not in sample
    assert sample["result_shape"]["paths"]["email"]["type_counts"] == {"str": 1}
    assert "alice@example.com" not in str(sample)
    assert fake_account.find_calls == [{"query": {}, "limit": 1}]


def test_mongo_executor_readonly_probe_rejects_banned_tokens_before_execution() -> None:
    executor = _executor_with_fake_client({"account": [{"_id": 1}]})
    fake_account = executor._connect()["tend_run123_financial"]["account"]

    with pytest.raises(DisabledOperatorError):
        executor.run_readonly_probe(
            "financial",
            'db.account.aggregate([{"$sample": {"size": 1}}])',
        )

    assert fake_account.aggregate_calls == []


def test_mongo_executor_norm_exec_rejects_banned_tokens_before_execution() -> None:
    executor = _executor_with_fake_client({"account": [{"_id": 1}]})
    fake_account = executor._connect()["tend_run123_financial"]["account"]

    with pytest.raises(DisabledOperatorError):
        executor.norm_exec(
            "financial",
            'db.account.aggregate([{"$sample": {"size": 1}}])',
        )

    assert fake_account.aggregate_calls == []


def test_mongo_executor_execute_prefix_uses_safe_norm_exec_adapter() -> None:
    from tend.solver.per_stage import PrefixExecutionRequest

    executor = _executor_with_fake_client({"account": [{"_id": 1}, {"_id": 2}]})
    request = PrefixExecutionRequest(
        db_id="financial",
        collection="account",
        stage_index=1,
        stage={"$limit": 1},
        pipeline=({"$limit": 1},),
        mql='db.account.aggregate([{"$limit":1}])',
    )

    result = executor.execute_prefix(request)
    fake_account = executor._connect()["tend_run123_financial"]["account"]

    assert result.variants[0].documents == ({"_id": 1},)
    assert result.variants[0].input_count == 2
    assert fake_account.aggregate_calls == [
        {"pipeline": [{"$limit": 1}], "maxTimeMS": _mongo_mod._EXEC_MAX_TIME_MS}
    ]


def test_mongo_executor_readonly_probe_forces_limit_and_returns_summary_not_rows() -> None:
    executor = _executor_with_fake_client(
        {
            "account": [
                {"_id": 1, "email": "alice@example.com", "status": "active"},
                {"_id": 2, "email": "bob@example.com", "status": "active"},
                {"_id": 3, "email": "carol@example.com", "status": "active"},
            ]
        }
    )

    result = executor.run_readonly_probe(
        "financial",
        'db.account.aggregate([{"$match": {"status": "active"}}])',
        limit=2,
    )
    fake_account = executor._connect()["tend_run123_financial"]["account"]

    assert fake_account.aggregate_calls == [
        {
            "pipeline": [{"$match": {"status": "active"}}, {"$limit": 2}],
            "maxTimeMS": _mongo_mod._EXEC_MAX_TIME_MS,
        }
    ]
    assert result["ok"] is True
    assert result["collection"] == "account"
    assert result["forced_limit"] == 2
    assert result["result_count"] == 2
    assert "documents" not in result
    assert result["result_shape"]["paths"]["email"]["type_counts"] == {"str": 2}
    assert "alice@example.com" not in str(result)
