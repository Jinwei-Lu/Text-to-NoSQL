"""Per-stage execution-guided decoding checkpoints for Mongo aggregation pipelines.

This module is intentionally solver-local and executor-agnostic. It parses a complete
``db.<collection>.aggregate([...])`` candidate, renders every stage prefix as a legal
pipeline, executes those prefixes through an injected local executor, and returns the
first structured checkpoint feedback that should be routed back into SMART stage 2/3/4.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Protocol, runtime_checkable

from tend.errors import DisabledOperatorError, ExecutionError, TendError
from tend.execution.ast_check import (
    DISABLED_OPERATORS,
    DISABLED_SYSTEM_VARS,
    parse_pipeline,
)

JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class CheckpointCode(str, Enum):
    """Stable feedback codes consumed by the solver self-debug loop."""

    DOC_COUNT_COLLAPSE = "DOC_COUNT_COLLAPSE"
    TARGET_FIELD_MISSING = "TARGET_FIELD_MISSING"
    EXEC_ERROR = "EXEC_ERROR"
    DISABLED_OPERATOR = "DISABLED_OPERATOR"


@dataclass(frozen=True)
class DisabledOperatorHit:
    """Location of a banned operator or system variable in a specific stage."""

    token: str
    path: str

    def to_log_context(self) -> dict[str, JSONValue]:
        return {"token": self.token, "path": self.path}


@dataclass(frozen=True)
class PrefixExecutionRequest:
    """A legal Mongo aggregate prefix sent to the local, variant-stratified executor."""

    db_id: str
    collection: str
    stage_index: int
    stage: Mapping[str, Any]
    pipeline: tuple[Mapping[str, Any], ...]
    mql: str

    def to_log_context(self) -> dict[str, JSONValue]:
        return {
            "db_id": self.db_id,
            "collection": self.collection,
            "stage_index": self.stage_index,
            "prefix_stage_count": len(self.pipeline),
            "root_operator": _root_operator(self.stage),
        }


@dataclass(frozen=True)
class VariantExecution:
    """Result for one local sample stratum after executing a prefix."""

    variant: str
    documents: tuple[Mapping[str, Any], ...] = ()
    input_count: int | None = None
    error: str | None = None
    context: Mapping[str, JSONValue] = field(default_factory=dict)

    @property
    def output_count(self) -> int:
        return len(self.documents)

    def to_log_context(self) -> dict[str, JSONValue]:
        payload: dict[str, JSONValue] = {
            "variant": self.variant,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "error": self.error,
        }
        if self.context:
            payload["context"] = dict(self.context)
        return payload


@dataclass(frozen=True)
class PrefixExecutionResult:
    """Executor output normalized into per-variant documents and counts."""

    variants: tuple[VariantExecution, ...]

    @classmethod
    def single_variant(
        cls,
        documents: Sequence[Mapping[str, Any]],
        *,
        variant: str = "default",
        input_count: int | None = None,
    ) -> "PrefixExecutionResult":
        return cls((VariantExecution(variant, tuple(documents), input_count),))

    def to_log_context(self) -> dict[str, JSONValue]:
        return {"variants": [v.to_log_context() for v in self.variants]}


@runtime_checkable
class PrefixExecutor(Protocol):
    """Executor contract used by this module.

    Production code can adapt a real Mongo executor behind this narrow method; tests can
    provide a fake. The executor should run ``request.mql`` on the solver's local
    variant-stratified sample, not on any evaluation gold database.
    """

    def execute_prefix(self, request: PrefixExecutionRequest) -> PrefixExecutionResult:
        """Execute one aggregate prefix and return per-variant results."""


@dataclass(frozen=True)
class CheckpointSpec:
    """Checkpoint policy for per-stage validation.

    ``target_fields`` are checked at every prefix unless ``required_fields_by_stage`` is
    supplied for a stage. This keeps the default strict for shape-preserving target
    fields, while allowing later integration to express fields that are only materialized
    after a specific stage.
    """

    target_fields: tuple[str, ...] = ()
    required_fields_by_stage: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    collapse_to_zero: bool = True

    def fields_for_stage(self, stage_index: int) -> tuple[str, ...]:
        return self.required_fields_by_stage.get(stage_index, self.target_fields)


@dataclass(frozen=True)
class CheckpointFeedback:
    """Structured feedback returned to the solver self-debug loop."""

    error_code: CheckpointCode
    stage_index: int
    failing_variant: str | None = None
    suspect_field: str | None = None
    message: str = ""
    context: Mapping[str, JSONValue] = field(default_factory=dict)

    def to_log_context(self) -> dict[str, JSONValue]:
        payload: dict[str, JSONValue] = {
            "error_code": self.error_code.value,
            "stage_index": self.stage_index,
            "failing_variant": self.failing_variant,
            "suspect_field": self.suspect_field,
            "message": self.message,
        }
        payload.update(dict(self.context))
        return payload


@dataclass(frozen=True)
class PerStageResult:
    """Outcome of checking all prefixes or the first failing checkpoint."""

    ok: bool
    collection: str
    stage_count: int
    prefixes_executed: int
    feedback: CheckpointFeedback | None = None
    final_mql: str | None = None
    cause: BaseException | None = field(default=None, compare=False, repr=False)

    def to_log_context(self) -> dict[str, JSONValue]:
        payload: dict[str, JSONValue] = {
            "ok": self.ok,
            "collection": self.collection,
            "stage_count": self.stage_count,
            "prefixes_executed": self.prefixes_executed,
        }
        if self.feedback is not None:
            payload["feedback"] = self.feedback.to_log_context()
        return payload


def run_per_stage_check(
    *,
    db_id: str,
    mql: str,
    executor: PrefixExecutor,
    checkpoint: CheckpointSpec | None = None,
    logger: Any | None = None,
) -> PerStageResult:
    """Execute every Mongo aggregation prefix and return the first checkpoint failure.

    The returned feedback is intentionally compact:
    ``{error_code, stage_index, failing_variant, suspect_field}`` plus small diagnostic
    context. Raw rows are never copied into log context or feedback.
    """

    checkpoint = checkpoint or CheckpointSpec()
    collection, pipeline = parse_pipeline(mql)
    stage_count = len(pipeline)
    _log_info(
        logger,
        "solver_per_stage_start",
        db_id=db_id,
        collection=collection,
        stage_count=stage_count,
        target_fields=list(checkpoint.target_fields),
    )

    executed = 0
    for stage_index, stage in enumerate(pipeline, start=1):
        prefix = tuple(pipeline[:stage_index])
        prefix_mql = render_prefix_mql(collection, prefix)
        request = PrefixExecutionRequest(
            db_id=db_id,
            collection=collection,
            stage_index=stage_index,
            stage=stage,
            pipeline=prefix,
            mql=prefix_mql,
        )
        _log_info(logger, "solver_per_stage_prefix", **request.to_log_context())

        disabled_hits = find_disabled_operator_hits(stage, stage_index)
        if disabled_hits:
            hit = disabled_hits[0]
            feedback = CheckpointFeedback(
                error_code=CheckpointCode.DISABLED_OPERATOR,
                stage_index=stage_index,
                suspect_field=hit.token,
                message="prefix uses disabled Mongo operator",
                context={
                    **request.to_log_context(),
                    "disabled_hits": [h.to_log_context() for h in disabled_hits],
                },
            )
            _emit_failure(logger, feedback)
            return PerStageResult(
                ok=False,
                collection=collection,
                stage_count=stage_count,
                prefixes_executed=executed,
                feedback=feedback,
            )

        try:
            raw_result = executor.execute_prefix(request)
            prefix_result = normalize_prefix_result(raw_result)
        except Exception as exc:  # noqa: BLE001 - executor boundaries normalize all backends
            feedback = _exec_error_feedback(request, exc)
            _emit_failure(logger, feedback, exc)
            return PerStageResult(
                ok=False,
                collection=collection,
                stage_count=stage_count,
                prefixes_executed=executed,
                feedback=feedback,
                cause=exc,
            )
        executed += 1

        feedback = checkpoint_prefix(request, prefix_result, checkpoint)
        if feedback is not None:
            _emit_failure(logger, feedback)
            return PerStageResult(
                ok=False,
                collection=collection,
                stage_count=stage_count,
                prefixes_executed=executed,
                feedback=feedback,
            )

    _log_info(
        logger,
        "solver_per_stage_ok",
        db_id=db_id,
        collection=collection,
        stage_count=stage_count,
        prefixes_executed=executed,
    )
    return PerStageResult(
        ok=True,
        collection=collection,
        stage_count=stage_count,
        prefixes_executed=executed,
        final_mql=mql,
    )


def checkpoint_prefix(
    request: PrefixExecutionRequest,
    result: PrefixExecutionResult,
    spec: CheckpointSpec,
) -> CheckpointFeedback | None:
    """Apply collapse, variant error, and target-field checkpoints to one prefix."""

    for variant in result.variants:
        if variant.error:
            return CheckpointFeedback(
                error_code=CheckpointCode.EXEC_ERROR,
                stage_index=request.stage_index,
                failing_variant=variant.variant,
                message="variant prefix execution failed",
                context={
                    **request.to_log_context(),
                    "error": variant.error,
                    "variants": [v.to_log_context() for v in result.variants],
                },
            )
        if _collapsed_to_zero(variant, spec):
            return CheckpointFeedback(
                error_code=CheckpointCode.DOC_COUNT_COLLAPSE,
                stage_index=request.stage_index,
                failing_variant=variant.variant,
                message="prefix collapsed a non-empty variant to zero documents",
                context={
                    **request.to_log_context(),
                    "variants": [v.to_log_context() for v in result.variants],
                },
            )

    required_fields = spec.fields_for_stage(request.stage_index)
    for field_name in required_fields:
        for variant in result.variants:
            missing_count = sum(1 for doc in variant.documents if not _has_path(doc, field_name))
            if variant.documents and missing_count:
                return CheckpointFeedback(
                    error_code=CheckpointCode.TARGET_FIELD_MISSING,
                    stage_index=request.stage_index,
                    failing_variant=variant.variant,
                    suspect_field=field_name,
                    message="required target field is absent from at least one document in a variant",
                    context={
                        **request.to_log_context(),
                        "required_fields": list(required_fields),
                        "missing_count": missing_count,
                        "output_count": variant.output_count,
                        "variants": [v.to_log_context() for v in result.variants],
                    },
                )
    return None


def normalize_prefix_result(raw_result: Any) -> PrefixExecutionResult:
    """Accept the native result dataclass plus lightweight fallback shapes."""

    if isinstance(raw_result, PrefixExecutionResult):
        return raw_result
    if isinstance(raw_result, Sequence) and not isinstance(raw_result, (str, bytes, bytearray)):
        if all(isinstance(item, Mapping) for item in raw_result):
            return PrefixExecutionResult.single_variant(raw_result)  # type: ignore[arg-type]
    if isinstance(raw_result, Mapping):
        if "variants" in raw_result:
            variants = []
            for raw_variant in raw_result["variants"]:
                if isinstance(raw_variant, VariantExecution):
                    variants.append(raw_variant)
                    continue
                if not isinstance(raw_variant, Mapping):
                    raise TypeError("variant result must be a mapping or VariantExecution")
                docs = tuple(raw_variant.get("documents", ()))
                variants.append(
                    VariantExecution(
                        str(raw_variant.get("variant", "default")),
                        docs,
                        raw_variant.get("input_count"),
                        raw_variant.get("error"),
                        dict(raw_variant.get("context") or {}),
                    )
                )
            return PrefixExecutionResult(tuple(variants))
        if "documents" in raw_result:
            return PrefixExecutionResult.single_variant(
                tuple(raw_result["documents"]),
                variant=str(raw_result.get("variant", "default")),
                input_count=raw_result.get("input_count"),
            )
    raise TypeError(f"unsupported prefix execution result: {type(raw_result).__name__}")


def render_prefix_mql(collection: str, pipeline: Sequence[Mapping[str, Any]]) -> str:
    """Render a legal ``db.<collection>.aggregate([...])`` prefix."""

    return (
        f"db.{collection}.aggregate("
        f"{json.dumps(list(pipeline), ensure_ascii=False, default=str, separators=(',', ':'))}"
        ")"
    )


def find_disabled_operator_hits(
    stage: Mapping[str, Any],
    stage_index: int,
) -> tuple[DisabledOperatorHit, ...]:
    """Find banned operators and system variables inside one stage."""

    hits: list[DisabledOperatorHit] = []
    _walk_disabled(stage, f"$[{stage_index - 1}]", hits)
    return tuple(hits)


def _walk_disabled(node: Any, path: str, hits: list[DisabledOperatorHit]) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            key_path = f"{path}.{key}"
            if isinstance(key, str) and key in DISABLED_OPERATORS:
                hits.append(DisabledOperatorHit(key, key_path))
            _walk_disabled(value, key_path, hits)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_disabled(value, f"{path}[{index}]", hits)
    elif isinstance(node, str) and node in DISABLED_SYSTEM_VARS:
        hits.append(DisabledOperatorHit(node, path))


def _exec_error_feedback(request: PrefixExecutionRequest, exc: BaseException) -> CheckpointFeedback:
    failing_variant = None
    error_type = type(exc).__name__
    message = str(exc)
    extra: dict[str, JSONValue] = {}
    if isinstance(exc, TendError):
        failing = exc.context.get("failing_variant")
        if failing is not None:
            failing_variant = str(failing)
        extra = _json_safe_mapping(exc.context)
    return CheckpointFeedback(
        error_code=CheckpointCode.EXEC_ERROR,
        stage_index=request.stage_index,
        failing_variant=failing_variant,
        message="prefix execution raised an exception",
        context={
            **request.to_log_context(),
            "exception_type": error_type,
            "error": message[:500],
            "exception_context": extra,
        },
    )


def _collapsed_to_zero(variant: VariantExecution, spec: CheckpointSpec) -> bool:
    if not spec.collapse_to_zero or variant.output_count != 0:
        return False
    return variant.input_count is None or variant.input_count > 0


def _has_path(doc: Mapping[str, Any], field_path: str) -> bool:
    return _has_parts(doc, tuple(part for part in field_path.split(".") if part))


def _has_parts(current: Any, parts: tuple[str, ...]) -> bool:
    if not parts:
        return True
    part, remaining = parts[0], parts[1:]
    if isinstance(current, Mapping):
        if part not in current:
            return False
        return _has_parts(current[part], remaining)
    if isinstance(current, list):
        return any(_has_parts(item, parts) for item in current)
    return False


def _root_operator(stage: Mapping[str, Any]) -> str | None:
    for key in stage:
        if isinstance(key, str) and key.startswith("$"):
            return key
    return None


def _json_safe_mapping(mapping: Mapping[str, Any]) -> dict[str, JSONValue]:
    safe: dict[str, JSONValue] = {}
    for key, value in mapping.items():
        safe[str(key)] = _json_safe(value)
    return safe


def _json_safe(value: Any) -> JSONValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(v) for v in value]
    return str(value)


def _log_info(logger: Any | None, event: str, **fields: JSONValue) -> None:
    if logger is not None:
        logger.info(event, **fields)


def _emit_failure(
    logger: Any | None,
    feedback: CheckpointFeedback,
    cause: BaseException | None = None,
    *,
    emit_anomaly: bool = False,
) -> None:
    if logger is None:
        return
    context = feedback.to_log_context()
    logger.warning("solver_per_stage_checkpoint_failed", **context)
    if not emit_anomaly:
        return
    if not hasattr(logger, "anomaly"):
        return
    if feedback.error_code == CheckpointCode.DISABLED_OPERATOR:
        err = DisabledOperatorError("per-stage prefix uses disabled operator", context=context)
        logger.anomaly(err)
    elif feedback.error_code == CheckpointCode.EXEC_ERROR:
        if isinstance(cause, ExecutionError):
            err = cause.with_context(**context)
        else:
            err = ExecutionError("per-stage prefix execution failed", context=context)
        logger.anomaly(err)
    else:
        logger.anomaly(
            kind=feedback.error_code.value,
            message=feedback.message or "per-stage checkpoint failed",
            **context,
        )
