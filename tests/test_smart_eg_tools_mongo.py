from __future__ import annotations

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

    def list_collections(self, db_id):
        return list(self.docs)

    def sample_documents(self, db_id, collection, limit=3, **_kwargs):
        return self.docs[collection][:limit]

    def aggregate_readonly_bounded(self, db_id, mql, limit=50):
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


def test_readonly_probe_rejects_banned_operators() -> None:
    tools = SmartEGMongoTools(_Mongo(), "financial")

    with pytest.raises(ValueError, match="disabled operator"):
        tools.run_readonly_probe(
            {
                "MQL": 'db.account.aggregate([{"$merge":"x"}])',
                "limit": 10,
            }
        )

