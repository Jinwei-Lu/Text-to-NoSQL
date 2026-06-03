from __future__ import annotations

import json

import pytest

from tend.solver.eg.mongo_tools import SmartEGMongoTools


class _Mongo:
    def __init__(self):
        self.docs = {
            "account": [
                {"_id": 1, "loan": {"amount": 10}, "tags": ["vip"], "metrics": {"Jan": 1}},
                {"_id": 2, "loan": None, "tags": [], "metrics": {"Feb": 2}},
            ]
        }
        self.probes = []

    def list_collections(self, db_id):
        return list(self.docs)

    def sample_documents(self, db_id, collection, limit=3, **_kwargs):
        return self.docs[collection][:limit]

    def aggregate_readonly_bounded(self, db_id, mql, limit=50):
        self.probes.append({"db_id": db_id, "mql": mql, "limit": limit})
        return {"collection": "account", "count": 2, "sample": self.docs["account"][:1]}


def test_environment_tools_return_bounded_redacted_summaries() -> None:
    tools = SmartEGMongoTools(_Mongo(), "financial")

    listed = tools.list_collections({})
    sample = tools.sample_documents({"collection": "account", "limit": 2})
    profile = tools.profile_path({"collection": "account", "path": "loan.amount"})
    values = tools.profile_path_values({"collection": "account", "path": "metrics.*"})

    assert listed["collections"] == ["account"]
    assert sample["sample_count"] == 2
    assert "documents" not in sample
    assert profile["exists_count"] == 1
    assert values["value_count"] == 2
    assert values["redaction"]["raw_rows"] is False


def test_sample_documents_returns_compact_shape_summary_for_deep_documents() -> None:
    class DeepMongo(_Mongo):
        def __init__(self):
            super().__init__()
            self.docs["account"] = [
                {
                    "_id": idx,
                    "relationships": {
                        "members_by_role": {
                            f"ROLE_{role}": [
                                {
                                    "member_id": f"M{idx}-{role}-{member}",
                                    "cards": [
                                        {
                                            "card_id": f"C{idx}-{role}-{member}-{card}",
                                            "limits": {"daily": card * 100, "monthly": card * 1000},
                                        }
                                        for card in range(12)
                                    ],
                                }
                                for member in range(8)
                            ]
                            for role in range(12)
                        }
                    },
                }
                for idx in range(4)
            ]

    tools = SmartEGMongoTools(DeepMongo(), "financial")

    sample = tools.sample_documents({"collection": "account", "limit": 4})
    encoded = json.dumps(sample, sort_keys=True)

    assert "redacted_samples" not in sample
    assert sample["sample_count"] == 4
    assert sample["path_count"] > 0
    assert "paths" in sample
    assert len(encoded) < 12_000


def test_path_discovery_tools_bound_dynamic_path_output() -> None:
    class DynamicMongo(_Mongo):
        def __init__(self):
            super().__init__()
            self.docs["account"] = [
                {
                    "_id": idx,
                    "metrics": {
                        f"bucket_{bucket}": {
                            f"leaf_{leaf}": [{"value": leaf, "rank": n} for n in range(4)]
                            for leaf in range(12)
                        }
                        for bucket in range(80)
                    },
                }
                for idx in range(3)
            ]

    tools = SmartEGMongoTools(DynamicMongo(), "financial")

    discovered = tools.discover_paths("account", limit=3)
    array_shape = tools.inspect_array_shape("account", "metrics.*.*", limit=3)

    assert discovered["path_count"] > discovered["returned_path_count"]
    assert discovered["omitted_path_count"] > 0
    assert len(json.dumps(discovered, sort_keys=True)) < 12_000
    assert len(json.dumps(array_shape, sort_keys=True)) < 12_000


def test_readonly_probe_rejects_banned_operators() -> None:
    tools = SmartEGMongoTools(_Mongo(), "financial")

    with pytest.raises(ValueError, match="disabled operator"):
        tools.run_readonly_probe(
            {
                "MQL": 'db.account.aggregate([{"$merge":"x"}])',
                "limit": 10,
            }
        )


def test_readonly_probe_accepts_collection_and_pipeline() -> None:
    mongo = _Mongo()
    tools = SmartEGMongoTools(mongo, "financial")

    result = tools.run_readonly_probe(
        {
            "collection": "account",
            "pipeline": [{"$match": {"loan": {"$exists": True}}}, {"$project": {"loan": 1}}],
            "limit": 7,
        }
    )

    assert result["tool"] == "run_readonly_probe"
    assert result["redaction"]["raw_rows"] is False
    assert mongo.probes == [
        {
            "db_id": "financial",
            "mql": 'db.account.aggregate([{"$match":{"loan":{"$exists":true}}},{"$project":{"loan":1}}])',
            "limit": 7,
        }
    ]


def test_readonly_probe_accepts_collection_and_raw_pipeline_string() -> None:
    mongo = _Mongo()
    tools = SmartEGMongoTools(mongo, "financial")

    tools.run_readonly_probe(
        {
            "collection": "account",
            "MQL": '[{"$match":{"_id":{"$exists":true}}},{"$limit":1}]',
            "limit": 3,
        }
    )

    assert mongo.probes == [
        {
            "db_id": "financial",
            "mql": 'db.account.aggregate([{"$match":{"_id":{"$exists":true}}},{"$limit":1}])',
            "limit": 3,
        }
    ]
