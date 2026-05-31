from __future__ import annotations

import json

from tend.execution.ast_check import parse_pipeline
from tend.solver.per_stage import (
    CheckpointCode,
    CheckpointSpec,
    PrefixExecutionRequest,
    PrefixExecutionResult,
    VariantExecution,
    run_per_stage_check,
)


class FakePrefixExecutor:
    def __init__(self, responses: dict[int, PrefixExecutionResult | BaseException]) -> None:
        self.responses = responses
        self.requests: list[PrefixExecutionRequest] = []

    def execute_prefix(self, request: PrefixExecutionRequest) -> PrefixExecutionResult:
        self.requests.append(request)
        response = self.responses[request.stage_index]
        if isinstance(response, BaseException):
            raise response
        return response


def _variant(
    name: str,
    docs: list[dict],
    *,
    input_count: int | None = 1,
    error: str | None = None,
) -> VariantExecution:
    return VariantExecution(name, tuple(docs), input_count, error)


def _result(*variants: VariantExecution) -> PrefixExecutionResult:
    return PrefixExecutionResult(tuple(variants))


def test_per_stage_executes_each_prefix_in_order() -> None:
    mql = (
        "db.account.aggregate(["
        '{"$match":{"status":"active"}},'
        '{"$addFields":{"score":1}},'
        '{"$project":{"score":1}}'
        "])"
    )
    executor = FakePrefixExecutor(
        {
            1: _result(_variant("loan-present", [{"_id": 1}], input_count=2)),
            2: _result(_variant("loan-present", [{"_id": 1, "score": 1}], input_count=2)),
            3: _result(_variant("loan-present", [{"score": 1}], input_count=2)),
        }
    )

    result = run_per_stage_check(db_id="financial", mql=mql, executor=executor)

    assert result.ok is True
    assert result.prefixes_executed == 3
    assert [request.stage_index for request in executor.requests] == [1, 2, 3]
    assert [len(parse_pipeline(request.mql)[1]) for request in executor.requests] == [1, 2, 3]
    assert [request.collection for request in executor.requests] == ["account"] * 3


def test_doc_count_collapse_feedback_identifies_stage_and_variant() -> None:
    mql = (
        "db.account.aggregate(["
        '{"$match":{"loan.status":{"$exists":true}}},'
        '{"$addFields":{"ratio":0}}'
        "])"
    )
    executor = FakePrefixExecutor(
        {
            1: _result(
                _variant("loan-present", [{"_id": 1}], input_count=1),
                _variant("loan-absent", [], input_count=4),
            ),
            2: _result(_variant("loan-present", [{"_id": 1, "ratio": 0}], input_count=1)),
        }
    )

    result = run_per_stage_check(db_id="financial", mql=mql, executor=executor)

    assert result.ok is False
    assert result.feedback is not None
    assert result.feedback.error_code == CheckpointCode.DOC_COUNT_COLLAPSE
    assert result.feedback.stage_index == 1
    assert result.feedback.failing_variant == "loan-absent"
    assert result.feedback.suspect_field is None
    assert result.prefixes_executed == 1
    assert [request.stage_index for request in executor.requests] == [1]
    json.dumps(result.feedback.to_log_context())


def test_target_field_missing_feedback_identifies_projection_stage() -> None:
    mql = (
        "db.account.aggregate(["
        '{"$addFields":{"score":1}},'
        '{"$project":{"_id":1}}'
        "])"
    )
    executor = FakePrefixExecutor(
        {
            1: _result(_variant("default", [{"_id": 1, "score": 1}], input_count=1)),
            2: _result(_variant("default", [{"_id": 1}], input_count=1)),
        }
    )

    result = run_per_stage_check(
        db_id="financial",
        mql=mql,
        executor=executor,
        checkpoint=CheckpointSpec(required_fields_by_stage={1: (), 2: ("score",)}),
    )

    assert result.ok is False
    assert result.feedback is not None
    assert result.feedback.error_code == CheckpointCode.TARGET_FIELD_MISSING
    assert result.feedback.stage_index == 2
    assert result.feedback.failing_variant == "default"
    assert result.feedback.suspect_field == "score"
    assert result.prefixes_executed == 2


def test_target_field_missing_fails_when_any_output_document_lacks_field() -> None:
    mql = 'db.account.aggregate([{"$addFields":{"score":1}}])'
    executor = FakePrefixExecutor(
        {
            1: _result(
                _variant(
                    "default",
                    [{"_id": 1, "score": 1}, {"_id": 2}],
                    input_count=2,
                )
            ),
        }
    )

    result = run_per_stage_check(
        db_id="financial",
        mql=mql,
        executor=executor,
        checkpoint=CheckpointSpec(required_fields_by_stage={1: ("score",)}),
    )

    assert result.ok is False
    assert result.feedback is not None
    assert result.feedback.error_code == CheckpointCode.TARGET_FIELD_MISSING
    assert result.feedback.stage_index == 1
    assert result.feedback.failing_variant == "default"
    assert result.feedback.suspect_field == "score"
    assert result.feedback.context["missing_count"] == 1
    assert result.feedback.context["output_count"] == 2


def test_disabled_operator_rejects_before_executing_bad_prefix() -> None:
    mql = (
        "db.account.aggregate(["
        '{"$match":{"status":"active"}},'
        '{"$sample":{"size":1}}'
        "])"
    )
    executor = FakePrefixExecutor(
        {
            1: _result(_variant("default", [{"_id": 1}], input_count=1)),
            2: _result(_variant("default", [{"_id": 1}], input_count=1)),
        }
    )

    result = run_per_stage_check(db_id="financial", mql=mql, executor=executor)

    assert result.ok is False
    assert result.feedback is not None
    assert result.feedback.error_code == CheckpointCode.DISABLED_OPERATOR
    assert result.feedback.stage_index == 2
    assert result.feedback.suspect_field == "$sample"
    assert result.prefixes_executed == 1
    assert [request.stage_index for request in executor.requests] == [1]
    json.dumps(result.feedback.to_log_context())


def test_recoverable_checkpoint_feedback_logs_warning_without_anomaly() -> None:
    class CapturingLogger:
        def __init__(self) -> None:
            self.warnings: list[tuple[str, dict]] = []
            self.anomalies: list[tuple[tuple, dict]] = []

        def info(self, _event: str, **_fields) -> None:
            pass

        def warning(self, event: str, **fields) -> None:
            self.warnings.append((event, fields))

        def anomaly(self, *args, **fields) -> None:
            self.anomalies.append((args, fields))

    mql = 'db.account.aggregate([{"$project":{"_id":1}}])'
    executor = FakePrefixExecutor(
        {
            1: _result(_variant("default", [{"_id": 1}], input_count=1)),
        }
    )
    logger = CapturingLogger()

    result = run_per_stage_check(
        db_id="financial",
        mql=mql,
        executor=executor,
        checkpoint=CheckpointSpec(required_fields_by_stage={1: ("score",)}),
        logger=logger,
    )

    assert result.ok is False
    assert logger.warnings
    assert logger.warnings[-1][0] == "solver_per_stage_checkpoint_failed"
    assert logger.anomalies == []


def test_executor_exception_becomes_exec_error_feedback_with_cause() -> None:
    mql = (
        "db.account.aggregate(["
        '{"$match":{"status":"active"}},'
        '{"$addFields":{"ratio":{"$divide":[1,0]}}}'
        "])"
    )
    boom = RuntimeError("division by zero")
    executor = FakePrefixExecutor(
        {
            1: _result(_variant("default", [{"_id": 1}], input_count=1)),
            2: boom,
        }
    )

    result = run_per_stage_check(db_id="financial", mql=mql, executor=executor)

    assert result.ok is False
    assert result.feedback is not None
    assert result.feedback.error_code == CheckpointCode.EXEC_ERROR
    assert result.feedback.stage_index == 2
    assert result.cause is boom
    assert result.prefixes_executed == 1
    json.dumps(result.feedback.to_log_context())
