"""Runtime workflow for constrained LLM baselines."""
from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..errors import Anomaly, PromptAnomalyError, TendError, wrap_unexpected
from ..execution.ast_check import parse_pipeline, scan_disabled
from ..solver.guards import SolverBoundary, check_disjointness, load_solver_allow_list
from ..solver.workflow import build_witness_digest, load_solver_release_inputs
from ..workflow import Workflow
from .strategies import (
    BaselinePromptContext,
    BaselineSpec,
    baseline_ids,
    resolve_baselines,
)

BASELINE_IDS = baseline_ids()


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
    record_id: int | None = None,
    limit: int = 1,
    witness_k: int = 3,
) -> list[dict[str, Any]]:
    specs = resolve_baselines(baseline_selection)
    inputs = load_solver_release_inputs(
        dataset_dir,
        db_id=db_id,
        record_id=record_id,
        limit=limit,
    )
    log = wf.ctx.log.bind(component="baseline_suite")
    log.info(
        "baseline_suite_start",
        baselines=[spec.id for spec in specs],
        records=len(inputs),
        dataset_dir=str(dataset_dir),
        db_id=db_id,
        record_id=record_id,
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
    except Exception:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for exc in results:
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
    boundary = SolverBoundary.from_settings(wf.ctx.settings, logger=base_log)
    safe = boundary.sanitize_test_record(record)
    db_id = str(safe["db_id"])
    record_id = safe.get("record_id")
    disclosure = _baseline_disclosure(wf, spec)
    try:
        nlq = _canonical_nlq(safe)
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
        )
    schema_summary = summarize_schema(schema)
    witness_digest = build_witness_digest(local_data, witness_k)
    prompt_ctx = BaselinePromptContext(
        record=safe,
        schema=schema,
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
    try:
        for step in spec.steps:
            if spec.id == "static_self_debug" and step.id == "repair":
                static_feedback = _static_feedback(state.get("MQL"))
                state["static_feedback"] = static_feedback
            output, trace = await _run_step(
                ctx, spec, step, prompt_ctx, state, group, batch_index=batch_index
            )
            traces.append(trace)
            state.update(output)

        mql = _extract_mql(state)
        final_feedback = _static_feedback(mql)
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
            steps=traces,
            static_feedback=static_feedback,
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
            steps=traces,
            static_feedback=static_feedback,
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
            if isinstance(value, dict) and key not in {"db_id", "metadata"}
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


def _baseline_disclosure(wf: Workflow, spec: BaselineSpec) -> dict[str, Any]:
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
    }


def _static_feedback(mql: Any) -> list[dict[str, Any]]:
    text = str(mql or "")
    if not text.strip():
        return [{"severity": "error", "code": "EMPTY_MQL", "message": "MQL is empty"}]
    feedback: list[dict[str, Any]] = []
    try:
        collection, pipeline = parse_pipeline(text)
        feedback.append({
            "severity": "info",
            "code": "PARSE_OK",
            "collection": collection,
            "stages": len(pipeline),
        })
    except Exception as exc:  # noqa: BLE001 - parser details belong in feedback
        feedback.append({
            "severity": "error",
            "code": "PARSE_ERROR",
            "message": str(exc),
        })
        return feedback
    disabled = scan_disabled(text)
    if disabled:
        feedback.append({
            "severity": "error",
            "code": "DISABLED_OPERATOR",
            "operators": disabled,
        })
    return feedback


def _extract_mql(state: dict[str, Any]) -> str:
    value = state.get("MQL") or state.get("mql")
    return str(value or "")


def _canonical_nlq(record: dict[str, Any]) -> str:
    nl_queries = record.get("nl_queries")
    canonical = nl_queries.get("canonical") if isinstance(nl_queries, dict) else None
    if isinstance(canonical, str) and canonical.strip():
        return canonical
    raise PromptAnomalyError(
        "baseline record missing canonical NLQ",
        context={"record_id": record.get("record_id"), "db_id": record.get("db_id")},
    )
