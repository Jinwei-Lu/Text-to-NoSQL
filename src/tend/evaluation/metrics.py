"""Proposal 05 evaluator for TEND solver, baseline, and ablation outputs.

The evaluator is deliberately file-first:

* per-record fingerprints are written as JSONL and CSV for tooling;
* a compact ``report.json`` carries aggregate and slice scores;
* ``report.md`` is readable by a human or an agent during run triage.

Operational failures such as missing predictions, missing witness data, or MongoDB
unavailability are logged as anomalies. Bad model predictions are scored as 0 where the
proposal says so and are kept in the diagnostic counters rather than aborting the run.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import sys
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..errors import Anomaly, TendError, wrap_unexpected
from ..execution.ast_check import ast_check, parse_pipeline, root_ops, scan_disabled
from ..execution.mongo import equiv_rec
from ..execution.signature import canonical_json
from ..observability import RunLogger

EVALUATION_METRICS: tuple[str, ...] = ("EM", "QSM", "QFC", "EX", "EFM", "EVM", "QIM")
SLICE_AXES: tuple[str, ...] = (
    "domain",
    "join_depth",
    "aggregation_depth",
    "schema_pattern",
    "schema_flex",
    "difficulty_tier",
)
DIAGNOSTIC_SLICE_AXES: tuple[str, ...] = (
    "functional_sql_solvable",
    "structural_sql_solvable",
    "sql_infeasibility_class",
)
ORDER_SENSITIVE_ROOT_OPS = {"$sort", "$limit", "$skip", "$setWindowFields"}
FIELD_VALUE_KEYS = {"localField", "foreignField", "as"}
NON_FIELD_VALUE_KEYS = {"from"}


class EvaluationExecutor(Protocol):
    def load_witness(self, db_id: str, collections: dict[str, list[dict[str, Any]]]) -> None:
        ...

    def norm_exec(self, db_id: str, mql: str) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True, slots=True)
class EvaluationPaths:
    out_dir: Path
    per_record_jsonl: Path
    per_record_csv: Path
    report_json: Path
    report_md: Path

    @classmethod
    def under(cls, out_dir: Path) -> "EvaluationPaths":
        return cls(
            out_dir=out_dir,
            per_record_jsonl=out_dir / "per_record_metrics.jsonl",
            per_record_csv=out_dir / "per_record_metrics.csv",
            report_json=out_dir / "report.json",
            report_md=out_dir / "report.md",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "out_dir": str(self.out_dir),
            "per_record_jsonl": str(self.per_record_jsonl),
            "per_record_csv": str(self.per_record_csv),
            "report_json": str(self.report_json),
            "report_md": str(self.report_md),
        }


@dataclass(frozen=True, slots=True)
class EvaluationOutput:
    status: str
    report: dict[str, Any]
    paths: EvaluationPaths

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class _GoldRecord:
    record: dict[str, Any]
    gold_mql: str
    canonical_form_set: dict[str, Any]
    parsed: tuple[str, list[dict[str, Any]]] | None
    canonical_text: str
    structural_signature: Any
    field_paths: list[str]
    order_sensitive: bool
    gold_result: list[dict[str, Any]] | None = None


def evaluate_predictions(
    *,
    dataset_dir: Path,
    predictions_path: Path,
    out_dir: Path,
    experiment_kind: str,
    run_id: str,
    logger: RunLogger,
    progress: Any = None,
    executor: EvaluationExecutor | None = None,
    max_workers: int = 8,
) -> EvaluationOutput:
    """Evaluate a prediction JSONL file against a release dataset.

    ``experiment_kind`` is used only for artifact naming and system id extraction. It
    should be one of ``solver``, ``baseline``, ``ablation``, or a custom label.
    """
    paths = EvaluationPaths.under(out_dir)
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    log = logger.bind(component="evaluator", experiment_kind=experiment_kind)
    if progress:
        progress.phase("EVAL")

    log.info(
        "evaluation_start",
        dataset_dir=str(dataset_dir),
        predictions_path=str(predictions_path),
        out_dir=str(paths.out_dir),
        max_workers=max_workers,
    )

    try:
        records = _load_records(dataset_dir)
        predictions = _load_predictions(predictions_path)
    except TendError as err:
        err.with_context(
            dataset_dir=str(dataset_dir),
            predictions_path=str(predictions_path),
            experiment_kind=experiment_kind,
        )
        log.anomaly(err)
        return _write_failed_report(
            paths,
            run_id=run_id,
            experiment_kind=experiment_kind,
            message=err.message,
            error_code=err.anomaly.value if err.anomaly else Anomaly.INTERNAL.value,
            logger=log,
        )
    except Exception as exc:  # noqa: BLE001 - final evaluation boundary
        err = wrap_unexpected(
            exc,
            stage="evaluation_load",
            dataset_dir=str(dataset_dir),
            predictions_path=str(predictions_path),
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        log.anomaly(err)
        return _write_failed_report(
            paths,
            run_id=run_id,
            experiment_kind=experiment_kind,
            message=err.message,
            error_code=Anomaly.INTERNAL.value,
            logger=log,
        )

    if not predictions:
        log.anomaly(
            kind=Anomaly.SUPPLY_EXHAUSTED,
            message="prediction file contains no scorable predictions",
            predictions_path=str(predictions_path),
        )
        return _write_failed_report(
            paths,
            run_id=run_id,
            experiment_kind=experiment_kind,
            message="prediction file contains no scorable predictions",
            error_code=Anomaly.SUPPLY_EXHAUSTED.value,
            logger=log,
        )

    if executor is None or not _executor_available(executor):
        log.anomaly(
            kind=Anomaly.EXEC_ERROR,
            message="evaluation executor unavailable; cannot run proposal 05 NormExec",
            predictions_path=str(predictions_path),
        )
        return _write_failed_report(
            paths,
            run_id=run_id,
            experiment_kind=experiment_kind,
            message="evaluation executor unavailable; cannot run proposal 05 NormExec",
            error_code=Anomaly.EXEC_ERROR.value,
            logger=log,
        )

    record_index = {_record_key(record): record for record in records}
    try:
        _load_witnesses(dataset_dir, records, executor, log)
        gold = _prepare_gold_records(records, executor, log)
    except TendError as err:
        if not err.logged:
            log.anomaly(err)
        return _write_failed_report(
            paths,
            run_id=run_id,
            experiment_kind=experiment_kind,
            message=err.message,
            error_code=err.anomaly.value if err.anomaly else Anomaly.EXEC_ERROR.value,
            logger=log,
        )
    except Exception as exc:  # noqa: BLE001 - final evaluation boundary
        err = wrap_unexpected(
            exc,
            stage="evaluation_prepare",
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        log.anomaly(err)
        return _write_failed_report(
            paths,
            run_id=run_id,
            experiment_kind=experiment_kind,
            message=err.message,
            error_code=Anomaly.INTERNAL.value,
            logger=log,
        )

    if progress:
        progress.add_group(
            f"eval:{experiment_kind}",
            f"eval {experiment_kind}",
            phase="EVAL",
            total=len(predictions),
        )

    rows = _score_predictions_concurrently(
        predictions,
        record_index=record_index,
        gold=gold,
        executor=executor,
        experiment_kind=experiment_kind,
        run_id=run_id,
        logger=log,
        progress=progress,
        max_workers=max_workers,
    )
    rows.sort(
        key=lambda row: (
            str(row.get("system_id")),
            str(row.get("db_id")),
            row.get("record_id") or -1,
            row.get("prediction_index") or 0,
        )
    )

    _write_per_record(paths, rows)
    report = _build_report(
        rows,
        records=record_index,
        run_id=run_id,
        experiment_kind=experiment_kind,
        dataset_dir=dataset_dir,
        predictions_path=predictions_path,
        paths=paths,
    )
    paths.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    paths.report_md.write_text(_render_markdown_report(report), encoding="utf-8")
    log.info(
        "evaluation_done",
        status=report["status"],
        predictions=len(predictions),
        rows=len(rows),
        headline_ex=report["scores"].get("EX"),
        report_json=str(paths.report_json),
        report_md=str(paths.report_md),
    )
    return EvaluationOutput(status=str(report["status"]), report=report, paths=paths)


def _load_records(dataset_dir: Path) -> list[dict[str, Any]]:
    path = dataset_dir / "test.json"
    if not path.exists():
        raise TendError(
            "dataset test.json not found",
            anomaly=Anomaly.SUPPLY_EXHAUSTED,
            context={"path": str(path)},
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TendError(
            "dataset test.json must be a list",
            anomaly=Anomaly.INTERNAL,
            context={"path": str(path), "got_type": type(raw).__name__},
        )
    return [record for record in raw if isinstance(record, dict)]


def _load_predictions(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise TendError(
            "prediction file not found",
            anomaly=Anomaly.SUPPLY_EXHAUSTED,
            context={"path": str(path)},
        )
    out: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TendError(
                "prediction JSONL contains an invalid JSON line",
                anomaly=Anomaly.PARSE_ERROR,
                context={"path": str(path), "line_no": line_no, "error": str(exc)},
            ) from exc
        if isinstance(item, dict):
            item["_prediction_line"] = line_no
            out.append(item)
    return out


def _executor_available(executor: EvaluationExecutor) -> bool:
    available = getattr(executor, "available", None)
    if callable(available):
        try:
            return bool(available())
        except Exception:
            return False
    return True


def _load_witnesses(
    dataset_dir: Path,
    records: list[dict[str, Any]],
    executor: EvaluationExecutor,
    log: RunLogger,
) -> None:
    db_ids = sorted({str(record.get("db_id") or "") for record in records if record.get("db_id")})
    for db_id in db_ids:
        data_path = dataset_dir / "mongodb_data" / f"{db_id}.json"
        if not data_path.exists():
            raise TendError(
                "mongodb witness data not found",
                anomaly=Anomaly.EXEC_ERROR,
                context={"db_id": db_id, "path": str(data_path)},
            )
        data = json.loads(data_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TendError(
                "mongodb witness data must be a collection mapping",
                anomaly=Anomaly.EXEC_ERROR,
                context={"db_id": db_id, "path": str(data_path), "got_type": type(data).__name__},
            )
        executor.load_witness(db_id, data)
    log.info("evaluation_witnesses_loaded", db_ids=db_ids, db_count=len(db_ids))


def _prepare_gold_records(
    records: list[dict[str, Any]],
    executor: EvaluationExecutor,
    log: RunLogger,
) -> dict[tuple[str, Any], _GoldRecord]:
    gold: dict[tuple[str, Any], _GoldRecord] = {}
    for record in records:
        db_id = str(record.get("db_id") or "")
        record_id = record.get("record_id")
        mql = str(record.get("MQL") or "")
        cfs = record.get("canonical_form_set") if isinstance(record.get("canonical_form_set"), dict) else {}
        try:
            parsed = parse_pipeline(mql)
            disabled = scan_disabled(mql)
        except Exception as exc:  # noqa: BLE001 - release defects must be surfaced as eval faults
            raise TendError(
                "gold MQL is not parseable",
                anomaly=Anomaly.PARSE_ERROR,
                context={"db_id": db_id, "record_id": record_id, "message": str(exc)[:500]},
            ) from exc
        if disabled:
            raise TendError(
                "gold MQL contains disabled operators",
                anomaly=Anomaly.DISABLED_OPERATOR,
                context={"db_id": db_id, "record_id": record_id, "hits": disabled},
            )
        try:
            result = executor.norm_exec(db_id, mql)
        except Exception as exc:  # noqa: BLE001 - executor wraps most failures as TendError
            err = wrap_unexpected(
                exc,
                stage="gold_norm_exec",
                db_id=db_id,
                record_id=record_id,
                traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            )
            err.anomaly = Anomaly.EXEC_ERROR
            log.anomaly(err)
            raise err from exc
        gold[_record_key(record)] = _GoldRecord(
            record=record,
            gold_mql=mql,
            canonical_form_set=cfs,
            parsed=parsed,
            canonical_text=_canonical_text(parsed),
            structural_signature=_structural_signature(parsed),
            field_paths=sorted(_field_paths(parsed)),
            order_sensitive=_order_sensitive(parsed),
            gold_result=result,
        )
    return gold


def _score_predictions_concurrently(
    predictions: list[dict[str, Any]],
    *,
    record_index: dict[tuple[str, Any], dict[str, Any]],
    gold: dict[tuple[str, Any], _GoldRecord],
    executor: EvaluationExecutor,
    experiment_kind: str,
    run_id: str,
    logger: RunLogger,
    progress: Any,
    max_workers: int,
) -> list[dict[str, Any]]:
    workers = max(1, max_workers)
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tend-eval") as pool:
        futures = {
            pool.submit(
                _score_one_prediction,
                prediction,
                prediction_index=index,
                record_index=record_index,
                gold=gold,
                executor=executor,
                experiment_kind=experiment_kind,
                run_id=run_id,
                logger=logger,
                progress=progress,
            ): index
            for index, prediction in enumerate(predictions)
        }
        for future in as_completed(futures):
            rows.append(future.result())
    return rows


def _score_one_prediction(
    prediction: dict[str, Any],
    *,
    prediction_index: int,
    record_index: dict[tuple[str, Any], dict[str, Any]],
    gold: dict[tuple[str, Any], _GoldRecord],
    executor: EvaluationExecutor,
    experiment_kind: str,
    run_id: str,
    logger: RunLogger,
    progress: Any,
) -> dict[str, Any]:
    db_id = str(prediction.get("db_id") or "")
    record_id = prediction.get("record_id")
    system_id = _system_id(prediction, experiment_kind)
    task_id = f"eval:{experiment_kind}:{prediction_index}:{system_id}:{db_id}:{record_id}"
    group = f"eval:{experiment_kind}"
    if progress:
        progress.start_task(task_id, f"{system_id} {db_id}/{record_id}", group=group)
    log = logger.bind(
        system_id=system_id,
        db_id=db_id,
        record_id=record_id,
        prediction_index=prediction_index,
    )
    log.info("evaluation_record_start")
    try:
        row = _score_one_prediction_inner(
            prediction,
            prediction_index=prediction_index,
            record_index=record_index,
            gold=gold,
            executor=executor,
            experiment_kind=experiment_kind,
            run_id=run_id,
            system_id=system_id,
        )
        if progress:
            metrics = row.get("metrics", {})
            progress.finish_task(
                task_id,
                ok=True,
                detail=f"EX={metrics.get('EX', 0)} QIM={metrics.get('QIM', 0)}",
            )
        log.info(
            "evaluation_record_done",
            status=row.get("status"),
            metrics=row.get("metrics"),
            diagnostics=row.get("diagnostics"),
        )
        return row
    except Exception as exc:  # noqa: BLE001 - one evaluator bug should not drop all rows
        err = wrap_unexpected(
            exc,
            stage="evaluation_record",
            system_id=system_id,
            db_id=db_id,
            record_id=record_id,
            prediction_index=prediction_index,
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        logger.anomaly(err)
        if progress:
            progress.finish_task(task_id, ok=False, anomaly=Anomaly.INTERNAL.value)
        return _failed_record_row(
            prediction,
            run_id=run_id,
            experiment_kind=experiment_kind,
            system_id=system_id,
            prediction_index=prediction_index,
            error_code=Anomaly.INTERNAL.value,
            message=err.message,
        )


def _score_one_prediction_inner(
    prediction: dict[str, Any],
    *,
    prediction_index: int,
    record_index: dict[tuple[str, Any], dict[str, Any]],
    gold: dict[tuple[str, Any], _GoldRecord],
    executor: EvaluationExecutor,
    experiment_kind: str,
    run_id: str,
    system_id: str,
) -> dict[str, Any]:
    key = (str(prediction.get("db_id") or ""), prediction.get("record_id"))
    record = record_index.get(key)
    if record is None or key not in gold:
        return _failed_record_row(
            prediction,
            run_id=run_id,
            experiment_kind=experiment_kind,
            system_id=system_id,
            prediction_index=prediction_index,
            error_code="record_not_found",
            message="prediction does not match a release record",
        )
    gold_record = gold[key]
    mql = str(prediction.get("MQL") or "")
    diagnostics: dict[str, Any] = {}
    metrics = dict.fromkeys(EVALUATION_METRICS, 0)

    parsed: tuple[str, list[dict[str, Any]]] | None = None
    parse_error: str | None = None
    disabled: list[str] = []
    try:
        parsed = parse_pipeline(mql)
        disabled = scan_disabled(mql)
    except Exception as exc:  # noqa: BLE001 - scoring diagnostics should keep going
        parse_error = str(exc)

    if parse_error:
        diagnostics["parse_error"] = parse_error[:500]
    if disabled:
        diagnostics["forbidden_op_hit"] = disabled

    ast_ok = False
    ast_reasons: list[str] = []
    if parsed is not None:
        ast_ok, ast_reasons = ast_check(mql, gold_record.canonical_form_set)
    if ast_reasons:
        diagnostics["ast_reasons"] = ast_reasons

    metrics["QIM"] = int(parsed is not None and ast_ok)
    if parsed is not None:
        canonical_text = _canonical_text(parsed)
        metrics["EM"] = int(canonical_text == gold_record.canonical_text)
        metrics["QSM"] = int(_structural_signature(parsed) == gold_record.structural_signature)
        metrics["QFC"] = int(sorted(_field_paths(parsed)) == gold_record.field_paths)
        diagnostics["field_paths"] = {
            "predicted": sorted(_field_paths(parsed)),
            "gold": gold_record.field_paths,
        }

    predicted_result: list[dict[str, Any]] | None = None
    if parsed is not None and not disabled:
        try:
            predicted_result = executor.norm_exec(str(record.get("db_id") or ""), mql)
        except Exception as exc:  # noqa: BLE001 - bad predictions score 0 but remain scorable
            diagnostics["exec_error"] = str(exc)[:700]

    gold_result = gold_record.gold_result
    if predicted_result is not None and gold_result is not None:
        order_sensitive = gold_record.order_sensitive
        metrics["EX"] = int(
            ast_ok and equiv_rec(predicted_result, gold_result, order_sensitive=order_sensitive)
        )
        aligned_pred, aligned_gold = _align_results(
            predicted_result,
            gold_result,
            order_sensitive=order_sensitive,
        )
        metrics["EFM"] = int(_field_match(aligned_pred, aligned_gold))
        metrics["EVM"] = int(metrics["EFM"] and _value_match(aligned_pred, aligned_gold))
        diagnostics["result_rows"] = {
            "predicted": len(predicted_result),
            "gold": len(gold_result),
            "order_sensitive": order_sensitive,
        }
        diagnostics["result_hash"] = {
            "predicted": _hash_result(predicted_result),
            "gold": _hash_result(gold_result),
        }

    fingerprint = [metrics[name] for name in EVALUATION_METRICS]
    return {
        "result_type": "evaluation_record",
        "status": "scored",
        "run_id": run_id,
        "experiment_kind": experiment_kind,
        "system_id": system_id,
        "prediction_index": prediction_index,
        "prediction_line": prediction.get("_prediction_line"),
        "record_id": record.get("record_id"),
        "db_id": record.get("db_id"),
        "metrics": metrics,
        "fingerprint": fingerprint,
        "fingerprint_order": list(EVALUATION_METRICS),
        "diagnostics": diagnostics,
        "slice_keys": _slice_keys(record, gold_record.parsed),
        "prediction_ref": _prediction_ref(prediction),
    }


def _failed_record_row(
    prediction: dict[str, Any],
    *,
    run_id: str,
    experiment_kind: str,
    system_id: str,
    prediction_index: int,
    error_code: str,
    message: str,
) -> dict[str, Any]:
    metrics = dict.fromkeys(EVALUATION_METRICS, 0)
    return {
        "result_type": "evaluation_record",
        "status": "failed",
        "run_id": run_id,
        "experiment_kind": experiment_kind,
        "system_id": system_id,
        "prediction_index": prediction_index,
        "prediction_line": prediction.get("_prediction_line"),
        "record_id": prediction.get("record_id"),
        "db_id": prediction.get("db_id"),
        "metrics": metrics,
        "fingerprint": [0 for _ in EVALUATION_METRICS],
        "fingerprint_order": list(EVALUATION_METRICS),
        "diagnostics": {"error_code": error_code, "message": message},
        "slice_keys": {},
        "prediction_ref": _prediction_ref(prediction),
    }


def _record_key(record: dict[str, Any]) -> tuple[str, Any]:
    return (str(record.get("db_id") or ""), record.get("record_id"))


def _system_id(prediction: dict[str, Any], experiment_kind: str) -> str:
    for key in ("ablation_id", "baseline_id", "solver_variant", "system_id"):
        value = prediction.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return {
        "solver": "smart_solver",
        "baseline": "baseline",
        "ablation": "ablation",
    }.get(experiment_kind, experiment_kind)


def _prediction_ref(prediction: dict[str, Any]) -> dict[str, Any]:
    return {
        "line": prediction.get("_prediction_line"),
        "work_item_id": prediction.get("work_item_id"),
        "batch_index": prediction.get("batch_index"),
        "result_type": prediction.get("result_type"),
    }


def _canonical_text(parsed: tuple[str, list[dict[str, Any]]] | None) -> str:
    if parsed is None:
        return ""
    collection, pipeline = parsed
    return canonical_json({"collection": collection, "pipeline": pipeline})


def _structural_signature(parsed: tuple[str, list[dict[str, Any]]] | None) -> Any:
    if parsed is None:
        return None
    collection, pipeline = parsed
    return ("aggregate", "<collection>", _mask_structure(pipeline), bool(collection))


def _mask_structure(value: Any) -> Any:
    if isinstance(value, dict):
        items = []
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            key_text = str(key)
            masked_key = key_text if key_text.startswith("$") else "<field>"
            items.append((masked_key, _mask_structure(child)))
        return ("dict", tuple(items))
    if isinstance(value, list):
        return ("list", tuple(_mask_structure(item) for item in value))
    if isinstance(value, str):
        if value.startswith("$$"):
            return "<var>"
        if value.startswith("$"):
            return "<field-ref>"
        return "<literal>"
    if value is None or isinstance(value, (bool, int, float)):
        return "<literal>"
    return f"<{type(value).__name__}>"


def _field_paths(parsed: tuple[str, list[dict[str, Any]]] | None) -> set[str]:
    if parsed is None:
        return set()
    _, pipeline = parsed
    fields: set[str] = set()
    _collect_fields(pipeline, fields, parent_key=None)
    return fields


def _collect_fields(value: Any, fields: set[str], *, parent_key: str | None) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if not key_text.startswith("$") and parent_key not in {"let"}:
                fields.add(_normalize_field_path(key_text))
            if key_text in FIELD_VALUE_KEYS and isinstance(child, str):
                fields.add(_normalize_field_path(child))
            elif key_text not in NON_FIELD_VALUE_KEYS:
                _collect_fields(child, fields, parent_key=key_text)
    elif isinstance(value, list):
        for item in value:
            _collect_fields(item, fields, parent_key=parent_key)
    elif isinstance(value, str):
        if value.startswith("$") and not value.startswith("$$"):
            fields.add(_normalize_field_path(value))


def _normalize_field_path(value: str) -> str:
    text = value.strip()
    while text.startswith("$"):
        text = text[1:]
    return text


def _order_sensitive(parsed: tuple[str, list[dict[str, Any]]] | None) -> bool:
    if parsed is None:
        return False
    _, pipeline = parsed
    return bool(root_ops(pipeline) & ORDER_SENSITIVE_ROOT_OPS)


def _align_results(
    predicted: list[dict[str, Any]],
    gold: list[dict[str, Any]],
    *,
    order_sensitive: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if order_sensitive:
        return predicted, gold
    return sorted(predicted, key=_result_key), sorted(gold, key=_result_key)


def _result_key(value: Any) -> str:
    return canonical_json(value)


def _field_match(predicted: list[dict[str, Any]], gold: list[dict[str, Any]]) -> bool:
    if len(predicted) != len(gold):
        return False
    for left, right in zip(predicted, gold):
        if not isinstance(left, dict) or not isinstance(right, dict):
            if type(left) is not type(right):
                return False
            continue
        if set(left.keys()) != set(right.keys()):
            return False
    return True


def _value_match(predicted: list[dict[str, Any]], gold: list[dict[str, Any]]) -> bool:
    keys: set[str] = set()
    for row in [*predicted, *gold]:
        if isinstance(row, dict):
            keys.update(str(key) for key in row)
    for key in keys:
        predicted_values = Counter(
            _value_key(row.get(key)) for row in predicted if isinstance(row, dict)
        )
        gold_values = Counter(_value_key(row.get(key)) for row in gold if isinstance(row, dict))
        if predicted_values != gold_values:
            return False
    return True


def _value_key(value: Any) -> str:
    return canonical_json(value)


def _hash_result(value: Any) -> str:
    data = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _slice_keys(
    record: dict[str, Any],
    parsed: tuple[str, list[dict[str, Any]]] | None,
) -> dict[str, str]:
    return {
        "domain": str(record.get("domain_id") or record.get("db_id") or "unknown"),
        "join_depth": _join_depth(record, parsed),
        "aggregation_depth": _aggregation_depth(record, parsed),
        "schema_pattern": str(
            record.get("schema_pattern")
            or record.get("mechanism")
            or record.get("sql_infeasibility_class")
            or "unknown"
        ),
        "schema_flex": str(record.get("schema_flex") or "none"),
        "difficulty_tier": str(record.get("difficulty_tier") or record.get("difficulty") or "unknown"),
        "functional_sql_solvable": str(record.get("functional_sql_solvable", "unknown")),
        "structural_sql_solvable": str(record.get("structural_sql_solvable", "unknown")),
        "sql_infeasibility_class": str(record.get("sql_infeasibility_class") or "unknown"),
    }


def _join_depth(record: dict[str, Any], parsed: tuple[str, list[dict[str, Any]]] | None) -> str:
    raw = record.get("join_depth")
    if raw is not None:
        try:
            n = int(raw)
            return "3+" if n >= 3 else str(n)
        except (TypeError, ValueError):
            return str(raw)
    if parsed is None:
        return "unknown"
    _, pipeline = parsed
    n = 0
    for stage in pipeline:
        if isinstance(stage, dict):
            n += sum(1 for op in stage if op in {"$lookup", "$graphLookup", "$unionWith"})
    return "3+" if n >= 3 else str(n)


def _aggregation_depth(
    record: dict[str, Any],
    parsed: tuple[str, list[dict[str, Any]]] | None,
) -> str:
    raw = record.get("aggregation_depth")
    if raw is not None:
        return str(raw)
    if parsed is None:
        return "unknown"
    _, pipeline = parsed
    root = root_ops(pipeline)
    agg_ops = root & {"$group", "$bucket", "$bucketAuto", "$facet", "$setWindowFields"}
    if not agg_ops:
        return "shallow"
    if len(pipeline) <= 4:
        return "medium"
    return "deep"


def _write_per_record(paths: EvaluationPaths, rows: list[dict[str, Any]]) -> None:
    with paths.per_record_jsonl.open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    columns = [
        "run_id",
        "experiment_kind",
        "system_id",
        "db_id",
        "record_id",
        "prediction_index",
        "status",
        *EVALUATION_METRICS,
        "fingerprint",
        "error_code",
    ]
    with paths.per_record_csv.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            metrics = row.get("metrics") or {}
            diagnostics = row.get("diagnostics") or {}
            writer.writerow({
                "run_id": row.get("run_id"),
                "experiment_kind": row.get("experiment_kind"),
                "system_id": row.get("system_id"),
                "db_id": row.get("db_id"),
                "record_id": row.get("record_id"),
                "prediction_index": row.get("prediction_index"),
                "status": row.get("status"),
                **{name: metrics.get(name, 0) for name in EVALUATION_METRICS},
                "fingerprint": "".join(str(metrics.get(name, 0)) for name in EVALUATION_METRICS),
                "error_code": diagnostics.get("error_code")
                or _diagnostic_error_code(diagnostics),
            })


def _diagnostic_error_code(diagnostics: dict[str, Any]) -> str:
    for key in ("parse_error", "forbidden_op_hit", "exec_error", "ast_reasons"):
        if diagnostics.get(key):
            return key
    return ""


def _build_report(
    rows: list[dict[str, Any]],
    *,
    records: dict[tuple[str, Any], dict[str, Any]],
    run_id: str,
    experiment_kind: str,
    dataset_dir: Path,
    predictions_path: Path,
    paths: EvaluationPaths,
) -> dict[str, Any]:
    scores = _aggregate(rows)
    by_system: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_system[str(row.get("system_id"))].append(row)

    diagnostics = _diagnostic_counts(rows)
    status = "ok"
    if any(row.get("status") == "failed" for row in rows):
        status = "partial"

    return {
        "result_type": "evaluation_report",
        "status": status,
        "run_id": run_id,
        "experiment_kind": experiment_kind,
        "headline_metric": "EX",
        "metrics_order": list(EVALUATION_METRICS),
        "record_count": len(rows),
        "release_record_count": len(records),
        "scores": scores,
        "systems": {
            system_id: {
                "record_count": len(items),
                "scores": _aggregate(items),
                "diagnostics": _diagnostic_counts(items),
            }
            for system_id, items in sorted(by_system.items())
        },
        "slice_aggregates": _aggregate_slices(rows, SLICE_AXES),
        "diagnostic_slice_aggregates": _aggregate_slices(rows, DIAGNOSTIC_SLICE_AXES),
        "diagnostics": diagnostics,
        "disclosure": {
            "proposal": "05_evaluation_methodology",
            "disclosure_status": "analysis_report_not_official_leaderboard_submission",
            "per_record_fingerprint_csv": str(paths.per_record_csv),
            "per_record_fingerprint_jsonl": str(paths.per_record_jsonl),
            "environment_digest": _environment_digest(),
        },
        "artifacts": paths.as_dict(),
        "inputs": {
            "dataset_dir": str(dataset_dir),
            "predictions_path": str(predictions_path),
        },
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {name: 0.0 for name in EVALUATION_METRICS}
    return {
        name: round(
            sum(float((row.get("metrics") or {}).get(name, 0)) for row in rows) / len(rows),
            6,
        )
        for name in EVALUATION_METRICS
    }


def _aggregate_slices(rows: list[dict[str, Any]], axes: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for axis in axes:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            slice_keys = row.get("slice_keys") if isinstance(row.get("slice_keys"), dict) else {}
            value = str(slice_keys.get(axis, "unknown"))
            buckets[value].append(row)
        out[axis] = {
            value: {
                "record_count": len(items),
                "scores": _aggregate(items),
            }
            for value, items in sorted(buckets.items())
        }
    return out


def _diagnostic_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        diagnostics = row.get("diagnostics") if isinstance(row.get("diagnostics"), dict) else {}
        if row.get("status") == "failed":
            counts["record_failed"] += 1
        for key in ("parse_error", "forbidden_op_hit", "exec_error", "ast_reasons"):
            if diagnostics.get(key):
                counts[key] += 1
    return dict(sorted(counts.items()))


def _environment_digest() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
    }


def _write_failed_report(
    paths: EvaluationPaths,
    *,
    run_id: str,
    experiment_kind: str,
    message: str,
    error_code: str,
    logger: RunLogger,
) -> EvaluationOutput:
    report = {
        "result_type": "evaluation_report",
        "status": "failed",
        "run_id": run_id,
        "experiment_kind": experiment_kind,
        "headline_metric": "EX",
        "metrics_order": list(EVALUATION_METRICS),
        "record_count": 0,
        "scores": {name: 0.0 for name in EVALUATION_METRICS},
        "systems": {},
        "slice_aggregates": {},
        "diagnostic_slice_aggregates": {},
        "diagnostics": {"error_code": error_code, "message": message},
        "artifacts": paths.as_dict(),
    }
    paths.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    paths.report_md.write_text(_render_markdown_report(report), encoding="utf-8")
    # Keep empty per-record artifacts present so automation can rely on paths.
    _write_per_record(paths, [])
    logger.error(
        "evaluation_done",
        status="failed",
        error_code=error_code,
        message=message,
        report_json=str(paths.report_json),
    )
    return EvaluationOutput(status="failed", report=report, paths=paths)


def _render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# TEND Evaluation Report: {report.get('experiment_kind')}",
        "",
        f"- run_id: `{report.get('run_id')}`",
        f"- status: `{report.get('status')}`",
        f"- headline: `EX = {report.get('scores', {}).get('EX', 0.0)}`",
        f"- records: `{report.get('record_count', 0)}`",
        "",
        "## Scores",
        "",
        "| Metric | Mean |",
        "|--------|------|",
    ]
    for metric in EVALUATION_METRICS:
        lines.append(f"| {metric} | {report.get('scores', {}).get(metric, 0.0)} |")
    lines += ["", "## Systems", "", "| System | Records | EX | QIM | EFM | EVM |", "|--------|---------|----|-----|-----|-----|"]
    systems = report.get("systems") if isinstance(report.get("systems"), dict) else {}
    if systems:
        for system_id, payload in systems.items():
            scores = payload.get("scores", {})
            lines.append(
                f"| {system_id} | {payload.get('record_count', 0)} | "
                f"{scores.get('EX', 0.0)} | {scores.get('QIM', 0.0)} | "
                f"{scores.get('EFM', 0.0)} | {scores.get('EVM', 0.0)} |"
            )
    else:
        lines.append("| (none) | 0 | 0 | 0 | 0 | 0 |")
    diagnostics = report.get("diagnostics") if isinstance(report.get("diagnostics"), dict) else {}
    lines += ["", "## Diagnostics", ""]
    if diagnostics:
        lines += ["| Key | Count / Value |", "|-----|---------------|"]
        for key, value in diagnostics.items():
            lines.append(f"| {key} | {value} |")
    else:
        lines.append("No diagnostic errors were recorded.")
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    lines += ["", "## Artifacts", ""]
    for key, value in artifacts.items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    return "\n".join(lines)
