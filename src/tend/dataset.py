"""Dataset writer — persist Tier-1 release assets produced by the pipeline.

Mirrors the proposals/02 layout: per-db ``mongodb_schema/``, ``mongodb_data/``,
``agent_design_rationale/`` plus ``test.json`` / ``TEND.json`` (== test, sorted).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .workflow.flows import DbArtifacts


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _as_mapping(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def _write_yaml(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(_as_mapping(obj), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def write_phase_a(out_dir: Path, artifacts: dict[str, DbArtifacts]) -> None:
    for db_id, art in artifacts.items():
        _write_json(out_dir / "mongodb_schema" / f"{db_id}.json", art.mongodb_schema)
        _write_json(out_dir / "mongodb_data" / f"{db_id}.json", art.mongodb_data)
        rat_dir = out_dir / "agent_design_rationale"
        rat_dir.mkdir(parents=True, exist_ok=True)
        (rat_dir / f"{db_id}.yaml").write_text(
            yaml.safe_dump(art.rationale, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def write_records(out_dir: Path, records: list[dict[str, Any]]) -> None:
    ordered = sorted(records, key=lambda r: r.get("record_id", 0))
    _write_json(out_dir / "test.json", ordered)
    _write_json(out_dir / "TEND.json", ordered)


def write_catalog(out_dir: Path, artifacts: dict[str, DbArtifacts]) -> None:
    catalog = {
        "source_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selection_policy": {
            "min_tables": 1,
            "min_queries": 1,
            "require_non_empty_data": True,
            "min_flex_db_ratio": 0.30,
        },
        "databases": [
            {
                "db_id": db_id,
                "domain_id": art.domain_id,
                "sqlite_path": art.sqlite_path,
                "table_count": art.table_count,
                "query_count": art.query_count,
                "selected": True,
                "flex_eligible": art.query_bearing,
                "selection_reason": "constructed by tend workflow",
            }
            for db_id, art in sorted(artifacts.items())
        ]
    }
    _write_json(out_dir / "bird_db_catalog.json", catalog)


def write_native_recipe(out_dir: Path, db_id: str, recipe: Any) -> None:
    _write_yaml(out_dir / "migration_recipe" / f"{db_id}.yaml", recipe)


def write_native_feature_manifest(out_dir: Path, db_id: str, manifest: Any) -> None:
    _write_yaml(out_dir / "native_feature_manifest" / f"{db_id}.yaml", manifest)


def write_provenance(out_dir: Path, db_id: str, provenance: Any) -> None:
    _write_json(out_dir / "provenance" / f"{db_id}.json", _as_mapping(provenance))


def write_native_phase_a(out_dir: Path, artifacts: dict[str, Any]) -> None:
    for db_id, art in artifacts.items():
        _write_json(out_dir / "mongodb_schema" / f"{db_id}.json", art.mongodb_schema)
        _write_json(out_dir / "mongodb_data" / f"{db_id}.json", art.mongodb_data)
        write_native_recipe(out_dir, db_id, art.migration_recipe)
        write_native_feature_manifest(out_dir, db_id, art.native_feature_manifest)
        write_provenance(out_dir, db_id, art.provenance)
    write_catalog(out_dir, artifacts)
