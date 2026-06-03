from __future__ import annotations

import json
from pathlib import Path

import yaml


class Dumpable:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def to_dict(self) -> dict:
        return self.payload


def _artifact_payload() -> dict:
    return {
        "mongodb_schema": {
            "collections": {
                "account_activity": {
                    "_id": "integer",
                    "activity_by_month": {"type": "object"},
                }
            }
        },
        "mongodb_data": {
            "account_activity": [
                {"_id": 1, "activity_by_month": {"2024-01": {"credit": 100.0}}}
            ]
        },
        "rationale": {"design_goal": "native financial documents"},
        "world_signature": "native-world",
        "migration_recipe": Dumpable(
            {
                "db_id": "financial",
                "recipe_version": 1,
                "collections": {"account_activity": {"transforms": []}},
            }
        ),
        "native_feature_manifest": Dumpable(
            {
                "db_id": "financial",
                "features": [
                    {
                        "id": "account_activity.activity_by_month",
                        "type": "dynamic_key_object",
                    }
                ],
            }
        ),
        "provenance": {
            "db_id": "financial",
            "entries": {
                "account_activity.activity_by_month": {
                    "source_columns": ["account.account_id", "trans.amount"]
                }
            },
        },
        "domain_id": "finance",
        "sqlite_path": "/tmp/financial.sqlite",
        "table_count": 4,
        "query_count": 32,
        "conversion_code_ref": "tend.construction.designs.financial",
        "query_bearing": True,
    }


def test_native_writers_materialize_recipe_manifest_and_provenance(tmp_path: Path) -> None:
    from tend.construction.artifacts import (
        write_native_feature_manifest,
        write_native_recipe,
        write_provenance,
    )

    write_native_recipe(tmp_path, "financial", Dumpable({"db_id": "financial"}))
    write_native_feature_manifest(
        tmp_path,
        "financial",
        Dumpable({"db_id": "financial", "features": [{"id": "f1"}]}),
    )
    write_provenance(tmp_path, "financial", {"db_id": "financial", "entries": {"f1": {}}})

    assert yaml.safe_load((tmp_path / "migration_recipe" / "financial.yaml").read_text()) == {
        "db_id": "financial"
    }
    assert yaml.safe_load(
        (tmp_path / "native_feature_manifest" / "financial.yaml").read_text()
    ) == {"db_id": "financial", "features": [{"id": "f1"}]}
    assert json.loads((tmp_path / "provenance" / "financial.json").read_text()) == {
        "db_id": "financial",
        "entries": {"f1": {}},
    }


def test_write_native_phase_a_materializes_native_artifact_tree(tmp_path: Path) -> None:
    from tend.construction.artifacts import write_native_phase_a
    from tend.construction.phase_a import NativeDbArtifacts

    artifacts = {"financial": NativeDbArtifacts(**_artifact_payload())}

    write_native_phase_a(tmp_path, artifacts)

    assert json.loads((tmp_path / "mongodb_schema" / "financial.json").read_text()) == (
        artifacts["financial"].mongodb_schema
    )
    assert json.loads((tmp_path / "mongodb_data" / "financial.json").read_text()) == (
        artifacts["financial"].mongodb_data
    )
    assert yaml.safe_load(
        (tmp_path / "agent_design_rationale" / "financial.yaml").read_text()
    ) == {"design_goal": "native financial documents"}
    assert yaml.safe_load((tmp_path / "migration_recipe" / "financial.yaml").read_text()) == {
        "db_id": "financial",
        "recipe_version": 1,
        "collections": {"account_activity": {"transforms": []}},
    }
    assert yaml.safe_load(
        (tmp_path / "native_feature_manifest" / "financial.yaml").read_text()
    )["features"] == [{"id": "account_activity.activity_by_month", "type": "dynamic_key_object"}]
    assert json.loads((tmp_path / "provenance" / "financial.json").read_text())["entries"] == {
        "account_activity.activity_by_month": {
            "source_columns": ["account.account_id", "trans.amount"]
        }
    }

    catalog = json.loads((tmp_path / "bird_db_catalog.json").read_text())
    assert catalog["databases"] == [
        {
            "db_id": "financial",
            "domain_id": "finance",
            "sqlite_path": "/tmp/financial.sqlite",
            "table_count": 4,
            "query_count": 32,
            "selected": True,
            "flex_eligible": True,
            "selection_reason": "constructed by tend native workflow",
        }
    ]
