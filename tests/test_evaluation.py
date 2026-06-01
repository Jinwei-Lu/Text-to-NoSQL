from __future__ import annotations

import json
from pathlib import Path

import tend.cli as cli
import tend.config as config_module
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
    assert output.report["scores"]["EX"] == 0.5
    assert output.report["systems"]["exact_gold"]["scores"]["EX"] == 1.0
    assert output.report["systems"]["empty_pipeline"]["scores"]["QIM"] == 0.0
    assert output.report["slice_aggregates"]["domain"]["financial"]["record_count"] == 2

    rows = [
        json.loads(line)
        for line in output.paths.per_record_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    exact = next(row for row in rows if row["system_id"] == "exact_gold")
    assert exact["fingerprint"] == [1, 1, 1, 1, 1, 1, 1]
    empty = next(row for row in rows if row["system_id"] == "empty_pipeline")
    assert empty["metrics"]["EX"] == 0
    assert empty["diagnostics"]["ast_reasons"]


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
