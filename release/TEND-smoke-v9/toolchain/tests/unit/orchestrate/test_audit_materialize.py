"""Unit tests for audit materialization into release bundles."""

from __future__ import annotations

import json
from pathlib import Path

from tend.orchestrate.audit_materialize import materialize_audit_trail
from tend.orchestrate.publish import check_c1_c9


def test_materialize_audit_trail_resolves_c8_refs(tmp_path: Path) -> None:
    in_root = tmp_path / "input"
    out_root = tmp_path / "release"
    audit_in = in_root / "audit" / "demo_db" / "1001"
    audit_in.mkdir(parents=True)
    (audit_in / "pv.yaml").write_text("pass: true\n", encoding="utf-8")
    (audit_in / "rtv.yaml").write_text("rtv_pass: true\n", encoding="utf-8")
    (audit_in / "nnc.yaml").write_text("pass: true\n", encoding="utf-8")

    (in_root / "agent_design_rationale").mkdir(parents=True)
    (in_root / "agent_design_rationale" / "demo_db.yaml").write_text("db_id: demo_db\n", encoding="utf-8")
    (in_root / "mongodb_schema").mkdir()
    (in_root / "mongodb_data").mkdir()
    (in_root / "mongodb_schema" / "demo_db.json").write_text('{"demo_db": {"_id": "INT"}}', encoding="utf-8")
    (in_root / "mongodb_data" / "demo_db.json").write_text('{"items": [{"_id": 1}]}', encoding="utf-8")

    record = {
        "record_id": 1001,
        "db_id": "demo_db",
        "nl_queries": {"canonical": "q", "colloquial": "q"},
        "MQL": 'db.items.aggregate([{"$match": {"_id": 1}}])',
        "canonical_form_set": {
            "must_contain": ["$match"],
            "must_not_contain": [],
            "must_contain_at_root": ["$match"],
            "must_not_contain_at_root": [],
        },
        "difficulty": "L1",
        "sql_infeasibility_class": "feasible",
        "schema_flex": "none",
        "shape_policy": "reshape",
        "world_signature": "sha256:" + "a" * 64,
        "domain_id": "demo",
        "join_depth": 0,
        "aggregation_depth": "shallow",
        "schema_pattern": "embed",
        "agent_design_rationale_ref": "agent_design_rationale/demo_db.yaml",
        "property_verification_ref": "audit/demo_db/1001/pv.yaml",
        "round_trip_ref": "audit/demo_db/1001/rtv.yaml",
        "_diagnostic_bridge_ref": "audit/demo_db/1001/nnc.yaml",
    }

    materialize_audit_trail(out_root, [record], input_root=in_root)
    shutil_copy_library(in_root, out_root)

    errors = check_c1_c9([record], out_root)
    assert errors == []
    assert (out_root / "audit" / "demo_db" / "1001" / "pv.yaml").exists()


def shutil_copy_library(in_root: Path, out_root: Path) -> None:
    for sub in ("mongodb_schema", "mongodb_data", "agent_design_rationale"):
        src = in_root / sub
        dst = out_root / sub
        dst.mkdir(parents=True, exist_ok=True)
        for path in src.glob("*"):
            dst.joinpath(path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
