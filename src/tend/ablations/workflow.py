"""Runtime workflow for SMART ablation studies."""
from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..errors import Anomaly, SourceError, TendError, wrap_unexpected
from ..execution.ast_check import static_mql_feedback
from ..solver.workflow import (
    NlqTrack,
    SmartSolveOptions,
    SolverFailure,
    build_nlq_db_solver_input,
    build_witness_digest,
    load_solver_release_inputs,
    smart_solve_record,
)
from ..workflow import Workflow
from .strategies import AblationSpec, resolve_ablations


@dataclass(frozen=True, slots=True)
class AblationPrediction:
    run_id: str
    ablation_id: str
    ablation_title: str
    solver_variant: str
    baseline_id: None
    record_id: int | None
    db_id: str
    MQL: str
    attempts: int
    r_max: int
    witness_k: int
    prompt_witness_sample_count_by_collection: dict[str, int]
    uses_shape_model: bool
    uses_variant_handling: bool
    uses_clause_coverage: bool
    uses_preserve_guard: bool
    uses_per_stage: bool
    uses_variant_strata: bool
    uses_execution_feedback: bool
    uses_static_feedback: bool
    disclosure: dict[str, Any]
    shape_model: dict[str, Any]
    logical_spec: dict[str, Any]
    physical_plan: dict[str, Any]
    feedback: list[dict[str, Any]]
    static_feedback: list[dict[str, Any]]
    transcript_refs: list[str]
    diagnostics_refs: list[str]
    events_path: str
    anomalies_path: str
    result_type: str = "ablation_prediction"
    status: str = "ok"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AblationFailure:
    run_id: str
    ablation_id: str
    ablation_title: str
    solver_variant: str
    baseline_id: None
    record_id: int | None
    db_id: str
    error_code: str
    message: str
    attempts: int
    r_max: int
    witness_k: int
    prompt_witness_sample_count_by_collection: dict[str, int]
    uses_shape_model: bool
    uses_variant_handling: bool
    uses_clause_coverage: bool
    uses_preserve_guard: bool
    uses_per_stage: bool
    uses_variant_strata: bool
    uses_execution_feedback: bool
    uses_static_feedback: bool
    disclosure: dict[str, Any]
    shape_model: dict[str, Any] = field(default_factory=dict)
    logical_spec: dict[str, Any] = field(default_factory=dict)
    physical_plan: dict[str, Any] = field(default_factory=dict)
    feedback: list[dict[str, Any]] = field(default_factory=list)
    static_feedback: list[dict[str, Any]] = field(default_factory=list)
    transcript_refs: list[str] = field(default_factory=list)
    diagnostics_refs: list[str] = field(default_factory=list)
    events_path: str = "events.jsonl"
    anomalies_path: str = "anomalies.jsonl"
    result_type: str = "ablation_failure"
    status: str = "failed"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


async def run_ablation_suite(
    wf: Workflow,
    *,
    dataset_dir: Path,
    ablation_selection: str | list[str] | tuple[str, ...] | None = "all",
    db_id: str | None = None,
    nlq: str | None = None,
    nlq_track: NlqTrack = "record",
    record_id: int | None = None,
    limit: int = 1,
    r_max: int = 2,
    witness_k: int = 3,
) -> list[dict[str, Any]]:
    specs = resolve_ablations(ablation_selection)
    if nlq is not None:
        if not db_id:
            raise SourceError("NLQ+DB ablation mode requires --db-id")
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
    log = wf.ctx.log.bind(component="ablation_suite")
    log.info(
        "ablation_suite_start",
        ablations=[spec.id for spec in specs],
        records=len(inputs),
        dataset_dir=str(dataset_dir),
        db_id=db_id,
        record_id=record_id,
        r_max=r_max,
        witness_k=witness_k,
        input_mode="nlq_db" if nlq is not None else "release",
    )
    if not inputs:
        log.anomaly(
            kind=Anomaly.SUPPLY_EXHAUSTED,
            message="no ablation records matched filters",
            dataset_dir=str(dataset_dir),
            db_id=db_id,
            record_id=record_id,
        )
        return []

    if wf.ctx.progress:
        wf.ctx.progress.phase("ABLATION")

    if nlq is not None and db_id:
        preloaded_dbs = {str(db_id)}
    else:
        preloaded_dbs = await _preload_ablation_witnesses(wf, inputs, log)

    work: list[
        tuple[int, AblationSpec, dict[str, Any], dict[str, Any], dict[str, Any] | None]
    ] = []
    for record, schema, data in inputs:
        for spec in specs:
            work.append((len(work), spec, record, schema, data))

    async def run_one(
        batch_index: int,
        spec: AblationSpec,
        record: dict[str, Any],
        schema: dict[str, Any],
        data: dict[str, list[dict[str, Any]]] | None,
    ) -> tuple[int, dict[str, Any]]:
        result = await run_ablation_record(
            wf,
            spec,
            record,
            schema,
            local_data=data,
            r_max=r_max,
            witness_k=witness_k,
            batch_index=batch_index,
            witness_preloaded=str(record.get("db_id") or "") in preloaded_dbs,
        )
        payload = result.to_json()
        payload["batch_index"] = batch_index
        payload["work_item_id"] = (
            f"ablation:{batch_index}:{spec.id}:{record.get('db_id')}:"
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
                log.anomaly(wrap_unexpected(exc, stage="ablation_suite_gather"))
        raise
    outputs = [payload for _, payload in sorted(completed, key=lambda item: item[0])]
    log.info("ablation_suite_done", outputs=len(outputs), ablations=len(specs))
    return outputs


async def _preload_ablation_witnesses(
    wf: Workflow,
    inputs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]],
    log: Any,
) -> set[str]:
    """Load each unique db witness exactly once before the (record x spec) fan-out.

    All work items gather concurrently and share the run+db-scoped working database, so a
    per-record reload would race ``drop_database``/``insert_many`` against ``norm_exec``.
    Preloading once mirrors the solver ``solve`` path and lets each record pass
    ``witness_preloaded=True``.
    """
    if wf.ctx.settings.stub or wf.ctx.mongo is None or not wf.ctx.mongo.available():
        return set()
    by_db: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for record, _schema, data in inputs:
        db = str(record.get("db_id") or "")
        if db and data and db not in by_db:
            by_db[db] = data
    if not by_db:
        return set()

    async def load_one(db: str, data: dict[str, list[dict[str, Any]]]) -> str:
        await asyncio.to_thread(wf.ctx.mongo.load_witness, db, data)
        return db

    loaded = await asyncio.gather(
        *(load_one(db, data) for db, data in sorted(by_db.items()))
    )
    log.info("ablation_witness_preloaded", db_ids=list(loaded), db_count=len(loaded))
    return set(loaded)


async def run_ablation_record(
    wf: Workflow,
    spec: AblationSpec,
    record: dict[str, Any],
    schema: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    r_max: int = 2,
    witness_k: int = 3,
    batch_index: int | None = None,
    witness_preloaded: bool = False,
) -> AblationPrediction | AblationFailure:
    db_id = str(record.get("db_id") or "")
    record_id = record.get("record_id")
    options = _runtime_options(spec, r_max=r_max, witness_k=witness_k,
                               batch_index=batch_index)
    base_log = wf.ctx.log.bind(
        component="ablation_runner",
        ablation_id=spec.id,
        solver_variant=options.solver_variant,
        db_id=db_id,
        record_id=record_id,
        batch_index=batch_index,
    )
    base_log.info(
        "ablation_record_start",
        title=spec.title,
        limitations=list(spec.limitations),
        solver_options=options.to_json(),
    )

    variant_wf = _variant_workflow(wf, spec, options, batch_index)
    try:
        result = await smart_solve_record(
            variant_wf,
            record,
            schema,
            local_data=local_data,
            r_max=r_max,
            witness_k=witness_k,
            options=options,
            witness_preloaded=witness_preloaded,
        )
        if isinstance(result, SolverFailure):
            payload = result.to_json()
            refs = _llm_refs_for(wf, spec.id, db_id, record_id)
            failure = _failure_from_solver_payload(
                wf,
                spec,
                options,
                payload,
                local_data=local_data,
                transcript_refs=refs["transcript_refs"],
                diagnostics_refs=refs["diagnostics_refs"],
            )
            base_log.warning(
                "ablation_record_done",
                status="failed",
                error_code=failure.error_code,
                attempts=failure.attempts,
            )
            return failure

        payload = result.to_json()
        refs = _llm_refs_for(wf, spec.id, db_id, record_id)
        prediction = _prediction_from_solver_payload(
            wf,
            spec,
            options,
            payload,
            local_data=local_data,
            transcript_refs=refs["transcript_refs"],
            diagnostics_refs=refs["diagnostics_refs"],
        )
        base_log.info(
            "ablation_record_done",
            status="ok",
            attempts=prediction.attempts,
            mql_preview=prediction.MQL[:240],
        )
        return prediction
    except TendError as err:
        err.with_context(ablation_id=spec.id, db_id=db_id, record_id=record_id)
        if not err.logged:
            base_log.anomaly(err)
        refs = _llm_refs_for(wf, spec.id, db_id, record_id)
        failure = _failure_from_error(
            wf,
            spec,
            options,
            err,
            db_id=db_id,
            record_id=record_id,
            local_data=local_data,
            transcript_refs=refs["transcript_refs"],
            diagnostics_refs=refs["diagnostics_refs"],
        )
        base_log.error(
            "ablation_record_done",
            status="failed",
            error_code=failure.error_code,
            message=failure.message,
        )
        return failure
    except Exception as exc:  # noqa: BLE001 - ablations should continue across variants
        err = wrap_unexpected(
            exc,
            ablation_id=spec.id,
            db_id=db_id,
            record_id=record_id,
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        base_log.anomaly(err)
        refs = _llm_refs_for(wf, spec.id, db_id, record_id)
        return _failure_from_error(
            wf,
            spec,
            options,
            err,
            db_id=db_id,
            record_id=record_id,
            local_data=local_data,
            transcript_refs=refs["transcript_refs"],
            diagnostics_refs=refs["diagnostics_refs"],
        )


def _runtime_options(
    spec: AblationSpec,
    *,
    r_max: int,
    witness_k: int,
    batch_index: int | None = None,
) -> SmartSolveOptions:
    options = spec.options
    effective_r_max = options.r_max if options.r_max is not None else r_max
    effective_witness_k = options.witness_k if options.witness_k is not None else witness_k
    prefix = (
        f"ablation:{batch_index}:{spec.id}"
        if batch_index is not None
        else f"ablation:{spec.id}"
    )
    return replace(
        options,
        solver_variant=spec.id,
        r_max=max(0, effective_r_max),
        witness_k=max(0, effective_witness_k),
        progress_group_prefix=prefix,
        progress_work_item_id=(
            f"{spec.id}:{batch_index}" if batch_index is not None else spec.id
        ),
    )


def _variant_workflow(
    wf: Workflow,
    spec: AblationSpec,
    options: SmartSolveOptions,
    batch_index: int | None = None,
) -> Workflow:
    ctx = replace(
        wf.ctx,
        log=wf.ctx.log.bind(
            ablation_id=spec.id,
            solver_variant=options.solver_variant,
            batch_index=batch_index,
        ),
        extra={
            **wf.ctx.extra,
            "ablation_id": spec.id,
            "batch_index": batch_index,
            "solver_options": options.to_json(),
        },
    )
    return Workflow(ctx)


def _prediction_from_solver_payload(
    wf: Workflow,
    spec: AblationSpec,
    options: SmartSolveOptions,
    payload: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None,
    transcript_refs: list[str],
    diagnostics_refs: list[str],
) -> AblationPrediction:
    logical = dict(payload.get("logical_spec") or {})
    physical = dict(payload.get("physical_plan") or {})
    feedback = list(payload.get("feedback") or [])
    mql = str(payload.get("MQL") or "")
    return AblationPrediction(
        run_id=wf.ctx.settings.run_id,
        ablation_id=spec.id,
        ablation_title=spec.title,
        solver_variant=options.solver_variant,
        baseline_id=None,
        record_id=payload.get("record_id"),
        db_id=str(payload.get("db_id") or ""),
        MQL=mql,
        attempts=_attempt_count(feedback),
        r_max=int(options.r_max or 0),
        witness_k=int(options.witness_k or 0),
        prompt_witness_sample_count_by_collection=_witness_counts(local_data, options),
        uses_shape_model=options.use_shape_comprehension,
        uses_variant_handling=bool(physical.get("variant_handling")),
        uses_clause_coverage=bool(logical.get("clause_coverage")),
        uses_preserve_guard=options.use_preserve_guard,
        uses_per_stage=options.execution_mode == "per_stage",
        uses_variant_strata=options.use_variant_stratification,
        uses_execution_feedback=options.execution_mode in {"per_stage", "whole_query"},
        uses_static_feedback=options.execution_mode == "static",
        disclosure=_disclosure(spec, options, payload.get("disclosure") or {}),
        shape_model=dict(payload.get("shape_model") or {}),
        logical_spec=logical,
        physical_plan=physical,
        feedback=feedback,
        static_feedback=static_mql_feedback(mql),
        transcript_refs=transcript_refs,
        diagnostics_refs=diagnostics_refs,
        events_path="events.jsonl",
        anomalies_path="anomalies.jsonl",
    )


def _failure_from_solver_payload(
    wf: Workflow,
    spec: AblationSpec,
    options: SmartSolveOptions,
    payload: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None,
    transcript_refs: list[str],
    diagnostics_refs: list[str],
) -> AblationFailure:
    feedback = list(payload.get("feedback") or [])
    physical = dict(payload.get("physical_plan") or {})
    logical = dict(payload.get("logical_spec") or {})
    static_feedback = static_mql_feedback(str(payload.get("MQL") or ""))
    return AblationFailure(
        run_id=wf.ctx.settings.run_id,
        ablation_id=spec.id,
        ablation_title=spec.title,
        solver_variant=options.solver_variant,
        baseline_id=None,
        record_id=payload.get("record_id"),
        db_id=str(payload.get("db_id") or ""),
        error_code=str(payload.get("error_code") or "SOLVER_FAILURE"),
        message=str(payload.get("message") or "solver returned failure"),
        attempts=_attempt_count(feedback),
        r_max=int(options.r_max or 0),
        witness_k=int(options.witness_k or 0),
        prompt_witness_sample_count_by_collection=_witness_counts(local_data, options),
        uses_shape_model=options.use_shape_comprehension,
        uses_variant_handling=bool(physical.get("variant_handling")),
        uses_clause_coverage=bool(logical.get("clause_coverage")),
        uses_preserve_guard=options.use_preserve_guard,
        uses_per_stage=options.execution_mode == "per_stage",
        uses_variant_strata=options.use_variant_stratification,
        uses_execution_feedback=options.execution_mode in {"per_stage", "whole_query"},
        uses_static_feedback=options.execution_mode == "static",
        disclosure=_disclosure(spec, options, payload.get("disclosure") or {}),
        shape_model=dict(payload.get("shape_model") or {}),
        logical_spec=logical,
        physical_plan=physical,
        feedback=feedback,
        static_feedback=static_feedback,
        transcript_refs=transcript_refs,
        diagnostics_refs=diagnostics_refs,
    )


def _failure_from_error(
    wf: Workflow,
    spec: AblationSpec,
    options: SmartSolveOptions,
    err: TendError,
    *,
    db_id: str,
    record_id: int | None,
    local_data: dict[str, list[dict[str, Any]]] | None,
    transcript_refs: list[str],
    diagnostics_refs: list[str],
) -> AblationFailure:
    return AblationFailure(
        run_id=wf.ctx.settings.run_id,
        ablation_id=spec.id,
        ablation_title=spec.title,
        solver_variant=options.solver_variant,
        baseline_id=None,
        record_id=record_id,
        db_id=db_id,
        error_code=err.anomaly.value if err.anomaly else "tend_error",
        message=err.message,
        # attempts=0 is a sentinel: the error fired pre-loop / before any solve attempt, so
        # the true attempt count is unknown (distinct from a genuine single-attempt run).
        attempts=0,
        r_max=int(options.r_max or 0),
        witness_k=int(options.witness_k or 0),
        prompt_witness_sample_count_by_collection=_witness_counts(local_data, options),
        uses_shape_model=options.use_shape_comprehension,
        uses_variant_handling=False,
        uses_clause_coverage=False,
        uses_preserve_guard=options.use_preserve_guard,
        uses_per_stage=options.execution_mode == "per_stage",
        uses_variant_strata=options.use_variant_stratification,
        uses_execution_feedback=options.execution_mode in {"per_stage", "whole_query"},
        uses_static_feedback=options.execution_mode == "static",
        disclosure=_disclosure(spec, options, {}),
        transcript_refs=transcript_refs,
        diagnostics_refs=diagnostics_refs,
    )


def _disclosure(
    spec: AblationSpec,
    options: SmartSolveOptions,
    solver_disclosure: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ablation_id": spec.id,
        "ablation_title": spec.title,
        "solver_variant": options.solver_variant,
        "options": options.to_json(),
        "limitations": list(spec.limitations),
        # Hoist the comparable fields to the top level so ablation disclosures line up
        # with the baseline _baseline_disclosure shape; the full object stays nested below.
        "backbone": solver_disclosure.get("backbone"),
        "disjointness_ok": solver_disclosure.get("disjointness_ok"),
        "s_solver": solver_disclosure.get("s_solver"),
        "r_max": solver_disclosure.get("r_max"),
        "witness_k": solver_disclosure.get("witness_k"),
        "no_training": solver_disclosure.get("no_training"),
        "solver_disclosure": solver_disclosure,
    }


def _attempt_count(feedback: list[dict[str, Any]]) -> int:
    # NOTE: changes measured behavior; affected ablation/leaderboard numbers need re-run (review fix H6)
    # feedback_log holds only failed attempts; the winning (final) attempt is not appended,
    # so the total attempt count is the number of failed attempts plus the successful one.
    if not feedback:
        return 1
    return len(feedback) + 1


def _witness_counts(
    local_data: dict[str, list[dict[str, Any]]] | None,
    options: SmartSolveOptions,
) -> dict[str, int]:
    digest = build_witness_digest(local_data if int(options.witness_k or 0) > 0 else None,
                                  int(options.witness_k or 0))
    return {
        str(collection): int(info.get("sample_count") or 0)
        for collection, info in digest.items()
        if isinstance(info, dict)
    }


def _llm_refs_for(
    wf: Workflow,
    ablation_id: str,
    db_id: str,
    record_id: int | None,
) -> dict[str, list[str]]:
    events_path = wf.ctx.log.run_dir / "events.jsonl"
    transcript_refs: list[str] = []
    diagnostics_refs: list[str] = []
    if not events_path.exists():
        return {"transcript_refs": transcript_refs, "diagnostics_refs": diagnostics_refs}
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("ablation_id") != ablation_id:
            continue
        if event.get("db_id") != db_id or event.get("record_id") != record_id:
            continue
        transcript_ref = event.get("transcript_ref")
        diagnostics_ref = event.get("diagnostics_ref")
        if isinstance(transcript_ref, str) and transcript_ref not in transcript_refs:
            transcript_refs.append(transcript_ref)
        if isinstance(diagnostics_ref, str) and diagnostics_ref not in diagnostics_refs:
            diagnostics_refs.append(diagnostics_ref)
    return {"transcript_refs": transcript_refs, "diagnostics_refs": diagnostics_refs}


__all__ = [
    "AblationFailure",
    "AblationPrediction",
    "run_ablation_record",
    "run_ablation_suite",
]
