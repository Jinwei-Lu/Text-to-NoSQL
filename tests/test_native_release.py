from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bson.objectid import ObjectId

from tend.execution import native_release


class FakeAdmin:
    def command(self, command: str) -> dict[str, int]:
        assert command == "ping"
        return {"ok": 1}


class FakeCollection:
    def __init__(self, name: str, aggregate_result: list[dict[str, Any]] | None = None) -> None:
        self.name = name
        self.docs: list[dict[str, Any]] = []
        self.indexes: list[str] = []
        self.insert_batch_sizes: list[int] = []
        self.aggregate_result = aggregate_result if aggregate_result is not None else []
        self.aggregate_calls: list[dict[str, Any]] = []

    def insert_many(self, docs: list[dict[str, Any]]) -> None:
        self.docs.extend(docs)
        self.insert_batch_sizes.append(len(docs))

    def create_index(self, field: str) -> str:
        self.indexes.append(field)
        return f"{field}_1"

    def aggregate(self, pipeline: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        self.aggregate_calls.append({"pipeline": pipeline, **kwargs})
        return self.aggregate_result


class FakeDatabase:
    def __init__(self, name: str) -> None:
        self.name = name
        self.collections: dict[str, FakeCollection] = {}

    def __getitem__(self, collection: str) -> FakeCollection:
        if collection not in self.collections:
            self.collections[collection] = FakeCollection(collection)
        return self.collections[collection]


class FakeMongoClient:
    instances: list["FakeMongoClient"] = []

    def __init__(self, uri: str, **kwargs: Any) -> None:
        self.uri = uri
        self.kwargs = kwargs
        self.admin = FakeAdmin()
        self.dropped: list[str] = []
        self.closed = False
        self.databases = {
            "admin": FakeDatabase("admin"),
            "config": FakeDatabase("config"),
            "local": FakeDatabase("local"),
            "financial": FakeDatabase("financial"),
            "tend_previous": FakeDatabase("tend_previous"),
        }
        FakeMongoClient.instances.append(self)

    def __getitem__(self, db_name: str) -> FakeDatabase:
        if db_name not in self.databases:
            self.databases[db_name] = FakeDatabase(db_name)
        return self.databases[db_name]

    def list_database_names(self) -> list[str]:
        return list(self.databases)

    def drop_database(self, db_name: str) -> None:
        assert db_name not in {"admin", "config", "local"}
        self.dropped.append(db_name)
        self.databases.pop(db_name, None)

    def close(self) -> None:
        self.closed = True


def _write_dataset_json(dataset_dir: Path, db_id: str, body: str) -> None:
    data_dir = dataset_dir / "mongodb_data"
    data_dir.mkdir(parents=True)
    (data_dir / f"{db_id}.json").write_text(body, encoding="utf-8")


def test_load_dataset_exact_databases_recreates_exact_db_and_indexes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeMongoClient.instances.clear()
    monkeypatch.setattr(native_release, "MongoClient", FakeMongoClient)
    dataset_dir = tmp_path / "dataset"
    _write_dataset_json(
        dataset_dir,
        "financial",
        """
        {
          "account": [
            {"_id": {"$oid": "64b64c000000000000000001"}, "account_id": 1, "name": "A"},
            {"_id": {"$oid": "64b64c000000000000000002"}, "account_id": 2, "city_id": {"nested": 7}}
          ],
          "loan": [
            {"_id": 10, "loan_id": 10, "account_id": 1}
          ],
          "empty": []
        }
        """,
    )

    result = native_release.load_dataset_exact_databases(
        dataset_dir,
        "mongodb://fake",
        db_ids=["financial"],
        drop_tend_prefixed=True,
    )

    client = FakeMongoClient.instances[-1]
    assert client.uri == "mongodb://fake"
    assert client.kwargs["serverSelectionTimeoutMS"] == 4000
    assert client.closed
    assert client.dropped == ["tend_previous", "financial"]
    assert result["dropped_tend_prefixed_databases"] == ["tend_previous"]
    assert result["dropped_exact_databases"] == ["financial"]
    assert result["databases"]["financial"]["collections"] == {
        "account": 2,
        "loan": 1,
        "empty": 0,
    }
    assert result["databases"]["financial"]["indexes"] == {
        "account": ["account_id"],
        "loan": ["account_id", "loan_id"],
    }
    assert result["total_documents"] == 3

    account = client.databases["financial"].collections["account"]
    assert account.insert_batch_sizes == [2]
    assert isinstance(account.docs[0]["_id"], ObjectId)
    assert account.indexes == ["account_id"]


def test_load_dataset_exact_databases_refuses_system_db_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeMongoClient.instances.clear()
    monkeypatch.setattr(native_release, "MongoClient", FakeMongoClient)
    dataset_dir = tmp_path / "dataset"
    _write_dataset_json(dataset_dir, "admin", '{"system": [{"_id": 1}]}')

    with pytest.raises(ValueError, match="refusing to drop protected MongoDB database"):
        native_release.load_dataset_exact_databases(
            dataset_dir,
            "mongodb://fake",
            db_ids=["admin"],
        )

    assert FakeMongoClient.instances == []


def test_execute_gold_records_exact_summarizes_without_returning_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeMongoClient.instances.clear()

    class ExecuteFakeMongoClient(FakeMongoClient):
        def __init__(self, uri: str, **kwargs: Any) -> None:
            super().__init__(uri, **kwargs)
            financial = self["financial"]
            financial.collections["account"] = FakeCollection(
                "account",
                aggregate_result=[{"_id": 1, "account_id": 1}],
            )
            financial.collections["empty"] = FakeCollection("empty", aggregate_result=[])

    monkeypatch.setattr(native_release, "MongoClient", ExecuteFakeMongoClient)
    records = [
        {
            "record_id": "ok",
            "db_id": "financial",
            "MQL": 'db.account.aggregate([{ "$match": { "account_id": 1 } }])',
        },
        {
            "record_id": "empty",
            "db_id": "financial",
            "MQL": "db.empty.aggregate([])",
        },
        {
            "record_id": "parse-error",
            "db_id": "financial",
            "MQL": "not an aggregate",
        },
        {"record_id": "missing-db", "MQL": "db.account.aggregate([])"},
        {"record_id": "missing-mql", "db_id": "financial"},
    ]

    result = native_release.execute_gold_records_exact(
        tmp_path,
        "mongodb://fake",
        records=records,
        max_time_ms=1234,
    )

    assert result["total"] == 5
    assert result["executed"] == 2
    assert result["failures"] == 3
    assert result["empty"] == 1
    assert result["field_missing"] == 2
    assert [row["record_id"] for row in result["empty_records"]] == ["empty"]
    assert [row["record_id"] for row in result["field_missing_records"]] == [
        "missing-db",
        "missing-mql",
    ]
    assert {row["stage"] for row in result["failure_records"]} == {"parse", "record"}
    assert result["records"][0]["index"] == 0
    assert result["records"][0]["record_id"] == "ok"
    assert result["records"][0]["db_id"] == "financial"
    assert result["records"][0]["collection"] == "account"
    assert result["records"][0]["result_count"] == 1
    assert result["records"][0]["ok"] is True
    assert "rows" not in result["records"][0]

    client = FakeMongoClient.instances[-1]
    assert client.closed
    account_call = client.databases["financial"].collections["account"].aggregate_calls[0]
    assert account_call["maxTimeMS"] == 1234
    assert account_call["pipeline"] == [{"$match": {"account_id": 1}}]
