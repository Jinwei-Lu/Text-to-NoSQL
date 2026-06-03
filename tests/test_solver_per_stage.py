from __future__ import annotations

import json

from tend.execution.ast_check import parse_pipeline
from tend.solver.per_stage import (
    CheckpointCode,
    CheckpointSpec,
    PrefixExecutionRequest,
    PrefixExecutionResult,
    VariantExecution,
    _collapsed_to_zero,
    _emit_failure,
    _walk_disabled,
    CheckpointFeedback,
    DisabledOperatorHit,
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

    result = run_per_stage_check(
        db_id="financial",
        mql=mql,
        executor=executor,
        checkpoint=CheckpointSpec(collapse_to_zero=True),
    )

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


# ─── F3: _collapsed_to_zero regression ────────────────────────────────────────


def test_collapsed_to_zero_no_false_positive_when_input_count_is_none() -> None:
    # output_count==0 but input_count==None must NOT trigger DOC_COUNT_COLLAPSE
    variant = VariantExecution("v", (), input_count=None)
    spec = CheckpointSpec(collapse_to_zero=True)
    assert _collapsed_to_zero(variant, spec) is False


def test_collapsed_to_zero_true_when_input_positive_and_output_zero() -> None:
    # input_count>0 and output_count==0 with collapse_to_zero enabled => True
    variant = VariantExecution("v", (), input_count=5)
    spec = CheckpointSpec(collapse_to_zero=True)
    assert _collapsed_to_zero(variant, spec) is True


def test_collapsed_to_zero_false_when_spec_disabled() -> None:
    # Even if input>0 and output==0, collapse_to_zero=False means no collapse flag
    variant = VariantExecution("v", (), input_count=3)
    spec = CheckpointSpec(collapse_to_zero=False)
    assert _collapsed_to_zero(variant, spec) is False


# ─── F5: _walk_disabled string-value detection ────────────────────────────────


def test_walk_disabled_detects_operator_as_string_value() -> None:
    # $sample appearing as a string value (not as a dict key) must be flagged
    hits: list[DisabledOperatorHit] = []
    _walk_disabled({"$set": {"method": "$sample"}}, "$[0]", hits)
    tokens = [h.token for h in hits]
    assert "$sample" in tokens


def test_walk_disabled_detects_system_var_as_string_value() -> None:
    # $$NOW appearing as a string value must be flagged (it's in DISABLED_SYSTEM_VARS)
    hits: list[DisabledOperatorHit] = []
    _walk_disabled({"$project": {"x": "$$NOW"}}, "$[0]", hits)
    tokens = [h.token for h in hits]
    assert "$$NOW" in tokens


def test_walk_disabled_still_detects_operator_as_key() -> None:
    # Original key-based detection must remain intact
    hits: list[DisabledOperatorHit] = []
    _walk_disabled({"$sample": {"size": 1}}, "$[1]", hits)
    tokens = [h.token for h in hits]
    assert "$sample" in tokens


# ─── F4: _emit_failure / EXEC_ERROR anomaly escalation ────────────────────────


def test_emit_failure_logs_warning_for_exec_error_without_anomaly_by_default() -> None:
    class CapturingLogger:
        def __init__(self) -> None:
            self.warnings: list[str] = []
            self.anomalies: list[object] = []

        def warning(self, event: str, **_fields) -> None:
            self.warnings.append(event)

        def anomaly(self, *args, **_kwargs) -> None:
            self.anomalies.extend(args)

    feedback = CheckpointFeedback(
        error_code=CheckpointCode.EXEC_ERROR,
        stage_index=1,
        message="boom",
    )
    logger = CapturingLogger()
    _emit_failure(logger, feedback, cause=RuntimeError("boom"), emit_anomaly=False)

    assert logger.warnings == ["solver_per_stage_checkpoint_failed"]
    assert logger.anomalies == []


def test_emit_failure_escalates_anomaly_for_exec_error_when_requested() -> None:
    class CapturingLogger:
        def __init__(self) -> None:
            self.warnings: list[str] = []
            self.anomalies: list[object] = []

        def warning(self, event: str, **_fields) -> None:
            self.warnings.append(event)

        def anomaly(self, *args, **_kwargs) -> None:
            self.anomalies.extend(args)

    feedback = CheckpointFeedback(
        error_code=CheckpointCode.EXEC_ERROR,
        stage_index=2,
        message="exec boom",
    )
    logger = CapturingLogger()
    _emit_failure(logger, feedback, cause=RuntimeError("exec boom"), emit_anomaly=True)

    assert logger.warnings == ["solver_per_stage_checkpoint_failed"]
    assert len(logger.anomalies) == 1
