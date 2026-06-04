"""Runtime workflow for constrained LLM baselines."""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..errors import Anomaly, SourceError, TendError, wrap_unexpected
from ..execution.ast_check import static_mql_feedback
from .boundary import (
    PUBLIC_SCHEMA_VERSION,
    check_disjointness,
    load_solver_allow_list,
    public_schema_shape,
    sanitize_public_record,
    sanitize_public_schema,
)
from ..solver.inputs import (
    NlqTrack,
    _canonical_nlq,
    build_nlq_db_solver_input,
    build_witness_digest,
    load_solver_release_inputs,
)
from ..workflow import Workflow
from .strategies import (
    BaselinePromptContext,
    BaselineSpec,
    resolve_baselines,
)


@dataclass(frozen=True, slots=True)
class BaselineStepTrace:
    step_id: str
    agent: str
    title: str
    transcript_ref: str
    diagnostics_ref: str
    output: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BaselinePrediction:
    baseline_id: str
    baseline_title: str
    record_id: int | None
    db_id: str
    MQL: str
    disclosure: dict[str, Any]
    steps: list[BaselineStepTrace]
    witness_k: int = 0
    r_max: int = 0
    static_feedback: list[dict[str, Any]] = field(default_factory=list)
    result_type: str = "baseline_prediction"
    status: str = "ok"

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [asdict(step) for step in self.steps]
        return payload


@dataclass(frozen=True, slots=True)
class BaselineFailure:
    baseline_id: str
    baseline_title: str
    record_id: int | None
    db_id: str
    error_code: str
    message: str
    disclosure: dict[str, Any]
    witness_k: int = 0
    r_max: int = 0
    steps: list[BaselineStepTrace] = field(default_factory=list)
    static_feedback: list[dict[str, Any]] = field(default_factory=list)
    result_type: str = "baseline_failure"
    status: str = "failed"

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [asdict(step) for step in self.steps]
        return payload


async def run_baseline_suite(
    wf: Workflow,
    *,
    dataset_dir: Path,
    baseline_selection: str | list[str] | tuple[str, ...] | None = "all",
    db_id: str | None = None,
    nlq: str | None = None,
    nlq_track: NlqTrack = "record",
    record_id: int | None = None,
    limit: int = 1,
    witness_k: int = 3,
) -> list[dict[str, Any]]:
    specs = resolve_baselines(baseline_selection)
    if nlq is not None:
        if not db_id:
            raise SourceError("NLQ+DB baseline mode requires --db-id")
        runtime_input = await build_nlq_db_solver_input(
            wf,
            db_id=str(db_id),
            nlq=nlq,
            record_id=record_id,
            witness_k=witness_k,
        )
        inputs = [(runtime_input.record, runtime_input.schema, runtime_input.local_data)]
    else:
        inputs = load_solver_release_inputs(
            dataset_dir,
            db_id=db_id,
            record_id=record_id,
            limit=limit,
            nlq_track=nlq_track,
        )
    log = wf.ctx.log.bind(component="baseline_suite")
    log.info(
        "baseline_suite_start",
        baselines=[spec.id for spec in specs],
        records=len(inputs),
        dataset_dir=str(dataset_dir),
        db_id=db_id,
        record_id=record_id,
        input_mode="nlq_db" if nlq is not None else "release",
    )
    if not inputs:
        log.anomaly(
            kind=Anomaly.SUPPLY_EXHAUSTED,
            message="no baseline records matched filters",
            dataset_dir=str(dataset_dir),
            db_id=db_id,
            record_id=record_id,
        )
        return []

    if wf.ctx.progress:
        wf.ctx.progress.phase("BASELINE")

    work: list[tuple[int, BaselineSpec, dict, dict, dict | None]] = []
    for record, schema, data in inputs:
        for spec in specs:
            work.append((len(work), spec, record, schema, data))

    async def run_one(
        batch_index: int,
        spec: BaselineSpec,
        record: dict,
        schema: dict,
        data: dict | None,
    ) -> tuple[int, dict[str, Any]]:
        result = await run_baseline_record(
            wf,
            spec,
            record,
            schema,
            local_data=data,
            witness_k=witness_k,
            batch_index=batch_index,
        )
        payload = result.to_json()
        payload["batch_index"] = batch_index
        payload["work_item_id"] = (
            f"baseline:{batch_index}:{spec.id}:{record.get('db_id')}:"
            f"{record.get('record_id')}"
        )
        return batch_index, payload

    tasks = [
        asyncio.create_task(run_one(batch_index, spec, record, schema, data))
        for batch_index, spec, record, schema, data in work
    ]
    try:
        completed = await asyncio.gather(*tasks)
    except Exception as first_exc:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for exc in results:
            # The originating exception already propagated (and run_baseline_record logs
            # per-record); skip it here so we do not double-log the same failure.
            if exc is first_exc:
                continue
            if isinstance(exc, Exception) and not isinstance(exc, asyncio.CancelledError):
                log.anomaly(wrap_unexpected(exc, stage="baseline_suite_gather"))
        raise
    outputs = [payload for _, payload in sorted(completed, key=lambda item: item[0])]
    log.info("baseline_suite_done", outputs=len(outputs), baselines=len(specs))
    return outputs


async def run_baseline_record(
    wf: Workflow,
    spec: BaselineSpec,
    record: dict[str, Any],
    schema: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    witness_k: int = 3,
    batch_index: int | None = None,
) -> BaselinePrediction | BaselineFailure:
    base_log = wf.ctx.log.bind(
        component="baseline_runner", baseline_id=spec.id, batch_index=batch_index
    )
    sanitized_record = sanitize_public_record(record)
    sanitized_schema = sanitize_public_schema(schema)
    if sanitized_record.stripped_fields:
        base_log.info("baseline_record_fields_stripped", fields=sanitized_record.stripped_fields)
    if sanitized_schema.stripped_fields:
        base_log.info("baseline_schema_fields_stripped", fields=sanitized_schema.stripped_fields)
    safe = sanitized_record.value
    public_schema = sanitized_schema.value
    db_id = str(safe["db_id"])
    record_id = safe.get("record_id")
    disclosure = _baseline_disclosure(
        wf,
        spec,
        witness_k=witness_k,
        schema_stripped_fields=sanitized_schema.stripped_fields,
        record_stripped_fields=sanitized_record.stripped_fields,
        schema_public_shape=public_schema_shape(public_schema),
    )
    try:
        # Baselines expose only the canonical NLQ track after record sanitization.
        nlq = _canonical_nlq(safe, use_colloquial=False)
    except TendError as err:
        err.with_context(baseline_id=spec.id, db_id=db_id, record_id=record_id)
        base_log.anomaly(err)
        return BaselineFailure(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            error_code=err.anomaly.value if err.anomaly else "prompt_error",
            message=err.message,
            disclosure=disclosure,
            witness_k=witness_k,
            r_max=0,
        )
    schema_summary = summarize_schema(public_schema)
    witness_digest = build_witness_digest(local_data, witness_k)
    prompt_ctx = BaselinePromptContext(
        record=safe,
        schema=public_schema,
        witness_digest=witness_digest,
        schema_summary=schema_summary,
        nlq=nlq,
    )
    group_prefix = (
        f"baseline:{batch_index}:{spec.id}"
        if batch_index is not None
        else f"baseline:{spec.id}"
    )
    group = (
        f"{group_prefix}:{db_id}:{record_id}"
        if record_id is not None
        else f"{group_prefix}:{db_id}"
    )
    if wf.ctx.progress:
        wf.ctx.progress.add_group(
            group,
            f"{spec.id} {db_id}/{record_id}",
            phase="BASELINE",
            total=len(spec.steps),
        )

    ctx = wf.context(
        db_id=db_id,
        record_id=record_id,
        group=group,
        phase="BASELINE",
        work_item_id=f"batch_index={batch_index}" if batch_index is not None else None,
        extra={**wf.ctx.extra, "batch_index": batch_index},
    )
    log = ctx.log.bind(component="baseline_runner", baseline_id=spec.id,
                       batch_index=batch_index)
    log.info(
        "baseline_record_start",
        title=spec.title,
        limitations=list(spec.limitations),
        steps=[step.id for step in spec.steps],
    )

    state: dict[str, Any] = {}
    traces: list[BaselineStepTrace] = []
    static_feedback: list[dict[str, Any]] = []
    final_feedback: list[dict[str, Any]] = []
    try:
        for step in spec.steps:
            if spec.id == "static_self_debug" and step.id == "repair":
                static_feedback = static_mql_feedback(_extract_mql(state))
                state["static_feedback"] = static_feedback
            output, trace = await _run_step(
                ctx, spec, step, prompt_ctx, state, group, batch_index=batch_index
            )
            traces.append(trace)
            state.update(output)

        mql = _extract_mql(state)
        final_feedback = static_mql_feedback(mql)
        static_feedback = static_feedback or final_feedback
        if any(item["severity"] == "error" for item in final_feedback):
            log.anomaly(
                kind=Anomaly.PARSE_ERROR,
                message="baseline produced statically invalid MQL",
                baseline_id=spec.id,
                feedback=final_feedback,
            )
            return BaselineFailure(
                baseline_id=spec.id,
                baseline_title=spec.title,
                record_id=record_id,
                db_id=db_id,
                error_code="STATIC_INVALID_MQL",
                message="baseline produced statically invalid MQL",
                disclosure=disclosure,
                witness_k=witness_k,
                r_max=0,
                steps=traces,
                static_feedback=final_feedback,
            )

        log.info(
            "baseline_record_done",
            status="ok",
            mql_preview=mql[:240],
            steps=len(traces),
        )
        return BaselinePrediction(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            MQL=mql,
            disclosure=disclosure,
            witness_k=witness_k,
            r_max=0,
            steps=traces,
            static_feedback=final_feedback,
        )
    except TendError as err:
        err.with_context(baseline_id=spec.id, db_id=db_id, record_id=record_id)
        if not err.logged:
            log.anomaly(err)
        return BaselineFailure(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            error_code=err.anomaly.value if err.anomaly else "tend_error",
            message=err.message,
            disclosure=disclosure,
            witness_k=witness_k,
            r_max=0,
            steps=traces,
            static_feedback=final_feedback or static_feedback,
        )
    except Exception as exc:  # noqa: BLE001 - baseline runs should continue across records
        err = wrap_unexpected(
            exc,
            baseline_id=spec.id,
            db_id=db_id,
            record_id=record_id,
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        log.anomaly(err)
        return BaselineFailure(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            error_code="internal",
            message=err.message,
            disclosure=disclosure,
            witness_k=witness_k,
            r_max=0,
            steps=traces,
            static_feedback=final_feedback or static_feedback,
        )


async def _run_step(
    ctx: Any,
    spec: BaselineSpec,
    step: Any,
    prompt_ctx: BaselinePromptContext,
    state: dict[str, Any],
    group: str,
    *,
    batch_index: int | None = None,
) -> tuple[dict[str, Any], BaselineStepTrace]:
    prefix = (
        f"baseline:{batch_index}:{spec.id}"
        if batch_index is not None
        else f"baseline:{spec.id}"
    )
    task_id = (
        f"{prefix}:{prompt_ctx.record.get('db_id')}:"
        f"{prompt_ctx.record.get('record_id')}:{step.id}"
    )
    if ctx.progress:
        ctx.progress.start_task(task_id, step.title, group=group)
    log = ctx.log.bind(agent=step.agent, baseline_id=spec.id, baseline_step=step.id,
                       batch_index=batch_index)
    log.info("baseline_step_start", title=step.title)
    try:
        messages = step.build_messages(prompt_ctx, state)
        result = await ctx.llm.complete(
            agent=step.agent,
            messages=messages,
            logger=log,
            schema=step.schema,
            temperature=0.0,
        )
        output = result.data
        log.info(
            "baseline_step_done",
            transcript_ref=result.transcript_ref,
            diagnostics_ref=result.diagnostics_ref,
        )
        if ctx.progress:
            ctx.progress.finish_task(task_id, ok=True)
        return output, BaselineStepTrace(
            step_id=step.id,
            agent=step.agent,
            title=step.title,
            transcript_ref=result.transcript_ref,
            diagnostics_ref=result.diagnostics_ref,
            output=output,
        )
    except Exception:
        if ctx.progress:
            ctx.progress.finish_task(task_id, ok=False)
        raise


def summarize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    collections = schema.get("collections")
    if not isinstance(collections, dict):
        collections = {
            key: value for key, value in schema.items()
            if (
                isinstance(value, dict)
                and key not in {"db_id", "metadata", "structure_audit", "structure_gate"}
            )
        }
    summary: dict[str, Any] = {"collections": {}}
    for name, coll in sorted(collections.items()):
        if not isinstance(coll, dict):
            summary["collections"][name] = {"kind": type(coll).__name__}
            continue
        fields = coll.get("fields") if isinstance(coll.get("fields"), dict) else coll
        summary["collections"][name] = {
            "fields": sorted(str(key) for key in fields.keys())[:80]
            if isinstance(fields, dict)
            else [],
            "embeds": coll.get("embeds", []),
            "foreign_keys": coll.get("foreign_keys", coll.get("fks", [])),
        }
    return summary


def _baseline_disclosure(
    wf: Workflow,
    spec: BaselineSpec,
    *,
    witness_k: int,
    schema_stripped_fields: list[str] | None = None,
    record_stripped_fields: list[str] | None = None,
    schema_public_shape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_ids = [wf.ctx.settings.llm.model, *wf.ctx.settings.llm.agent_models.values()]
    allow_list = load_solver_allow_list(wf.ctx.settings.paths.schemas)
    disjointness = check_disjointness(
        model_ids,
        allow_list,
        require_manifests=not wf.ctx.settings.stub,
    )
    return {
        "baseline_id": spec.id,
        "baseline_title": spec.title,
        "backbone": wf.ctx.settings.llm.model,
        "s_solver": sorted(set(model_ids)),
        "no_training": True,
        "uses_train_json": False,
        "uses_gold_mql": False,
        "uses_execution_feedback": False,
        "disjointness_ok": disjointness["ok"],
        "disjointness_detail": disjointness,
        "limitations": list(spec.limitations),
        "r_max": 0,  # baselines have no retry loop
        "witness_k": witness_k,
        "public_schema_version": PUBLIC_SCHEMA_VERSION,
        "schema_sanitizer_applied": True,
        "record_sanitizer_applied": True,
        "schema_stripped_fields": list(schema_stripped_fields or []),
        "record_stripped_fields": list(record_stripped_fields or []),
        "schema_public_shape": schema_public_shape
        or {"format": "unknown", "collection_total": 0, "collections": []},
        "uses_public_witness_digest": True,
        "semantic_retry_budget": 0,
        "retry_contract": {
            "semantic_retry_budget": 0,
            "format_transport_retries_are_semantic_retries": False,
            "format_transport_retry_scope": "LLM client JSON/transport only",
        },
    }


def _extract_mql(state: dict[str, Any]) -> str:
    value = state.get("MQL") or state.get("mql")
    return str(value or "")
