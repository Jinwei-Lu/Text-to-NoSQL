"""CLI: run Phase B validation agents for one record."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from tend.config import FIXTURES_ROOT, RUN_DIR, REPO_ROOT, use_fixtures
from tend.core.io import load_json
from tend.core import logging as log_module
from tend.core.io import write_json
from tend.orchestrate.paths import global_audit_dir, record_audit_dir
from tend.phase_b.nlp import paraphrase_nlq_pair
from tend.phase_b.nnc import assess_nnc
from tend.phase_b.ra import audit_realism
from tend.phase_b.rtv import rtv_verify
from tend.schemas.validators import validate


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_bundle(db_id: str, record_id: int) -> dict[str, Any]:
    base = FIXTURES_ROOT / db_id
    record = json.loads((base / "record.json").read_text(encoding="utf-8"))
    if int(record.get("record_id", 0)) != record_id:
        record["record_id"] = record_id
    qps = _load_yaml(base / "qps.yaml")
    wp = _load_yaml(base / "wp.yaml")
    sra = _load_yaml(base / "sra.yaml")
    dm = _load_yaml(base / "dm.yaml")
    return {
        "record": record,
        "query_plan": qps["query_plan"],
        "scenario_summary": wp.get("scenario_summary", ""),
        "schema_pattern": (sra.get("patterns_applied") or ["embed"])[0],
        "world_signature": dm.get("world_signature"),
    }


def _default_witness() -> dict[str, Any]:
    """Synthetic orchestra witness sufficient for window_facet_filter gold MQL."""
    return {
        "conductor": [
            {
                "_id": 1,
                "Name": "Alice",
                "orchestra": [
                    {
                        "performance": [
                            {"Performance_ID": 1, "Attendance": 10},
                            {"Performance_ID": 2, "Attendance": 20},
                            {"Performance_ID": 3, "Attendance": 30},
                        ]
                    }
                ],
            },
            {
                "_id": 2,
                "Name": "Bob",
                "orchestra": [
                    {
                        "performance": [
                            {"Performance_ID": 1, "Attendance": 15},
                            {"Performance_ID": 2, "Attendance": 25},
                            {"Performance_ID": 3, "Attendance": 35},
                        ]
                    }
                ],
            },
            {
                "_id": 3,
                "Name": None,
                "orchestra": [
                    {
                        "performance": [
                            {"Performance_ID": 1, "Attendance": None},
                            {"Performance_ID": 2, "Attendance": 40},
                            {"Performance_ID": 3, "Attendance": 50},
                        ]
                    }
                ],
            },
            {
                "_id": 4,
                "Name": "Dan",
                "orchestra": [
                    {
                        "performance": [
                            {"Performance_ID": 1, "Attendance": 5},
                            {"Performance_ID": 2, "Attendance": 5},
                            {"Performance_ID": 3, "Attendance": 5},
                        ]
                    }
                ],
            },
        ]
    }


def _load_witness(db_id: str, out_root: Path | None) -> dict[str, Any]:
    candidates = [
        REPO_ROOT / "out" / "TEND" / "mongodb_data" / f"{db_id}.json",
        FIXTURES_ROOT / db_id / "mongodb_data.json",
    ]
    if out_root:
        candidates.insert(0, out_root.parent / "TEND" / "mongodb_data" / f"{db_id}.json")
    for path in candidates:
        if path.exists():
            return load_json(path)
    return _default_witness()


def run_phase_b_valid(
    db_id: str,
    record_id: int,
    *,
    out_root: Path | None = None,
    snapshot: dict[str, Any] | None = None,
    prefer_fixture: bool | None = None,
    bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if prefer_fixture is None:
        prefer_fixture = use_fixtures()
    if bundle is None:
        bundle = _load_bundle(db_id, record_id)
    record = bundle["record"]
    query_plan = bundle["query_plan"]
    query_plan = {**query_plan, "db_id": db_id, "record_id": record_id}
    witness = snapshot or _load_witness(db_id, out_root)
    schema = {"db_id": db_id, "world_signature": bundle.get("world_signature")}

    audit_dir = record_audit_dir(db_id, record_id, out_root)
    global_dir = global_audit_dir(out_root)

    log_module.bind(db_id=db_id, record_id=record_id, stage="phase_b.valid")
    log_module.emit("nlp.paraphrase", agent_state="start")

    nlp_out = paraphrase_nlq_pair(
        record["MQL"],
        query_plan,
        record["canonical_form_set"],
        bundle["scenario_summary"],
        db_id=db_id,
        record_id=record_id,
        prefer_fixture=prefer_fixture,
    )
    validate(nlp_out["nl_queries"], "nlq")
    write_json(audit_dir / "nlp.yaml", nlp_out)
    log_module.emit("nlp.paraphrase", agent_state="done")

    log_module.emit("rtv.canonical.ok", agent_state="start")
    rtv_out = rtv_verify(
        nlp_out["nl_queries"],
        schema,
        witness,
        record["canonical_form_set"],
        gold_mql=record["MQL"],
        db_id=db_id,
        prefer_fixture=prefer_fixture,
    )
    write_json(audit_dir / "rtv.yaml", rtv_out)
    event = "rtv.canonical.ok" if rtv_out["rtv_pass"] else "rtv.canonical.fail"
    log_module.emit(event, agent_state="done", rtv_pass=rtv_out["rtv_pass"])

    log_module.emit("nnc.tier", agent_state="start")
    nnc_out = assess_nnc(
        record["MQL"],
        nlp_out["nl_queries"],
        record["canonical_form_set"],
        query_plan,
        witness,
        shape_policy=record.get("shape_policy", query_plan.get("shape_policy", "reshape")),
        round_trip_verification=rtv_out["round_trip_verification"],
        record=record,
        db_id=db_id,
        audit_dir=global_dir,
        prefer_fixture=prefer_fixture,
    )
    write_json(audit_dir / "nnc.yaml", nnc_out)
    log_module.emit(
        "nnc.bridge",
        agent_state="done",
        gate_pass=nnc_out["diagnostic_bridge"].get("gate_pass"),
    )

    log_module.emit("ra.audit", agent_state="start")
    schema_flex = bundle.get("schema_flex") or record.get("schema_flex", "none")
    sra_embed_depth = min(2, record["MQL"].count("$unwind"))
    ra_out = audit_realism(
        record["MQL"],
        nlp_out["nl_queries"],
        query_plan,
        witness,
        schema,
        schema_pattern=bundle["schema_pattern"],
        schema_flex=schema_flex,
        sra_embed_depth=sra_embed_depth,
    )
    write_json(audit_dir / "ra.yaml", ra_out)
    log_module.emit(
        "ra.augment",
        agent_state="done",
        ra_pass=ra_out["ra_audit"]["pass"],
        augment_applied=ra_out.get("snapshot") is not None,
    )

    status = "ok"
    if not rtv_out["rtv_pass"] or not nnc_out["nnc_verdict"]["pass"] or not ra_out["ra_audit"]["pass"]:
        status = "fail"

    gate_pass = bool(nnc_out["diagnostic_bridge"].get("gate_pass"))
    summary = {
        "db_id": db_id,
        "record_id": record_id,
        "status": status,
        "ok": status == "ok",
        "gate_pass": gate_pass,
        "nlp": nlp_out,
        "rtv": rtv_out,
        "nnc": nnc_out,
        "ra": ra_out,
        "audit_dir": str(audit_dir),
    }
    write_json(audit_dir / "phase_b_valid_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Phase B validation for one record.")
    parser.add_argument("--db", required=True, help="Spider db_id")
    parser.add_argument("--record", type=int, default=1001, help="Record id")
    parser.add_argument("--out", default=str(RUN_DIR), help="Output root (audit paths)")
    args = parser.parse_args(argv)

    try:
        result = run_phase_b_valid(args.db, args.record, out_root=Path(args.out))
        print(
            f"Phase B validation {result['status']} for {args.db}/{args.record} "
            f"(audit={result['audit_dir']})"
        )
        return 0 if result["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        print(f"build_phase_b_valid failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
