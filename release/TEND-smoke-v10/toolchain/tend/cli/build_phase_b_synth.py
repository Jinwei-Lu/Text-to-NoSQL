"""CLI entrypoint for Phase B synthesis (QPS → MS → MUT → PV)."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from tend.config import FIXTURES_ROOT, REPO_ROOT, SPIDER_DATA_ROOT, default_llm_stub, use_fixtures
from tend.core import EX_verdict
from tend.core.io import load_json
from tend.orchestrate.paths import record_audit_dir
from tend.schemas.validators import validate

from tend.phase_b import (
    build_mutations_payload,
    generate_mutations,
    ms_synthesize,
    sample_query_plan,
    verify_properties,
)


def _build_orchestra_witness_from_sqlite() -> dict[str, Any]:
    db_path = SPIDER_DATA_ROOT / "database" / "orchestra" / "orchestra.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM conductor ORDER BY Conductor_ID")
    conductors = [dict(row) for row in cur.fetchall()]
    for conductor in conductors:
        cid = conductor["Conductor_ID"]
        conductor["_id"] = cid
        del conductor["Conductor_ID"]
        cur.execute(
            "SELECT * FROM orchestra WHERE Conductor_ID=? ORDER BY Orchestra_ID",
            (cid,),
        )
        orchestras: list[dict[str, Any]] = []
        for orchestra in cur.fetchall():
            od = dict(orchestra)
            oid = od["Orchestra_ID"]
            del od["Orchestra_ID"]
            del od["Conductor_ID"]
            cur.execute(
                "SELECT * FROM performance WHERE Orchestra_ID=? ORDER BY Performance_ID",
                (oid,),
            )
            performances: list[dict[str, Any]] = []
            for perf in cur.fetchall():
                pd = dict(perf)
                pid = pd["Performance_ID"]
                del pd["Performance_ID"]
                del pd["Orchestra_ID"]
                cur.execute("SELECT Attendance FROM show WHERE Performance_ID=?", (pid,))
                show_row = cur.fetchone()
                if show_row is not None:
                    pd["Attendance"] = show_row["Attendance"]
                performances.append(pd)
            od["performance"] = performances
            orchestras.append(od)
        conductor["orchestra"] = orchestras
    conn.close()
    return {"conductor": conductors}


def load_witness(db_id: str, out_root: Path) -> dict[str, Any]:
    phase_a_path = out_root / "mongodb_data" / f"{db_id}.json"
    if phase_a_path.exists():
        payload = load_json(phase_a_path)
        if isinstance(payload, dict) and db_id in payload:
            return {db_id: payload[db_id]} if isinstance(payload[db_id], list) else payload
        return payload

    fixture_path = FIXTURES_ROOT / db_id / "mongodb_data.json"
    if fixture_path.exists():
        return load_json(fixture_path)

    if db_id == "orchestra":
        return _build_orchestra_witness_from_sqlite()
    raise FileNotFoundError(
        f"No witness snapshot for {db_id}; run Phase A DM or provide mongodb_data.json"
    )


def load_schema(db_id: str, out_root: Path) -> dict[str, Any]:
    phase_a_path = out_root / "mongodb_schema" / f"{db_id}.json"
    if phase_a_path.exists():
        schema = load_json(phase_a_path)
        return {"collections": list(schema.keys()), **schema}
    return {"collections": [db_id], "root_collection": db_id}


def run_phase_b_synth(
    db_id: str,
    *,
    plan_pattern: str | None = None,
    out_root: Path | None = None,
    record_id: int = 1001,
    llm_stub: bool | None = None,
) -> dict[str, Any]:
    out_root = out_root or (REPO_ROOT / "out" / "TEND")
    if llm_stub is None:
        llm_stub = default_llm_stub()
    witness = load_witness(db_id, out_root)
    schema = load_schema(db_id, out_root)

    qps_output = sample_query_plan(
        db_id,
        plan_pattern=plan_pattern,
        schema=schema,
        witness=witness,
        use_fixture=llm_stub,
    )
    query_plan = qps_output["query_plan"]

    ms_output = ms_synthesize(query_plan, schema, witness, llm_stub=llm_stub)
    validate(ms_output["canonical_form_set"], "canonical_form_set")

    mutations = generate_mutations(
        query_plan,
        ms_output["MQL"],
        ms_output["canonical_form_set"],
        seed=42,
        min_n=5,
        max_n=5,
        use_fixture=llm_stub,
    )
    mutations_payload = build_mutations_payload(record_id, db_id, mutations)
    validate(mutations_payload, "mutations")

    pv_output = verify_properties(
        query_plan,
        ms_output,
        mutations,
        witness,
        record_id=record_id,
        db_id=db_id,
    )
    validate(pv_output, "property_verification")

    gold_record = {
        "record_id": record_id,
        "db_id": db_id,
        "MQL": ms_output["MQL"],
        "canonical_form_set": ms_output["canonical_form_set"],
    }
    fixture_record_path = FIXTURES_ROOT / db_id / "record.json"
    if fixture_record_path.exists():
        fixture_record = load_json(fixture_record_path)
        gold_record["MQL"] = fixture_record["MQL"]
        gold_record["canonical_form_set"] = fixture_record["canonical_form_set"]
        ex_vs_fixture = EX_verdict(ms_output["MQL"], fixture_record, witness)
    else:
        ex_vs_fixture = EX_verdict(ms_output["MQL"], gold_record, witness)

    audit_dir = record_audit_dir(db_id, record_id, out_root)
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "qps.yaml").write_text(
        yaml.safe_dump(qps_output, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (audit_dir / "ms.yaml").write_text(
        yaml.safe_dump(ms_output, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (audit_dir / "mutations.json").write_text(
        json.dumps(mutations_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (audit_dir / "pv.yaml").write_text(
        yaml.safe_dump(pv_output, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    return {
        "db_id": db_id,
        "record_id": record_id,
        "qps": qps_output,
        "ms": ms_output,
        "mutations": mutations_payload,
        "pv": pv_output,
        "ex_vs_fixture": ex_vs_fixture,
        "audit_dir": str(audit_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase B synthesis for a database.")
    parser.add_argument("--db", required=True, help="Spider db_id (e.g. orchestra)")
    parser.add_argument("--plan-pattern", default=None, help="Force primary_pattern for QPS")
    parser.add_argument("--out", default=str(REPO_ROOT / "out" / "TEND"), help="Output root")
    parser.add_argument("--record", type=int, default=1001, help="Record id")
    args = parser.parse_args()

    result = run_phase_b_synth(
        args.db,
        plan_pattern=args.plan_pattern,
        out_root=Path(args.out),
        record_id=args.record,
    )
    print(
        f"Phase B synth OK: db={result['db_id']} record={result['record_id']} "
        f"EX_vs_fixture={result['ex_vs_fixture']} audit={result['audit_dir']}"
    )
    if not result["pv"]["pv_pass"]:
        print("PV failures:", result["pv"]["pv_trace"]["blocking_failures"])
        return 1
    if args.db == "orchestra" and not result["ex_vs_fixture"]:
        print("Orchestra MVP requires EX=1 against fixture gold MQL")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
