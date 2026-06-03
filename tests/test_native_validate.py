from __future__ import annotations

import json
from pathlib import Path

import yaml

from tend.execution import mql_signature, mql_skeleton_signature, mql_skeleton_summary, world_signature


def _native_record(dataset_dir: Path) -> dict:
    data = {
        "account_activity": [
            {"_id": 1, "activity_by_month": {"2024-01": {"credit": 100.0}}}
        ]
    }
    mql = (
        'db.account_activity.aggregate([{"$addFields":{"entries":{"$objectToArray":'
        '"$activity_by_month"}}},{"$addFields":{"matches":{"$filter":{"input":"$entries",'
        '"as":"kv","cond":{"$ne":["$$kv.v",null]}}}}},{"$project":{"_id":1,'
        '"activity_by_month":1,"matches":1}}])'
    )
    return {
        "record_id": 1,
        "db_id": "financial",
        "mechanism": "native_schema_flex",
        "archetype": "dynamic_key_comparison",
        "schema_feature": "account_activity.activity_by_month",
        "nl_queries": {
            "canonical": "Find account activity records with non-empty monthly activity.",
            "colloquial": "Show accounts that have monthly activity entries.",
        },
        "MQL": mql,
        "mql_signature": mql_signature(mql),
        "mql_skeleton_signature": mql_skeleton_signature(mql),
        "mql_skeleton_summary": mql_skeleton_summary(mql),
        "canonical_form_set": {
            "must_contain": ["$objectToArray", "$filter"],
            "must_not_contain": ["$sample", "$rand", "$$NOW", "$out", "$merge", "$function"],
            "must_contain_at_root": [],
            "must_not_contain_at_root": [],
            "native_must_contain": ["$objectToArray", "$filter"],
        },
        "difficulty": "L4",
        "sql_infeasibility_class": "structural_schema_flex",
        "shape_policy": "preserve",
        "schema_flex": "dynamic_key",
        "world_signature": world_signature(data),
        "native_feature_id": "account_activity.activity_by_month",
        "native_feature_type": "dynamic_key_object",
        "native_query_pattern": "dynamic_key_comparison",
        "mongo_native_constructs": ["$objectToArray", "$filter"],
        "anti_sql_transfer_level": "strong",
        "anti_sql_transfer_evidence": ["strong_native_ops=$objectToArray"],
        "provenance_refs": ["account.account_id", "trans.amount"],
        "migration_recipe_ref": "migration_recipe/financial.yaml",
        "native_verification": {"ok": True, "errors": []},
        "native_metadata": {
            "feature_id": "account_activity.activity_by_month",
            "feature_type": "dynamic_key_object",
            "query_pattern": "dynamic_key_comparison",
            "anti_sql_transfer_target": "strong",
        },
    }


def _write_native_dataset(tmp_path: Path) -> Path:
    data = {
        "account_activity": [
            {"_id": 1, "activity_by_month": {"2024-01": {"credit": 100.0}}}
        ]
    }
    record = _native_record(tmp_path)
    (tmp_path / "mongodb_data").mkdir()
    (tmp_path / "mongodb_schema").mkdir()
    (tmp_path / "agent_design_rationale").mkdir()
    (tmp_path / "migration_recipe").mkdir()
    (tmp_path / "native_feature_manifest").mkdir()
    (tmp_path / "provenance").mkdir()
    (tmp_path / "mongodb_data" / "financial.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "mongodb_schema" / "financial.json").write_text(
        json.dumps({"account_activity": {"fields": ["activity_by_month"]}}),
        encoding="utf-8",
    )
    (tmp_path / "agent_design_rationale" / "financial.yaml").write_text(
        yaml.safe_dump({"design_goal": "native financial activity"}),
        encoding="utf-8",
    )
    (tmp_path / "migration_recipe" / "financial.yaml").write_text(
        yaml.safe_dump({"db_id": "financial", "recipe_version": 1, "collections": {}}),
        encoding="utf-8",
    )
    (tmp_path / "native_feature_manifest" / "financial.yaml").write_text(
        yaml.safe_dump(
            {
                "db_id": "financial",
                "features": [
                    {
                        "id": "account_activity.activity_by_month",
                        "type": "dynamic_key_object",
                        "collection": "account_activity",
                        "field": "activity_by_month",
                        "query_patterns": ["dynamic_key_comparison"],
                        "required_constructs": ["$objectToArray", "$filter"],
                        "provenance_refs": ["account.account_id", "trans.amount"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "provenance" / "financial.json").write_text(
        json.dumps(
            {
                "db_id": "financial",
                "conversion_code_ref": "tend.construction.designs.financial",
                "entries": {
                    "account_activity.activity_by_month": {
                        "source_columns": ["account.account_id", "trans.amount"]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    catalog = {
        "source_version": "1.0",
        "generated_at": "2026-06-03T00:00:00+00:00",
        "selection_policy": {},
        "databases": [
            {
                "db_id": "financial",
                "domain_id": "finance",
                "sqlite_path": "/tmp/financial.sqlite",
                "table_count": 4,
                "query_count": 1,
                "selected": True,
                "flex_eligible": True,
                "selection_reason": "constructed by tend workflow",
            }
        ],
    }
    (tmp_path / "bird_db_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (tmp_path / "test.json").write_text(json.dumps([record]), encoding="utf-8")
    (tmp_path / "TEND.json").write_text(json.dumps([record]), encoding="utf-8")
    return tmp_path


def test_native_validation_rejects_missing_manifest(tmp_path: Path):
    from tend.publish.validate import validate_release

    dataset = _write_native_dataset(tmp_path)
    (dataset / "native_feature_manifest" / "financial.yaml").unlink()

    report = validate_release(dataset, require_all_dbs=False)

    issues = report.record_violations + report.file_violations
    assert not report.ok
    assert any("missing native feature manifest" in issue for issue in issues)


def test_native_validation_rejects_unresolved_provenance(tmp_path: Path):
    from tend.publish.validate import validate_release

    dataset = _write_native_dataset(tmp_path)
    records = json.loads((dataset / "test.json").read_text())
    records[0]["provenance_refs"] = ["missing.table_column"]
    (dataset / "test.json").write_text(json.dumps(records), encoding="utf-8")
    (dataset / "TEND.json").write_text(json.dumps(records), encoding="utf-8")

    report = validate_release(dataset, require_all_dbs=False)

    assert not report.ok
    assert any("unresolved provenance refs" in issue for issue in report.record_violations)


def test_native_validation_rejects_false_dynamic_key_claim(tmp_path: Path):
    from tend.publish.validate import validate_release

    dataset = _write_native_dataset(tmp_path)
    records = json.loads((dataset / "test.json").read_text())
    records[0]["MQL"] = 'db.account_activity.aggregate([{"$project":{"_id":1}}])'
    records[0]["mql_signature"] = mql_signature(records[0]["MQL"])
    records[0]["mql_skeleton_signature"] = mql_skeleton_signature(records[0]["MQL"])
    records[0]["mql_skeleton_summary"] = mql_skeleton_summary(records[0]["MQL"])
    (dataset / "test.json").write_text(json.dumps(records), encoding="utf-8")
    (dataset / "TEND.json").write_text(json.dumps(records), encoding="utf-8")

    report = validate_release(dataset, require_all_dbs=False)

    assert not report.ok
    assert any("claimed native constructs absent from MQL" in issue for issue in report.record_violations)
