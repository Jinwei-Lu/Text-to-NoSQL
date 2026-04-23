from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any
import csv
import json

from .execution import ExecutionBackend, build_execution_backend
from .metrics import (
    ast_check,
    exact_match,
    execution_accuracy,
    execution_field_match,
    execution_value_match,
    has_forbidden_operator,
    parse_failure_fingerprint,
    query_field_coverage,
    query_intent_match,
    query_structure_match,
)
from .models import EvaluationRow, Record, load_json, write_json


METRIC_NAMES = ("em", "qsm", "qfc", "ex", "efm", "evm", "qim")


def _load_records(bundle_root: Path, split: str) -> list[Record]:
    payload = load_json(bundle_root / f"{split}.json")
    return [Record.from_dict(item) for item in payload]


def _load_db_asset(bundle_root: Path, folder: str, db_id: str) -> dict[str, Any]:
    return load_json(bundle_root / folder / f"{db_id}.json")


def export_solver_view(bundle_root: Path, out_path: Path, split: str = "test") -> Path:
    records = _load_records(bundle_root, split)
    exported: list[dict[str, Any]] = []
    for record in records:
        schema = _load_db_asset(bundle_root, "mongodb_schema", record.db_id)
        witness = _load_db_asset(bundle_root, "mongodb_data", record.db_id)
        phenomena = _load_db_asset(bundle_root, "phenomena_registry", record.db_id)
        exported.append(record.solver_view(schema=schema, witness=witness, phenomena_meta=phenomena))
    write_json(out_path, exported)
    return out_path


def _load_predictions(predictions_path: Path) -> dict[int, str]:
    predictions: dict[int, str] = {}
    with predictions_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            predictions[int(item["record_id"])] = item["prediction"]
    return predictions


def _mean(rows: list[EvaluationRow], key: str) -> float:
    if not rows:
        return 0.0
    return round(sum(getattr(row, key) for row in rows) / len(rows), 6)


def _build_summary(rows: list[EvaluationRow], records: list[Record]) -> dict[str, Any]:
    by_record = {record.record_id: record for record in records}
    summary: dict[str, Any] = {
        "count": len(rows),
        "overall": {name: _mean(rows, name) for name in METRIC_NAMES},
        "slices": {},
    }

    slice_fields = (
        "empirical_difficulty",
        "nosql_nativeness_level",
        "operator_family",
        "shape_policy",
        "tds_cell",
    )

    for field_name in slice_fields:
        grouped: dict[str, list[EvaluationRow]] = defaultdict(list)
        for row in rows:
            field_value = getattr(by_record[row.record_id], field_name)
            if field_value:
                grouped[field_value].append(row)
        summary["slices"][field_name] = {
            value: {name: _mean(group_rows, name) for name in METRIC_NAMES}
            for value, group_rows in sorted(grouped.items())
        }
    return summary


def evaluate_bundle(
    bundle_root: Path,
    predictions_path: Path,
    out_dir: Path,
    split: str = "test",
    backend: ExecutionBackend | None = None,
    backend_name: str = "replay",
    mongo_uri: str = "mongodb://localhost:27017",
) -> list[EvaluationRow]:
    records = _load_records(bundle_root, split)
    predictions = _load_predictions(predictions_path)
    backend = backend or build_execution_backend(
        bundle_root=bundle_root,
        backend_name=backend_name,
        mongo_uri=mongo_uri,
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[EvaluationRow] = []
    try:
        for record in records:
            prediction = predictions.get(record.record_id, "")
            forbidden_hit = has_forbidden_operator(prediction)
            ast_result = ast_check(prediction, record.canonical_form_set)

            if ast_result == "fail:parse_error":
                em, qsm, qfc, ex, efm, evm, qim = parse_failure_fingerprint()
                row = EvaluationRow(
                    record_id=record.record_id,
                    db_id=record.db_id,
                    prediction=prediction,
                    em=em,
                    qsm=qsm,
                    qfc=qfc,
                    ex=ex,
                    efm=efm,
                    evm=evm,
                    qim=qim,
                    ast_result=ast_result,
                    forbidden_op_hit=forbidden_hit,
                    exec_error="parse_error",
                )
                rows.append(row)
                continue

            witness = _load_db_asset(bundle_root, "mongodb_data", record.db_id)
            exec_error: str | None = None
            predicted_result: list[dict[str, Any]] | None = None
            gold_result: list[dict[str, Any]] | None = None
            try:
                predicted_result = backend.norm_exec(record, prediction, witness)
                gold_result = backend.norm_exec(record, record.mql, witness)
            except Exception as exc:  # noqa: BLE001
                exec_error = str(exc)

            em = exact_match(prediction, record.mql)
            qsm = query_structure_match(prediction, record.mql)
            qfc = query_field_coverage(prediction, record.mql)
            qim = query_intent_match(prediction, record.canonical_form_set)

            if exec_error is None and predicted_result is not None and gold_result is not None:
                ex = execution_accuracy(prediction, record.canonical_form_set, predicted_result, gold_result)
                efm = execution_field_match(predicted_result, gold_result)
                evm = execution_value_match(predicted_result, gold_result) if efm else 0
            else:
                ex = 0
                efm = 0
                evm = 0

            if forbidden_hit:
                ex = 0

            rows.append(
                EvaluationRow(
                    record_id=record.record_id,
                    db_id=record.db_id,
                    prediction=prediction,
                    em=em,
                    qsm=qsm,
                    qfc=qfc,
                    ex=ex,
                    efm=efm,
                    evm=evm,
                    qim=qim,
                    ast_result=ast_result,
                    forbidden_op_hit=forbidden_hit,
                    exec_error=exec_error,
                )
            )
    finally:
        backend.close()

    _write_outputs(out_dir, rows, records)
    return rows


def _write_outputs(out_dir: Path, rows: list[EvaluationRow], records: list[Record]) -> None:
    csv_path = out_dir / "fingerprints.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "record_id",
                "db_id",
                "em",
                "qsm",
                "qfc",
                "ex",
                "efm",
                "evm",
                "qim",
                "ast_result",
                "forbidden_op_hit",
                "exec_error",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "record_id": row.record_id,
                    "db_id": row.db_id,
                    "em": row.em,
                    "qsm": row.qsm,
                    "qfc": row.qfc,
                    "ex": row.ex,
                    "efm": row.efm,
                    "evm": row.evm,
                    "qim": row.qim,
                    "ast_result": row.ast_result,
                    "forbidden_op_hit": int(row.forbidden_op_hit),
                    "exec_error": row.exec_error or "",
                }
            )

    write_json(out_dir / "rows.json", [row.to_dict() for row in rows])
    write_json(out_dir / "summary.json", _build_summary(rows, records))
