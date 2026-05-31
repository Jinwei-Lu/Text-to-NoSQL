"""Build Phase A (DataWorld) assets for a Spider db_id."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from tend.config import default_llm_stub
from tend.core import logging as log_module
from tend.core.llm_client import LLMClient
from tend.phase_a.catalog import select_spider_dbs
from tend.phase_a.detectors import scan_phenomena
from tend.phase_a.dm import migrate
from tend.phase_a.sc import review_schema
from tend.phase_a.sra import design_schema
from tend.phase_a.wp import profile_workload
from tend.schemas.validators import validate


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_yaml(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def build_phase_a(
    db_id: str,
    out_root: Path,
    *,
    seed: int = 42,
    llm_stub: bool | None = None,
) -> dict[str, Path]:
    """Run WP → SRA → SC → DM for one db_id and write Tier-1/Tier-2 artifacts."""
    if llm_stub is None:
        llm_stub = default_llm_stub()
    log_module.configure_logging()
    log_module.bind(db_id=db_id, agent="phase_a", stage="build")
    log_module.emit("phase_a.start", db_id=db_id)

    llm = LLMClient(stub=llm_stub, use_cache=llm_stub)
    wp_output = profile_workload(db_id, llm=llm, seed=seed, use_fixture=llm_stub)
    schema, rationale = design_schema(wp_output, db_id=db_id)
    schema, rationale, sc_verdict = review_schema(wp_output, schema, rationale)
    data, migration_log = migrate(db_id, schema, rationale)
    phenomena = scan_phenomena(db_id, data)

    validate(migration_log, "migration_log")

    paths = {
        "schema": out_root / "mongodb_schema" / f"{db_id}.json",
        "data": out_root / "mongodb_data" / f"{db_id}.json",
        "rationale": out_root / "agent_design_rationale" / f"{db_id}.yaml",
        "wp_output": out_root.parent / "audit" / db_id / "wp_output.yaml",
        "migration_log": out_root.parent / "audit" / db_id / "migration_log.json",
        "phenomena_audit": out_root.parent / "audit" / db_id / "phenomena_audit.json",
    }
    _write_json(paths["schema"], schema)
    _write_json(paths["data"], data)
    _write_yaml(paths["rationale"], rationale)
    _write_yaml(paths["wp_output"], wp_output)
    _write_json(paths["migration_log"], migration_log)
    _write_json(paths["phenomena_audit"], phenomena)

    catalog_result = select_spider_dbs()
    catalog_path = out_root / "spider_db_catalog.json"
    _write_json(catalog_path, catalog_result["catalog"])
    warnings_path = out_root.parent / "audit" / "_global" / "domain_map_warnings.json"
    _write_json(warnings_path, catalog_result["domain_map_warnings"])

    flex_report = sc_verdict.flex_supply_report.copy()
    flex_report["selected_flex_ratio"] = catalog_result["catalog"].get("selected_flex_ratio", 0.0)
    flex_report_path = out_root.parent / "audit" / "_global" / "flex_supply_report.json"
    _write_json(flex_report_path, flex_report)

    log_module.emit("phase_a.done", db_id=db_id, world_signature=migration_log["world_signature"])
    paths["catalog"] = catalog_path
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Phase A DataWorld assets.")
    parser.add_argument("--db", required=True, help="Spider db_id (e.g. orchestra)")
    parser.add_argument("--out", default="out/TEND", help="Output root directory")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--llm-stub", action="store_true", help="Force stub/fixture mode (no API calls)")
    args = parser.parse_args(argv)

    out_root = Path(args.out)
    llm_stub = True if args.llm_stub else None
    paths = build_phase_a(args.db, out_root, seed=args.seed, llm_stub=llm_stub)
    print(f"Phase A built for {args.db}:")
    for name, path in paths.items():
        print(f"  {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
