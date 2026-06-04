from __future__ import annotations

import json
from pathlib import Path

import pytest

import tend.cli as cli
import tend.config as config_module
import tend.evaluation.metrics as metrics_module
from tend.evaluation import EVALUATION_METRICS, evaluate_predictions
from tend.execution.ast_check import parse_pipeline, root_ops
from tend.observability import ProgressReporter, setup_logging


class FakeEvaluationExecutor:
    def __init__(self, *_args, **_kwargs) -> None:
        self.loaded: set[str] = set()

    def available(self) -> bool:
        return True

    def load_witness(self, db_id: str, collections: dict) -> None:
        assert collections
        self.loaded.add(db_id)

    def norm_exec(self, db_id: str, mql: str) -> list[dict]:
        assert db_id == "financial"
        _, pipeline = parse_pipeline(mql)
        ops = root_ops(pipeline)
        if "$lookup" in ops:
            return [
                {"_id": 1, "loan_to_credit_ratio": 0},
                {"_id": 2, "loan_to_credit_ratio": 10},
            ]
        if "$addFields" in ops:
            return [{"_id": 1, "loan_amount": 0}, {"_id": 2, "loan_amount": 100}]
        return [{"_id": 1}]

    def close(self) -> None:
        pass


def test_evaluate_predictions_writes_proposal_05_artifacts(tmp_path: Path) -> None:
    dataset_dir = Path("tests/fixtures/smoke_release")
    record = json.loads((dataset_dir / "test.json").read_text(encoding="utf-8"))[0]
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        "\n".join([
            json.dumps({
                "result_type": "solver_prediction",
                "solver_variant": "exact_gold",
                "record_id": record["record_id"],
                "db_id": record["db_id"],
                "MQL": record["MQL"],
            }),
            json.dumps({
                "result_type": "solver_prediction",
                "solver_variant": "empty_pipeline",
                "record_id": record["record_id"],
                "db_id": record["db_id"],
                "MQL": "db.account.aggregate([])",
            }),
        ]),
        encoding="utf-8",
    )
    log = setup_logging(tmp_path / "run", console=False)
    progress = ProgressReporter("eval-test", log, enabled=False)
    try:
        output = evaluate_predictions(
            dataset_dir=dataset_dir,
            predictions_path=predictions,
            out_dir=tmp_path / "eval",
            experiment_kind="solver",
            run_id="eval-test",
            logger=log,
            progress=progress,
            executor=FakeEvaluationExecutor(),
            max_workers=2,
        )
    finally:
        log.close()

    assert output.ok
    assert output.paths.per_record_jsonl.exists()
    assert output.paths.per_record_csv.exists()
    assert output.paths.report_json.exists()
    assert output.paths.report_md.exists()
    assert output.report["metrics_order"] == list(EVALUATION_METRICS)
    assert output.report["headline_metric"] == "EX"
    assert output.report["metrics_order"] == ["EM", "QSM", "QFC", "EX", "EFM", "EVM"]
    assert output.report["diagnostic_metrics_order"] == ["EM", "QSM", "QFC", "EFM", "EVM"]
    assert output.report["scores"]["EX"] == 0.5
    assert "QIM" not in output.report["scores"]
    assert output.report["systems"]["exact_gold"]["scores"]["EX"] == 1.0
    assert "QIM" not in output.report["systems"]["empty_pipeline"]["scores"]
    assert output.report["slice_aggregates"]["domain"]["financial"]["record_count"] == 2
    report_md = output.paths.report_md.read_text(encoding="utf-8")
    assert "## Diagnostic Metrics" in report_md
    assert "| QIM |" not in report_md
    assert "| EX |" not in report_md.split("## Diagnostic Metrics", maxsplit=1)[1].split("## Systems", maxsplit=1)[0]

    rows = [
        json.loads(line)
        for line in output.paths.per_record_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    exact = next(row for row in rows if row["system_id"] == "exact_gold")
    assert exact["fingerprint"] == [1, 1, 1, 1, 1, 1]
    assert exact["fingerprint_order"] == ["EM", "QSM", "QFC", "EX", "EFM", "EVM"]
    assert "QIM" not in exact["metrics"]
    assert exact["diagnostics"]["parse_ok"] is True
    assert exact["diagnostics"]["ast_ok"] is True
    empty = next(row for row in rows if row["system_id"] == "empty_pipeline")
    assert empty["metrics"]["EX"] == 0
    assert empty["diagnostics"]["parse_ok"] is True
    assert empty["diagnostics"]["ast_ok"] is False
    assert empty["diagnostics"]["ast_reasons"]

    csv_header = output.paths.per_record_csv.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert "QIM" not in csv_header


class _SelectiveGoldExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.loaded: set[str] = set()

    def available(self) -> bool:
        return True

    def load_witness(self, db_id: str, collections: dict) -> None:
        assert collections
        self.loaded.add(db_id)

    def norm_exec(self, db_id: str, mql: str) -> list[dict]:
        self.calls.append(mql)
        return [{"_id": 1}]


def test_evaluate_predictions_prepares_only_predicted_gold_records(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "release"
    (dataset_dir / "mongodb_data").mkdir(parents=True)
    record_one = {
        "record_id": 1,
        "db_id": "financial",
        "MQL": "db.gold_one.aggregate([])",
        "canonical_form_set": {},
    }
    record_two = {
        "record_id": 2,
        "db_id": "financial",
        "MQL": "db.gold_two.aggregate([])",
        "canonical_form_set": {},
    }
    (dataset_dir / "test.json").write_text(
        json.dumps([record_one, record_two]),
        encoding="utf-8",
    )
    (dataset_dir / "mongodb_data" / "financial.json").write_text(
        json.dumps({"gold_one": [{"_id": 1}], "gold_two": [{"_id": 2}]}),
        encoding="utf-8",
    )
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps({
            "solver_variant": "only_one",
            "record_id": 1,
            "db_id": "financial",
            "MQL": record_one["MQL"],
        }) + "\n",
        encoding="utf-8",
    )
    executor = _SelectiveGoldExecutor()
    log = setup_logging(tmp_path / "run", console=False)
    try:
        output = evaluate_predictions(
            dataset_dir=dataset_dir,
            predictions_path=predictions,
            out_dir=tmp_path / "eval",
            experiment_kind="solver",
            run_id="eval-selective-gold",
            logger=log,
            progress=None,
            executor=executor,
            max_workers=1,
        )
    finally:
        log.close()

    assert output.status == "partial"
    assert output.report["release_record_count"] == 2
    assert output.report["record_count"] == 2
    assert output.report["scored_row_count"] == 2
    assert output.report["denominator"] == {
        "scope": "dataset_records",
        "release_record_count": 2,
        "scored_row_count": 2,
        "system_count": 1,
        "selection": None,
    }
    assert output.report["scores"]["EX"] == 0.5
    assert output.report["systems"]["only_one"]["record_count"] == 2
    assert output.report["systems"]["only_one"]["scores"]["EX"] == 0.5
    assert output.report["diagnostics"]["missing_prediction"] == 1
    assert len(executor.calls) == 2
    assert all("gold_one" in call for call in executor.calls)
    assert all("gold_two" not in call for call in executor.calls)
    rows = [
        json.loads(line)
        for line in output.paths.per_record_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    missing = next(row for row in rows if row["record_id"] == 2)
    assert missing["status"] == "failed"
    assert missing["diagnostics"]["error_code"] == "missing_prediction"
    assert missing["metrics"] == dict.fromkeys(EVALUATION_METRICS, 0)
    events = [
        json.loads(line)
        for line in (tmp_path / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    missing_event = next(event for event in events if event["event"] == "evaluation_missing_predictions")
    assert missing_event["missing_records_sample"] == [
        {"system_id": "only_one", "db_id": "financial", "record_id": 2}
    ]
    assert missing_event["per_record_jsonl"] == str(output.paths.per_record_jsonl)
    assert missing_event["report_json"] == str(output.paths.report_json)


@pytest.mark.parametrize(
    ("experiment_kind", "result_type", "system_key", "system_id"),
    [
        ("solver", "solver_failure", "solver_variant", "smart_solver"),
        ("baseline", "baseline_failure", "baseline_id", "direct"),
        ("ablation", "ablation_failure", "ablation_id", "smart_eg_no_evidence_gate"),
    ],
)
def test_evaluate_predictions_preserves_typed_failure_artifacts(
    tmp_path: Path,
    experiment_kind: str,
    result_type: str,
    system_key: str,
    system_id: str,
) -> None:
    dataset_dir = tmp_path / "release"
    (dataset_dir / "mongodb_data").mkdir(parents=True)
    record = {
        "record_id": 41,
        "db_id": "financial",
        "domain_id": "finance_domain",
        "difficulty_tier": "L4",
        "join_depth": 2,
        "aggregation_depth": "deep",
        "schema_pattern": "native_dynamic_keys",
        "schema_flex": "high",
        "functional_sql_solvable": False,
        "structural_sql_solvable": True,
        "sql_infeasibility_class": "native_shape",
        "MQL": "db.gold_one.aggregate([])",
        "canonical_form_set": {},
    }
    (dataset_dir / "test.json").write_text(json.dumps([record]), encoding="utf-8")
    (dataset_dir / "mongodb_data" / "financial.json").write_text(
        json.dumps({"gold_one": [{"_id": 1}]}),
        encoding="utf-8",
    )
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        "\n".join([
            json.dumps({
                "result_type": f"{experiment_kind}_prediction",
                system_key: "exact_gold",
                "record_id": record["record_id"],
                "db_id": record["db_id"],
                "MQL": record["MQL"],
                "batch_index": 0,
                "work_item_id": f"{experiment_kind}:0:financial:41",
            }),
            json.dumps({
                "result_type": result_type,
                system_key: system_id,
                "status": "failed",
                "record_id": record["record_id"],
                "db_id": record["db_id"],
                "error_code": "CONTRACT_VIOLATION",
                "message": "agent output failed semantic contract",
                "batch_index": 1,
                "work_item_id": f"{experiment_kind}:1:financial:41",
                "input_mode": "nlq_db",
                "nlq_track": "canonical",
                "nlq_hash": "sha256:failure",
                "witness_k": 3,
                "evaluation_skip_reason": "no_release_dataset",
                "transcript_refs": ["llm/failure.md"],
                "diagnostics_refs": ["llm/failure.diagnostics.json"],
                "agent_session_ref": "agent_sessions/failure.json",
                "evidence_ledger_ref": "evidence/failure-ledger.json",
                "execution_trace_ref": "traces/failure-execution.json",
                "last_candidate_ref": "candidates/failure-last.json",
                "unresolved_debts": ["target_fields_missing", "counterexample_unresolved"],
            }),
        ]) + "\n",
        encoding="utf-8",
    )

    log = setup_logging(tmp_path / "run", console=False)
    try:
        output = evaluate_predictions(
            dataset_dir=dataset_dir,
            predictions_path=predictions,
            out_dir=tmp_path / "eval",
            experiment_kind=experiment_kind,
            run_id=f"eval-{experiment_kind}-failure",
            logger=log,
            progress=None,
            executor=_SelectiveGoldExecutor(),
            max_workers=1,
        )
    finally:
        log.close()

    assert output.status == "partial"
    assert output.report["scores"]["EX"] == 0.5
    assert output.report["release_record_count"] == 1
    assert output.report["record_count"] == 2
    assert output.report["scored_row_count"] == 2
    assert output.report["denominator"] == {
        "scope": "dataset_records",
        "release_record_count": 1,
        "scored_row_count": 2,
        "system_count": 2,
        "selection": None,
    }
    assert output.report["diagnostics"]["record_failed"] == 1
    assert output.report["diagnostics"][result_type] == 1
    assert output.report["diagnostics"]["CONTRACT_VIOLATION"] == 1
    rows = [
        json.loads(line)
        for line in output.paths.per_record_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    failure = next(row for row in rows if row["prediction_ref"]["result_type"] == result_type)
    assert failure["status"] == "failed"
    assert failure["metrics"] == dict.fromkeys(EVALUATION_METRICS, 0)
    assert failure["fingerprint"] == [0 for _ in EVALUATION_METRICS]
    assert failure["diagnostics"] == {
        "error_code": "CONTRACT_VIOLATION",
        "failure_type": result_type,
        "message": "agent output failed semantic contract",
    }
    assert failure["prediction_ref"] == {
        "line": 2,
        "work_item_id": f"{experiment_kind}:1:financial:41",
        "batch_index": 1,
        "result_type": result_type,
        "input_mode": "nlq_db",
        "nlq_track": "canonical",
        "nlq_hash": "sha256:failure",
        "witness_k": 3,
        "evaluation_skip_reason": "no_release_dataset",
        "transcript_refs": ["llm/failure.md"],
        "diagnostics_refs": ["llm/failure.diagnostics.json"],
        "agent_session_ref": "agent_sessions/failure.json",
        "evidence_ledger_ref": "evidence/failure-ledger.json",
        "execution_trace_ref": "traces/failure-execution.json",
        "last_candidate_ref": "candidates/failure-last.json",
        "unresolved_debts": ["target_fields_missing", "counterexample_unresolved"],
    }
    assert output.report["diagnostic_artifact_refs"]["count"] == 1
    assert output.report["diagnostic_artifact_refs"]["items"][0]["work_item_id"] == (
        f"{experiment_kind}:1:financial:41"
    )
    assert output.report["diagnostic_artifact_refs"]["items"][0]["diagnostics_refs"] == [
        "llm/failure.diagnostics.json"
    ]
    assert (
        output.report["systems"][system_id]["diagnostic_artifact_refs"]["items"][0][
            "last_candidate_ref"
        ]
        == "candidates/failure-last.json"
    )
    assert failure["slice_keys"] == {
        "domain": "finance_domain",
        "join_depth": "2",
        "aggregation_depth": "deep",
        "schema_pattern": "native_dynamic_keys",
        "schema_flex": "high",
        "difficulty_tier": "L4",
        "functional_sql_solvable": "False",
        "structural_sql_solvable": "True",
        "sql_infeasibility_class": "native_shape",
    }
    assert "parse_error" not in failure["diagnostics"]
    assert "parse_ok" not in failure["diagnostics"]


def test_evaluate_predictions_supports_release_package_layout(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "release" / "tend-native-mongodb-v1"
    (dataset_dir / "data").mkdir(parents=True)
    (dataset_dir / "mongodb_data").mkdir()
    record = {
        "record_id": 10,
        "db_id": "financial",
        "MQL": "db.gold_one.aggregate([])",
        "canonical_form_set": {},
    }
    (dataset_dir / "data" / "test.json").write_text(
        json.dumps([record]),
        encoding="utf-8",
    )
    (dataset_dir / "mongodb_data" / "financial.json").write_text(
        json.dumps({"gold_one": [{"_id": 1}]}),
        encoding="utf-8",
    )
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps({
            "solver_variant": "package_exact",
            "record_id": 10,
            "db_id": "financial",
            "MQL": record["MQL"],
        }) + "\n",
        encoding="utf-8",
    )
    log = setup_logging(tmp_path / "run", console=False)
    try:
        output = evaluate_predictions(
            dataset_dir=dataset_dir,
            predictions_path=predictions,
            out_dir=tmp_path / "eval",
            experiment_kind="solver",
            run_id="eval-package",
            logger=log,
            progress=None,
            executor=_SelectiveGoldExecutor(),
            max_workers=1,
        )
    finally:
        log.close()

    assert output.ok
    assert output.report["release_record_count"] == 1
    assert output.report["denominator"]["scope"] == "dataset_records"
    assert output.report["scores"]["EX"] == 1.0


def test_ablation_evaluation_headline_uses_per_system_deltas(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "release"
    (dataset_dir / "mongodb_data").mkdir(parents=True)
    record = {
        "record_id": 44,
        "db_id": "financial",
        "MQL": "db.gold_one.aggregate([])",
        "canonical_form_set": {},
    }
    (dataset_dir / "test.json").write_text(json.dumps([record]), encoding="utf-8")
    (dataset_dir / "mongodb_data" / "financial.json").write_text(
        json.dumps({"gold_one": [{"_id": 1}], "account": [{"_id": 2}]}),
        encoding="utf-8",
    )
    predictions = tmp_path / "ablation_predictions.jsonl"
    predictions.write_text(
        "\n".join([
            json.dumps({
                "result_type": "ablation_prediction",
                "ablation_id": "smart_eg_full",
                "record_id": record["record_id"],
                "db_id": record["db_id"],
                "MQL": record["MQL"],
            }),
            json.dumps({
                "result_type": "ablation_prediction",
                "ablation_id": "smart_eg_no_evidence_gate",
                "record_id": record["record_id"],
                "db_id": record["db_id"],
                "MQL": "db.account.aggregate([])",
            }),
        ]) + "\n",
        encoding="utf-8",
    )

    class _AblationHeadlineExecutor(_SelectiveGoldExecutor):
        def norm_exec(self, db_id: str, mql: str) -> list[dict]:
            self.calls.append(mql)
            if "gold_one" in mql:
                return [{"_id": 1}]
            return [{"_id": 2}]

    log = setup_logging(tmp_path / "run", console=False)
    try:
        output = evaluate_predictions(
            dataset_dir=dataset_dir,
            predictions_path=predictions,
            out_dir=tmp_path / "eval",
            experiment_kind="ablation",
            run_id="eval-ablation-headline",
            logger=log,
            progress=None,
            executor=_AblationHeadlineExecutor(),
            max_workers=1,
        )
    finally:
        log.close()

    assert output.report["headline_metric"] == "per_system_EX"
    headline = output.report["headline"]
    assert headline["mode"] == "per_system"
    assert headline["reference_system_id"] == "smart_eg_full"
    assert headline["mixed_overall_scores_are_diagnostic"] is True
    assert headline["overall_scores"]["EX"] == 0.5
    assert headline["systems"]["smart_eg_full"]["scores"]["EX"] == 1.0
    assert (
        headline["systems"]["smart_eg_no_evidence_gate"]["delta_vs_smart_eg_full"]["EX"]
        == -1.0
    )
    report_md = output.paths.report_md.read_text(encoding="utf-8")
    assert "per-system EX; mixed overall is diagnostic" in report_md
    assert "Mixed Overall Scores (Diagnostic)" in report_md
    assert "headline: `EX = 0.5`" not in report_md


def test_evaluate_predictions_normalizes_stale_preserve_cfs_for_gold_unwind(
    tmp_path: Path,
) -> None:
    dataset_dir = tmp_path / "release"
    (dataset_dir / "mongodb_data").mkdir(parents=True)
    gold_mql = (
        'db.party_relationship_graphs.aggregate(['
        '{"$project":{"native_dynamic_entries":{"$objectToArray":"$roles"}}},'
        '{"$unwind":"$native_dynamic_entries"},'
        '{"$project":{"native_key":"$native_dynamic_entries.k"}}'
        '])'
    )
    record = {
        "record_id": 590389,
        "db_id": "financial",
        "MQL": gold_mql,
        "canonical_form_set": {
            "must_contain": [],
            "must_not_contain": ["$sample", "$rand", "$out", "$merge", "$function", "$$NOW"],
            "must_contain_at_root": [],
            "must_not_contain_at_root": ["$group", "$unwind"],
        },
    }
    (dataset_dir / "test.json").write_text(json.dumps([record]), encoding="utf-8")
    (dataset_dir / "mongodb_data" / "financial.json").write_text(
        json.dumps({"party_relationship_graphs": [{"_id": 1}]}),
        encoding="utf-8",
    )
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps({
            "solver_variant": "exact_gold",
            "record_id": record["record_id"],
            "db_id": record["db_id"],
            "MQL": gold_mql,
        }) + "\n",
        encoding="utf-8",
    )
    log = setup_logging(tmp_path / "run", console=False)
    try:
        output = evaluate_predictions(
            dataset_dir=dataset_dir,
            predictions_path=predictions,
            out_dir=tmp_path / "eval",
            experiment_kind="solver",
            run_id="eval-stale-cfs",
            logger=log,
            progress=None,
            executor=_SelectiveGoldExecutor(),
            max_workers=1,
        )
    finally:
        log.close()

    row = json.loads(output.paths.per_record_jsonl.read_text(encoding="utf-8"))

    assert row["diagnostics"]["ast_ok"] is True
    assert row["metrics"]["EX"] == 1


class _OrderCaseExecutor:
    """Returns gold rows for the gold MQL and a row *permutation* for the prediction.

    The two MQL strings differ only in collection name so the executor can tell which
    one it is scoring; the gold pipeline carries (or omits) ``$sort`` to drive
    order-sensitivity.
    """

    def __init__(self, gold_rows: list[dict], predicted_rows: list[dict]) -> None:
        self._gold_rows = gold_rows
        self._predicted_rows = predicted_rows

    def available(self) -> bool:
        return True

    def load_witness(self, db_id: str, collections: dict) -> None:
        pass

    def norm_exec(self, db_id: str, mql: str) -> list[dict]:
        if "predcoll" in mql:
            return [dict(row) for row in self._predicted_rows]
        return [dict(row) for row in self._gold_rows]

    def close(self) -> None:
        pass


def _run_order_case(
    tmp_path: Path,
    *,
    gold_mql: str,
    predicted_mql: str,
    gold_rows: list[dict],
    predicted_rows: list[dict],
) -> dict:
    dataset_dir = tmp_path / "release"
    (dataset_dir / "mongodb_data").mkdir(parents=True)
    (dataset_dir / "test.json").write_text(
        json.dumps([
            {
                "record_id": 7,
                "db_id": "ordering",
                "MQL": gold_mql,
                "canonical_form_set": {},
            }
        ]),
        encoding="utf-8",
    )
    (dataset_dir / "mongodb_data" / "ordering.json").write_text(
        json.dumps({"goldcoll": [{"_id": 1}]}),
        encoding="utf-8",
    )
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps({
            "solver_variant": "permuted",
            "record_id": 7,
            "db_id": "ordering",
            "MQL": predicted_mql,
        }) + "\n",
        encoding="utf-8",
    )
    log = setup_logging(tmp_path / "run", console=False)
    progress = ProgressReporter("eval-order", log, enabled=False)
    try:
        output = evaluate_predictions(
            dataset_dir=dataset_dir,
            predictions_path=predictions,
            out_dir=tmp_path / "eval",
            experiment_kind="solver",
            run_id="eval-order-test",
            logger=log,
            progress=progress,
            executor=_OrderCaseExecutor(gold_rows, predicted_rows),
            max_workers=1,
        )
    finally:
        log.close()
    rows = [
        json.loads(line)
        for line in output.paths.per_record_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    return next(row for row in rows if row["system_id"] == "permuted")


def test_evm_zero_for_order_wrong_answer_to_order_sensitive_query(tmp_path: Path) -> None:
    # $sort makes the gold query order-sensitive; the prediction is a row permutation,
    # so it must earn neither EX nor EVM (EVM must not exceed EFM either).
    gold_rows = [{"_id": 1, "v": 10}, {"_id": 2, "v": 20}]
    predicted_rows = list(reversed(gold_rows))
    row = _run_order_case(
        tmp_path,
        gold_mql="db.goldcoll.aggregate([{ \"$sort\": { \"v\": 1 } }])",
        predicted_mql="db.predcoll.aggregate([{ \"$sort\": { \"v\": -1 } }])",
        gold_rows=gold_rows,
        predicted_rows=predicted_rows,
    )
    metrics = row["metrics"]
    assert row["diagnostics"]["result_rows"]["order_sensitive"] is True
    assert metrics["EX"] == 0
    assert metrics["EFM"] == 1
    assert metrics["EVM"] == 0


def test_evm_one_for_permutation_under_order_insensitive_query(tmp_path: Path) -> None:
    # No $sort: order is irrelevant, so a permutation is fully correct (EX and EVM = 1).
    gold_rows = [{"_id": 1, "v": 10}, {"_id": 2, "v": 20}]
    predicted_rows = list(reversed(gold_rows))
    row = _run_order_case(
        tmp_path,
        gold_mql="db.goldcoll.aggregate([{ \"$project\": { \"v\": 1 } }])",
        predicted_mql="db.predcoll.aggregate([{ \"$project\": { \"v\": 1 } }])",
        gold_rows=gold_rows,
        predicted_rows=predicted_rows,
    )
    metrics = row["metrics"]
    assert row["diagnostics"]["result_rows"]["order_sensitive"] is False
    assert metrics["EX"] == 1
    assert metrics["EFM"] == 1
    assert metrics["EVM"] == 1


def test_result_hash_accepts_bson_object_id_from_existing_mongo() -> None:
    from bson.objectid import ObjectId

    digest = metrics_module._hash_result(
        [{"_id": ObjectId("656565656565656565656565"), "value": 1}]
    )

    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_manual_evaluate_cli_does_not_require_bird_or_llm(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(config_module, "load_dotenv", lambda _path: {})
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "TEND_BIRD_ROOT", "TEND_LLM_STUB"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cli, "MongoExecutor", FakeEvaluationExecutor)

    dataset_dir = Path("tests/fixtures/smoke_release")
    record = json.loads((dataset_dir / "test.json").read_text(encoding="utf-8"))[0]
    predictions = tmp_path / "manual_predictions.jsonl"
    predictions.write_text(
        json.dumps({
            "solver_variant": "manual_exact",
            "record_id": record["record_id"],
            "db_id": record["db_id"],
            "MQL": record["MQL"],
        }) + "\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "manual_eval"

    rc = cli.main([
        "evaluate",
        "--dataset-dir",
        str(dataset_dir),
        "--predictions",
        str(predictions),
        "--kind",
        "solver",
        "--out",
        str(out_dir),
        "--quiet",
        "--run-id",
        "manual-eval-test",
    ])

    assert rc == 0
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert report["scores"]["EX"] == 1.0
