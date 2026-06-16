"""Runtime workflow for SAG solver ablation studies.

Each (record × arm) work item runs the SAG runtime with that arm's canonical
policy (``sag_card1`` / ``sag_gate`` / ``sag_v2`` / the ``sag_full`` reference).
The suite owns one shared :class:`GroundingIndexCache` so every arm and record
reuses each db's induced index, and preloads witnesses once per db (the shared
working database is reloaded per db, never per record — the preload races of the
per-record era are structurally excluded).

Observability is DynaDB-style (``tend.utils.logging``): each arm is one stage, so
a record's solve lives in ``<arm>/<db_id>/<record_id>.log`` with its agent session
under ``<arm>/llm/``; suite lifecycle events go through the ``ablation`` stage
logger and anomalies through ``LogManager.log_exception_event`` (errors.jsonl).
"""
from __future__ import annotations

import asyncio
import hashlib
import traceback
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..errors import SourceError, TendError, wrap_unexpected
from ..execution.ast_check import static_mql_feedback
from ..solver.inputs import (
    DEFAULT_INPUT_SAMPLE_SIZE,
    NlqTrack,
    _canonical_nlq,
    build_nlq_db_solver_input,
    load_solver_release_inputs,
)
from ..solver.sag import GroundingIndexCache, sag_solve_record
from ..solver.sag.runtime import _session_ref, log_sag_anomaly
from ..utils.logging import TaskLogger
from ..workflow import Workflow
from .strategies import SagAblationSpec, resolve_ablations


@dataclass(frozen=True, slots=True)
class AblationPrediction:
    run_id: str
    session_id: str
    ablation_id: str
    ablation_title: str
    solver_variant: str
    baseline_id: None
    batch_index: int | None
    work_item_id: str
    record_id: int | str | None
    db_id: str
    input_mode: str
    nlq_track: str
    nlq_hash: str | None
    witness_k: int
    evaluation_skip_reason: str | None
    MQL: str
    attempts: int
    arm: str
    k_consistency: int
    max_repair_rounds: int
    disclosure: dict[str, Any]
    feedback: list[dict[str, Any]] = field(default_factory=list)
    static_feedback: list[dict[str, Any]] = field(default_factory=list)
    agent_session_ref: str | None = None
    result_type: str = "ablation_prediction"
    status: str = "ok"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AblationFailure:
    run_id: str
    session_id: str
    ablation_id: str
    ablation_title: str
    solver_variant: str
    baseline_id: None
    batch_index: int | None
    work_item_id: str
    record_id: int | str | None
    db_id: str
    input_mode: str
    nlq_track: str
    nlq_hash: str | None
    witness_k: int
    evaluation_skip_reason: str | None
    error_code: str
    message: str
    attempts: int
    arm: str
    k_consistency: int
    max_repair_rounds: int
    disclosure: dict[str, Any]
    MQL: str = ""
    feedback: list[dict[str, Any]] = field(default_factory=list)
    static_feedback: list[dict[str, Any]] = field(default_factory=list)
    agent_session_ref: str | None = None
    result_type: str = "ablation_failure"
    status: str = "failed"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _task_id(db_id: Any, record_id: Any) -> str:
    return f"{_trace_part(db_id)}/{_trace_part(record_id)}"


def _suite_logger(wf: Workflow) -> Any:
    """Stage logger for suite-level lifecycle events (DynaDB pattern)."""
    log_mgr = getattr(wf.ctx, "log_mgr", None)
    if log_mgr is not None:
        return log_mgr.get_stage_logger("ablation")
    return wf.ctx.log


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
    witness_k: int = DEFAULT_INPUT_SAMPLE_SIZE,
    workers: int = 1,
    policy_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    specs = resolve_ablations(ablation_selection)
    suite_workers = max(1, int(workers))
    input_mode = "nlq_db" if nlq is not None else "release"
    effective_nlq_track: NlqTrack = "canonical" if nlq is not None else nlq_track
    evaluation_skip_reason = "no_release_dataset" if nlq is not None else None
    if nlq is not None:
        if not db_id:
            raise SourceError("NLQ+DB ablation mode requires --db-id")
        runtime_input = await build_nlq_db_solver_input(
            wf,
            db_id=str(db_id),
            nlq=nlq,
            record_id=record_id,
            sample_size=witness_k,
        )
        inputs = [(runtime_input.record, runtime_input.schema, runtime_input.local_data)]
    else:
        inputs = load_solver_release_inputs(
            dataset_dir,
            db_id=db_id,
            record_id=record_id,
            limit=limit,
            nlq_track=effective_nlq_track,
        )
    nlq_hash = _nlq_hash(nlq) if nlq is not None else None
    log_mgr = getattr(wf.ctx, "log_mgr", None)
    suite_log = _suite_logger(wf)
    suite_log.info(
        "ablation_suite_start",
        ablations=[spec.id for spec in specs],
        arms={spec.id: spec.arm for spec in specs},
        policy_overrides=dict(policy_overrides or {}),
        records=len(inputs),
        dataset_dir=str(dataset_dir),
        db_id=db_id,
        record_id=record_id,
        input_mode=input_mode,
        nlq_track=effective_nlq_track,
        nlq_hash=nlq_hash,
        witness_k=witness_k,
        workers=suite_workers,
        evaluation_skip_reason=evaluation_skip_reason,
    )
    if not inputs:
        log_sag_anomaly(
            log_mgr,
            "ablation_no_records",
            SourceError(
                "no ablation records matched filters",
                context={
                    "dataset_dir": str(dataset_dir),
                    "db_id": db_id,
                    "record_id": record_id,
                },
            ),
            stage="ablation",
            task_id=_task_id(db_id, record_id),
        )
        suite_log.warning(
            "ablation_no_records",
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
        preloaded_dbs = await _preload_ablation_witnesses(wf, inputs, suite_log)

    # One induced index per db for the whole suite: every arm shares it.
    index_cache = GroundingIndexCache(wf.ctx.mongo, wf.ctx.settings, suite_log)

    work: list[
        tuple[int, SagAblationSpec, dict[str, Any], dict[str, Any], dict[str, Any] | None]
    ] = []
    for record, schema, data in inputs:
        for spec in specs:
            work.append((len(work), spec, record, schema, data))

    async def run_one(
        batch_index: int,
        spec: SagAblationSpec,
        record: dict[str, Any],
        schema: dict[str, Any],
        data: dict[str, list[dict[str, Any]]] | None,
    ) -> tuple[int, dict[str, Any]]:
        db = str(record.get("db_id") or "")
        rid = record.get("record_id")
        work_item_id = _work_item_id(spec.id, batch_index, db, rid)
        try:
            result = await run_ablation_record(
                wf,
                spec,
                record,
                schema,
                local_data=data,
                index_cache=index_cache,
                batch_index=batch_index,
                witness_preloaded=db in preloaded_dbs,
                input_mode=input_mode,
                nlq_track=effective_nlq_track,
                nlq_hash=nlq_hash,
                witness_k=witness_k,
                evaluation_skip_reason=evaluation_skip_reason,
                policy_overrides=policy_overrides,
            )
            payload = result.to_json() if hasattr(result, "to_json") else dict(result)
            if not isinstance(payload, dict):
                raise TypeError(
                    f"ablation result serialized to {type(payload).__name__}, expected dict"
                )
        except TendError as err:
            err.with_context(
                ablation_id=spec.id,
                db_id=db,
                record_id=rid,
                batch_index=batch_index,
            )
            if not err.logged:
                log_sag_anomaly(
                    log_mgr,
                    "ablation_worker_failed",
                    err,
                    stage=spec.id,
                    task_id=_task_id(db, rid),
                )
            payload = _ablation_worker_failure_payload(
                wf,
                spec,
                err,
                db_id=db,
                record_id=rid,
                batch_index=batch_index,
                input_mode=input_mode,
                nlq_track=effective_nlq_track,
                nlq_hash=nlq_hash,
                witness_k=witness_k,
                evaluation_skip_reason=evaluation_skip_reason,
            )
        except Exception as exc:  # noqa: BLE001 - one wrapper failure is one row
            err = wrap_unexpected(
                exc,
                stage="ablation_worker",
                ablation_id=spec.id,
                db_id=db,
                record_id=rid,
                batch_index=batch_index,
                traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            )
            log_sag_anomaly(
                log_mgr,
                "ablation_worker_failed",
                err,
                stage=spec.id,
                task_id=_task_id(db, rid),
            )
            payload = _ablation_worker_failure_payload(
                wf,
                spec,
                err,
                db_id=db,
                record_id=rid,
                batch_index=batch_index,
                input_mode=input_mode,
                nlq_track=effective_nlq_track,
                nlq_hash=nlq_hash,
                witness_k=witness_k,
                evaluation_skip_reason=evaluation_skip_reason,
            )
        payload["batch_index"] = batch_index
        payload["work_item_id"] = work_item_id
        payload.setdefault("session_id", _session_id(work_item_id))
        return batch_index, payload

    semaphore = asyncio.Semaphore(suite_workers)

    async def run_guarded(
        batch_index: int,
        spec: SagAblationSpec,
        record: dict[str, Any],
        schema: dict[str, Any],
        data: dict[str, list[dict[str, Any]]] | None,
    ) -> tuple[int, dict[str, Any]]:
        async with semaphore:
            return await run_one(batch_index, spec, record, schema, data)

    completed = await asyncio.gather(
        *(
            run_guarded(batch_index, spec, record, schema, data)
            for batch_index, spec, record, schema, data in work
        )
    )
    outputs = [payload for _, payload in sorted(completed, key=lambda item: item[0])]
    suite_log.info("ablation_suite_done", outputs=len(outputs), ablations=len(specs))
    return outputs


def _ablation_worker_failure_payload(
    wf: Workflow,
    spec: SagAblationSpec,
    err: TendError,
    *,
    db_id: str,
    record_id: int | str | None,
    batch_index: int,
    input_mode: str,
    nlq_track: NlqTrack,
    nlq_hash: str | None,
    witness_k: int,
    evaluation_skip_reason: str | None,
) -> dict[str, Any]:
    options = _runtime_options(
        spec,
        batch_index=batch_index,
        db_id=db_id,
        record_id=record_id,
        input_mode=input_mode,
        nlq_track=nlq_track,
        nlq_hash=nlq_hash,
        witness_k=witness_k,
        evaluation_skip_reason=evaluation_skip_reason,
    )
    failure = _failure_from_error(
        wf,
        spec,
        options,
        err,
        db_id=db_id,
        record_id=record_id,
    )
    return failure.to_json()


async def _preload_ablation_witnesses(
    wf: Workflow,
    inputs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]],
    log: Any,
) -> set[str]:
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
    spec: SagAblationSpec,
    record: dict[str, Any],
    schema: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    index_cache: GroundingIndexCache | None = None,
    batch_index: int | None = None,
    witness_preloaded: bool = False,
    input_mode: str = "release",
    nlq_track: NlqTrack = "record",
    nlq_hash: str | None = None,
    witness_k: int = DEFAULT_INPUT_SAMPLE_SIZE,
    evaluation_skip_reason: str | None = None,
    policy_overrides: dict[str, Any] | None = None,
) -> AblationPrediction | AblationFailure:
    db_id = str(record.get("db_id") or "")
    record_id = record.get("record_id")
    effective_nlq_hash = nlq_hash if nlq_hash is not None else _nlq_hash_from_record(record)
    policy = spec.to_policy(overrides=policy_overrides)
    options = _runtime_options(
        spec,
        policy=policy,
        batch_index=batch_index,
        db_id=db_id,
        record_id=record_id,
        input_mode=input_mode,
        nlq_track=nlq_track,
        nlq_hash=effective_nlq_hash,
        witness_k=witness_k,
        evaluation_skip_reason=evaluation_skip_reason,
    )
    log_mgr = getattr(wf.ctx, "log_mgr", None)
    task_id = _task_id(db_id, record_id)
    # Stage = the ablation arm name: yields <arm>/<db_id>/<record_id>.log with
    # the record's agent session under <arm>/llm/ (DynaDB layout).
    task_log: TaskLogger | None = (
        log_mgr.get_task_logger(spec.id, task_id) if log_mgr is not None else None
    )

    def record_event(event: str, *, _level: str = "info", **kw: Any) -> None:
        sink = task_log if task_log is not None else wf.ctx.log
        method = getattr(sink, _level, None)
        if callable(method):
            method(
                event,
                ablation_id=spec.id,
                solver_variant=options["solver_variant"],
                db_id=db_id,
                record_id=record_id,
                batch_index=batch_index,
                **kw,
            )

    record_event(
        "ablation_record_start",
        title=spec.title,
        arm=spec.arm,
        limitations=list(spec.limitations),
        solver_options=options,
    )

    variant_wf = _variant_workflow(wf, spec, options, batch_index)
    try:
        _canonical_nlq(record)
        result = await sag_solve_record(
            variant_wf,
            record,
            schema,
            local_data=local_data,
            policy=policy,
            index_cache=index_cache,
            witness_preloaded=witness_preloaded,
            stage=spec.id,
            task_log=task_log,
        )
        payload = result.to_json() if hasattr(result, "to_json") else dict(result)
        if payload.get("result_type") == "solver_failure":
            failure = _failure_from_solver_payload(wf, spec, options, payload)
            record_event(
                "ablation_record_done",
                _level="warning",
                status="failed",
                error_code=failure.error_code,
                attempts=failure.attempts,
                agent_session_ref=failure.agent_session_ref,
            )
            return failure

        prediction = _prediction_from_solver_payload(wf, spec, options, payload)
        record_event(
            "ablation_record_done",
            status="ok",
            attempts=prediction.attempts,
            mql_preview=prediction.MQL[:240],
            agent_session_ref=prediction.agent_session_ref,
        )
        return prediction
    except TendError as err:
        err.with_context(ablation_id=spec.id, db_id=db_id, record_id=record_id)
        if not err.logged:
            log_sag_anomaly(
                log_mgr,
                "ablation_record_failed",
                err,
                stage=spec.id,
                task_id=task_id,
                session_path=getattr(task_log, "agent_session_path", None),
            )
        failure = _failure_from_error(
            wf,
            spec,
            options,
            err,
            db_id=db_id,
            record_id=record_id,
            agent_session_ref=_session_ref(task_log, log_mgr) or None,
        )
        record_event(
            "ablation_record_done",
            _level="error",
            status="failed",
            error_code=failure.error_code,
            message=failure.message,
        )
        return failure
    except Exception as exc:
        err = wrap_unexpected(
            exc,
            ablation_id=spec.id,
            db_id=db_id,
            record_id=record_id,
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        log_sag_anomaly(
            log_mgr,
            "ablation_record_failed",
            err,
            stage=spec.id,
            task_id=task_id,
            session_path=getattr(task_log, "agent_session_path", None),
        )
        return _failure_from_error(
            wf,
            spec,
            options,
            err,
            db_id=db_id,
            record_id=record_id,
            agent_session_ref=_session_ref(task_log, log_mgr) or None,
        )


def _runtime_options(
    spec: SagAblationSpec,
    *,
    policy: Any | None = None,
    batch_index: int | None = None,
    db_id: str | None = None,
    record_id: int | str | None = None,
    input_mode: str = "release",
    nlq_track: NlqTrack = "record",
    nlq_hash: str | None = None,
    witness_k: int = DEFAULT_INPUT_SAMPLE_SIZE,
    evaluation_skip_reason: str | None = None,
) -> dict[str, Any]:
    prefix = (
        f"ablation:{batch_index}:{spec.id}"
        if batch_index is not None
        else f"ablation:{spec.id}"
    )
    work_item_id = _work_item_id(spec.id, batch_index, db_id, record_id)
    options = spec.to_runtime_options(
        progress_group_prefix=prefix,
        progress_work_item_id=work_item_id,
        policy=policy,
    )
    options.update(
        {
            "batch_index": batch_index,
            "db_id": db_id,
            "record_id": record_id,
            "input_mode": input_mode,
            "nlq_track": str(nlq_track),
            "nlq_hash": nlq_hash,
            "witness_k": max(0, int(witness_k)),
            "evaluation_skip_reason": evaluation_skip_reason,
            "work_item_id": work_item_id,
            "session_id": _session_id(work_item_id),
        }
    )
    return options


def _variant_workflow(
    wf: Workflow,
    spec: SagAblationSpec,
    options: dict[str, Any],
    batch_index: int | None = None,
) -> Workflow:
    ctx = replace(
        wf.ctx,
        extra={
            **wf.ctx.extra,
            "ablation_id": spec.id,
            "batch_index": batch_index,
            "session_id": options.get("session_id"),
            "work_item_id": options.get("work_item_id"),
            "solver_options": options,
        },
    )
    return Workflow(ctx)


def _prediction_from_solver_payload(
    wf: Workflow,
    spec: SagAblationSpec,
    options: dict[str, Any],
    payload: dict[str, Any],
) -> AblationPrediction:
    mql = str(payload.get("MQL") or "")
    db_id = str(payload.get("db_id") or "")
    record_id = payload.get("record_id")
    trace = _trace_fields(spec, options, db_id=db_id, record_id=record_id)
    return AblationPrediction(
        run_id=wf.ctx.settings.run_id,
        session_id=trace["session_id"],
        ablation_id=spec.id,
        ablation_title=spec.title,
        solver_variant=str(options["solver_variant"]),
        baseline_id=None,
        batch_index=trace["batch_index"],
        work_item_id=trace["work_item_id"],
        record_id=record_id,
        db_id=db_id,
        input_mode=_provenance_str(options, payload, "input_mode", default="release"),
        nlq_track=_provenance_str(options, payload, "nlq_track", default="record"),
        nlq_hash=_optional_str(options.get("nlq_hash") or payload.get("nlq_hash")),
        witness_k=_provenance_int(options, payload, "witness_k"),
        evaluation_skip_reason=_optional_str(
            options.get("evaluation_skip_reason") or payload.get("evaluation_skip_reason")
        ),
        MQL=mql,
        attempts=_attempt_count(payload),
        arm=str(options["arm"]),
        k_consistency=int(options["k_consistency"]),
        max_repair_rounds=int(options["max_repair_rounds"]),
        disclosure=_disclosure(spec, options, payload.get("disclosure") or {}),
        static_feedback=static_mql_feedback(mql),
        agent_session_ref=_optional_str(payload.get("agent_session_ref")),
    )


def _failure_from_solver_payload(
    wf: Workflow,
    spec: SagAblationSpec,
    options: dict[str, Any],
    payload: dict[str, Any],
) -> AblationFailure:
    mql = str(payload.get("MQL") or "")
    db_id = str(payload.get("db_id") or "")
    record_id = payload.get("record_id")
    trace = _trace_fields(spec, options, db_id=db_id, record_id=record_id)
    return AblationFailure(
        run_id=wf.ctx.settings.run_id,
        session_id=trace["session_id"],
        ablation_id=spec.id,
        ablation_title=spec.title,
        solver_variant=str(options["solver_variant"]),
        baseline_id=None,
        batch_index=trace["batch_index"],
        work_item_id=trace["work_item_id"],
        record_id=record_id,
        db_id=db_id,
        input_mode=_provenance_str(options, payload, "input_mode", default="release"),
        nlq_track=_provenance_str(options, payload, "nlq_track", default="record"),
        nlq_hash=_optional_str(options.get("nlq_hash") or payload.get("nlq_hash")),
        witness_k=_provenance_int(options, payload, "witness_k"),
        evaluation_skip_reason=_optional_str(
            options.get("evaluation_skip_reason") or payload.get("evaluation_skip_reason")
        ),
        error_code=str(payload.get("error_code") or "SOLVER_FAILURE"),
        message=str(payload.get("message") or "solver returned failure"),
        attempts=_attempt_count(payload),
        arm=str(options["arm"]),
        k_consistency=int(options["k_consistency"]),
        max_repair_rounds=int(options["max_repair_rounds"]),
        disclosure=_disclosure(spec, options, payload.get("disclosure") or {}),
        MQL=mql,
        static_feedback=static_mql_feedback(mql),
        agent_session_ref=_optional_str(payload.get("agent_session_ref")),
    )


def _failure_from_error(
    wf: Workflow,
    spec: SagAblationSpec,
    options: dict[str, Any],
    err: TendError,
    *,
    db_id: str,
    record_id: int | str | None,
    agent_session_ref: str | None = None,
) -> AblationFailure:
    trace = _trace_fields(spec, options, db_id=db_id, record_id=record_id)
    return AblationFailure(
        run_id=wf.ctx.settings.run_id,
        session_id=trace["session_id"],
        ablation_id=spec.id,
        ablation_title=spec.title,
        solver_variant=str(options["solver_variant"]),
        baseline_id=None,
        batch_index=trace["batch_index"],
        work_item_id=trace["work_item_id"],
        record_id=record_id,
        db_id=db_id,
        input_mode=str(options.get("input_mode") or "release"),
        nlq_track=str(options.get("nlq_track") or "record"),
        nlq_hash=_optional_str(options.get("nlq_hash")),
        witness_k=max(0, int(options.get("witness_k") or 0)),
        evaluation_skip_reason=_optional_str(options.get("evaluation_skip_reason")),
        error_code=err.anomaly.value if err.anomaly else "tend_error",
        message=err.message,
        attempts=0,
        arm=str(options["arm"]),
        k_consistency=int(options["k_consistency"]),
        max_repair_rounds=int(options["max_repair_rounds"]),
        disclosure=_disclosure(spec, options, {}),
        agent_session_ref=agent_session_ref,
    )


def _disclosure(
    spec: SagAblationSpec,
    options: dict[str, Any],
    solver_disclosure: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ablation_id": spec.id,
        "ablation_title": spec.title,
        "solver_variant": options["solver_variant"],
        "arm": options["arm"],
        "k_consistency": options["k_consistency"],
        "max_repair_rounds": options["max_repair_rounds"],
        "mechanism_claims": options.get("mechanism_claims"),
        "disabled_vs_solver": options.get("disabled_vs_solver"),
        "is_reference": options.get("is_reference"),
        "options": options,
        "limitations": list(spec.limitations),
        "input_mode": options.get("input_mode"),
        "nlq_track": options.get("nlq_track"),
        "nlq_hash": options.get("nlq_hash"),
        "witness_k": options.get("witness_k"),
        "evaluation_skip_reason": options.get("evaluation_skip_reason"),
        "uses_gold_mql": False,
        "backbone": solver_disclosure.get("backbone"),
        "disjointness_ok": solver_disclosure.get("disjointness_ok"),
        "s_solver": solver_disclosure.get("s_solver"),
        "no_training": solver_disclosure.get("no_training"),
        "solver_disclosure": solver_disclosure,
    }


def _attempt_count(payload: dict[str, Any] | None = None) -> int:
    if payload:
        for key in ("rounds", "attempts", "llm_turns", "turns"):
            value = payload.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return 1


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _nlq_hash(nlq: str | None) -> str | None:
    if not nlq:
        return None
    return "sha256:" + hashlib.sha256(nlq.encode("utf-8")).hexdigest()


def _nlq_hash_from_record(record: dict[str, Any]) -> str | None:
    try:
        return _nlq_hash(_canonical_nlq(record))
    except TendError:
        return None


def _provenance_str(
    options: dict[str, Any],
    payload: dict[str, Any],
    key: str,
    *,
    default: str,
) -> str:
    value = options.get(key) or payload.get(key) or default
    return str(value)


def _provenance_int(options: dict[str, Any], payload: dict[str, Any], key: str) -> int:
    value = options.get(key)
    if value is None:
        value = payload.get(key, 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _work_item_id(
    ablation_id: str,
    batch_index: int | None,
    db_id: Any,
    record_id: Any,
) -> str:
    batch_part = str(batch_index) if batch_index is not None else "single"
    db_part = _trace_part(db_id)
    record_part = _trace_part(record_id)
    return f"ablation:{batch_part}:{ablation_id}:{db_part}:{record_part}"


def _session_id(work_item_id: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in work_item_id)
    return f"ablation-{slug}"


def _trace_part(value: Any) -> str:
    if value in (None, ""):
        return "na"
    return str(value)


def _trace_fields(
    spec: SagAblationSpec,
    options: dict[str, Any],
    *,
    db_id: str,
    record_id: Any,
) -> dict[str, Any]:
    batch_index = options.get("batch_index")
    if not isinstance(batch_index, int):
        batch_index = None
    work_item_id = str(
        options.get("work_item_id") or _work_item_id(spec.id, batch_index, db_id, record_id)
    )
    return {
        "session_id": str(options.get("session_id") or _session_id(work_item_id)),
        "batch_index": batch_index,
        "work_item_id": work_item_id,
    }


__all__ = [
    "AblationFailure",
    "AblationPrediction",
    "run_ablation_record",
    "run_ablation_suite",
]
