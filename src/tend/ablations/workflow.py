"""Runtime workflow for SMART-EG ablation studies."""
from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..errors import Anomaly, SourceError, TendError, wrap_unexpected
from ..execution.ast_check import static_mql_feedback
from ..solver.inputs import (
    DEFAULT_INPUT_SAMPLE_SIZE,
    NlqTrack,
    _canonical_nlq,
    build_nlq_db_solver_input,
    load_solver_release_inputs,
)
from ..workflow import Workflow
from .strategies import SmartEGAblationSpec, resolve_ablations


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
    max_tool_turns: int
    max_revisits: int
    cost_budget_usd: float
    uses_evidence_gate: bool
    uses_counterexample: bool
    uses_value_grounding: bool
    uses_relationship_probe: bool
    uses_prefix_execution: bool
    uses_revisit: bool
    disclosure: dict[str, Any]
    feedback: list[dict[str, Any]] = field(default_factory=list)
    static_feedback: list[dict[str, Any]] = field(default_factory=list)
    environment_model_ref: str | None = None
    intent_ref: str | None = None
    query_plan_ref: str | None = None
    execution_trace_ref: str | None = None
    evidence_ledger_ref: str | None = None
    agent_session_ref: str | None = None
    submit_gate_refs: list[str] = field(default_factory=list)
    transcript_refs: list[str] = field(default_factory=list)
    diagnostics_refs: list[str] = field(default_factory=list)
    error_refs: list[str] = field(default_factory=list)
    events_path: str = "events.jsonl"
    anomalies_path: str = "anomalies.jsonl"
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
    max_tool_turns: int
    max_revisits: int
    cost_budget_usd: float
    uses_evidence_gate: bool
    uses_counterexample: bool
    uses_value_grounding: bool
    uses_relationship_probe: bool
    uses_prefix_execution: bool
    uses_revisit: bool
    disclosure: dict[str, Any]
    MQL: str = ""
    feedback: list[dict[str, Any]] = field(default_factory=list)
    static_feedback: list[dict[str, Any]] = field(default_factory=list)
    last_candidate_ref: str | None = None
    unresolved_debts: list[str] = field(default_factory=list)
    evidence_ledger_ref: str | None = None
    execution_trace_ref: str | None = None
    agent_session_ref: str | None = None
    transcript_refs: list[str] = field(default_factory=list)
    diagnostics_refs: list[str] = field(default_factory=list)
    error_refs: list[str] = field(default_factory=list)
    events_path: str = "events.jsonl"
    anomalies_path: str = "anomalies.jsonl"
    result_type: str = "ablation_failure"
    status: str = "failed"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


async def smart_solve_record_eg(*args: Any, **kwargs: Any) -> Any:
    from ..solver.eg import SmartEGPolicy, smart_solve_nlq_db_eg as solve

    if len(args) < 3:
        raise TypeError("smart_solve_record_eg requires wf, record, schema")
    wf = args[0]
    record = args[1]
    if not isinstance(record, dict):
        raise TypeError("smart_solve_record_eg record must be a dict")
    options = kwargs.get("options") if isinstance(kwargs.get("options"), dict) else {}
    policy = kwargs.get("policy") or SmartEGPolicy(
        max_tool_turns=int(kwargs.get("max_tool_turns", options.get("max_tool_turns", 48))),
        max_revisits=int(kwargs.get("max_revisits", options.get("max_revisits", 2))),
        cost_budget_usd=float(kwargs.get("cost_budget_usd", options.get("cost_budget_usd", 1.0))),
        evidence_gate=bool(options.get("use_evidence_gate", True)),
        counterexample_gate=bool(options.get("use_counterexample", True)),
        value_grounding=bool(options.get("use_value_grounding", True)),
        relationship_probe=bool(options.get("use_relationship_probe", True)),
        prefix_execution=bool(options.get("use_prefix_execution", True)),
        revisit=bool(options.get("use_revisit", True)),
        probe_scheduler=bool(options.get("use_probe_scheduler", False)),
        budget_profile=str(options.get("budget_profile") or "reference"),
    )
    session_id = options.get("session_id")
    return await solve(
        wf,
        db_id=str(record.get("db_id") or ""),
        nlq=_canonical_nlq(record),
        record_id=record.get("record_id"),
        session_id=str(session_id) if session_id else None,
        policy=policy,
    )


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
    max_tool_turns: int = 48,
    max_revisits: int = 2,
    cost_budget_usd: float = 1.0,
    workers: int = 1,
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
    log = wf.ctx.log.bind(component="ablation_suite")
    log.info(
        "ablation_suite_start",
        ablations=[spec.id for spec in specs],
        records=len(inputs),
        dataset_dir=str(dataset_dir),
        db_id=db_id,
        record_id=record_id,
        max_tool_turns=max_tool_turns,
        max_revisits=max_revisits,
        cost_budget_usd=cost_budget_usd,
        input_mode=input_mode,
        nlq_track=effective_nlq_track,
        nlq_hash=nlq_hash,
        witness_k=witness_k,
        workers=suite_workers,
        evaluation_skip_reason=evaluation_skip_reason,
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
        tuple[int, SmartEGAblationSpec, dict[str, Any], dict[str, Any], dict[str, Any] | None]
    ] = []
    for record, schema, data in inputs:
        for spec in specs:
            work.append((len(work), spec, record, schema, data))

    async def run_one(
        batch_index: int,
        spec: SmartEGAblationSpec,
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
                max_tool_turns=max_tool_turns,
                max_revisits=max_revisits,
                cost_budget_usd=cost_budget_usd,
                batch_index=batch_index,
                witness_preloaded=db in preloaded_dbs,
                input_mode=input_mode,
                nlq_track=effective_nlq_track,
                nlq_hash=nlq_hash,
                witness_k=witness_k,
                evaluation_skip_reason=evaluation_skip_reason,
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
                log.anomaly(err)
            payload = _ablation_worker_failure_payload(
                wf,
                spec,
                err,
                db_id=db,
                record_id=rid,
                batch_index=batch_index,
                local_data=data,
                max_tool_turns=max_tool_turns,
                max_revisits=max_revisits,
                cost_budget_usd=cost_budget_usd,
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
            log.anomaly(err)
            payload = _ablation_worker_failure_payload(
                wf,
                spec,
                err,
                db_id=db,
                record_id=rid,
                batch_index=batch_index,
                local_data=data,
                max_tool_turns=max_tool_turns,
                max_revisits=max_revisits,
                cost_budget_usd=cost_budget_usd,
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
        spec: SmartEGAblationSpec,
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
    log.info("ablation_suite_done", outputs=len(outputs), ablations=len(specs))
    return outputs


def _ablation_worker_failure_payload(
    wf: Workflow,
    spec: SmartEGAblationSpec,
    err: TendError,
    *,
    db_id: str,
    record_id: int | str | None,
    batch_index: int,
    local_data: dict[str, list[dict[str, Any]]] | None,
    max_tool_turns: int,
    max_revisits: int,
    cost_budget_usd: float,
    input_mode: str,
    nlq_track: NlqTrack,
    nlq_hash: str | None,
    witness_k: int,
    evaluation_skip_reason: str | None,
) -> dict[str, Any]:
    options = _runtime_options(
        spec,
        max_tool_turns=max_tool_turns,
        max_revisits=max_revisits,
        cost_budget_usd=cost_budget_usd,
        batch_index=batch_index,
        db_id=db_id,
        record_id=record_id,
        input_mode=input_mode,
        nlq_track=nlq_track,
        nlq_hash=nlq_hash,
        witness_k=witness_k,
        evaluation_skip_reason=evaluation_skip_reason,
    )
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
    spec: SmartEGAblationSpec,
    record: dict[str, Any],
    schema: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    max_tool_turns: int = 48,
    max_revisits: int = 2,
    cost_budget_usd: float = 1.0,
    batch_index: int | None = None,
    witness_preloaded: bool = False,
    input_mode: str = "release",
    nlq_track: NlqTrack = "record",
    nlq_hash: str | None = None,
    witness_k: int = DEFAULT_INPUT_SAMPLE_SIZE,
    evaluation_skip_reason: str | None = None,
) -> AblationPrediction | AblationFailure:
    db_id = str(record.get("db_id") or "")
    record_id = record.get("record_id")
    effective_nlq_hash = nlq_hash if nlq_hash is not None else _nlq_hash_from_record(record)
    options = _runtime_options(
        spec,
        max_tool_turns=max_tool_turns,
        max_revisits=max_revisits,
        cost_budget_usd=cost_budget_usd,
        batch_index=batch_index,
        db_id=db_id,
        record_id=record_id,
        input_mode=input_mode,
        nlq_track=nlq_track,
        nlq_hash=effective_nlq_hash,
        witness_k=witness_k,
        evaluation_skip_reason=evaluation_skip_reason,
    )
    base_log = wf.ctx.log.bind(
        component="ablation_runner",
        ablation_id=spec.id,
        solver_variant=options["solver_variant"],
        db_id=db_id,
        record_id=record_id,
        batch_index=batch_index,
    )
    base_log.info(
        "ablation_record_start",
        title=spec.title,
        limitations=list(spec.limitations),
        solver_options=options,
    )

    variant_wf = _variant_workflow(wf, spec, options, batch_index)
    try:
        _canonical_nlq(record)
        result = await smart_solve_record_eg(
            variant_wf,
            record,
            schema,
            local_data=local_data,
            options=options,
            max_tool_turns=options["max_tool_turns"],
            max_revisits=options["max_revisits"],
            cost_budget_usd=options["cost_budget_usd"],
            witness_preloaded=witness_preloaded,
        )
        payload = result.to_json() if hasattr(result, "to_json") else dict(result)
        refs = _llm_refs_for(wf, spec.id, db_id, record_id)
        if payload.get("result_type") == "solver_failure":
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
    except Exception as exc:
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
    spec: SmartEGAblationSpec,
    *,
    max_tool_turns: int,
    max_revisits: int,
    cost_budget_usd: float,
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
        max_tool_turns=max_tool_turns,
        max_revisits=max_revisits,
        cost_budget_usd=cost_budget_usd,
        progress_group_prefix=prefix,
        progress_work_item_id=work_item_id,
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
    spec: SmartEGAblationSpec,
    options: dict[str, Any],
    batch_index: int | None = None,
) -> Workflow:
    ctx = replace(
        wf.ctx,
        log=wf.ctx.log.bind(
            ablation_id=spec.id,
            solver_variant=options["solver_variant"],
            batch_index=batch_index,
            db_id=options.get("db_id"),
            record_id=options.get("record_id"),
            session_id=options.get("session_id"),
            work_item_id=options.get("work_item_id"),
        ),
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
    spec: SmartEGAblationSpec,
    options: dict[str, Any],
    payload: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None,
    transcript_refs: list[str],
    diagnostics_refs: list[str],
) -> AblationPrediction:
    feedback = list(payload.get("feedback") or [])
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
        attempts=_attempt_count(feedback, payload),
        max_tool_turns=int(options["max_tool_turns"]),
        max_revisits=int(options["max_revisits"]),
        cost_budget_usd=float(options["cost_budget_usd"]),
        uses_evidence_gate=bool(options["use_evidence_gate"]),
        uses_counterexample=bool(options["use_counterexample"]),
        uses_value_grounding=bool(options["use_value_grounding"]),
        uses_relationship_probe=bool(options["use_relationship_probe"]),
        uses_prefix_execution=bool(options["use_prefix_execution"]),
        uses_revisit=bool(options["use_revisit"]),
        disclosure=_disclosure(spec, options, payload.get("disclosure") or {}),
        feedback=feedback,
        static_feedback=static_mql_feedback(mql),
        environment_model_ref=_optional_str(payload.get("environment_model_ref")),
        intent_ref=_optional_str(payload.get("intent_ref")),
        query_plan_ref=_optional_str(payload.get("query_plan_ref")),
        execution_trace_ref=_optional_str(payload.get("execution_trace_ref")),
        evidence_ledger_ref=_optional_str(payload.get("evidence_ledger_ref")),
        agent_session_ref=_optional_str(payload.get("agent_session_ref")),
        submit_gate_refs=_str_list(payload.get("submit_gate_refs")),
        transcript_refs=transcript_refs,
        diagnostics_refs=diagnostics_refs,
        error_refs=_str_list(payload.get("error_refs")),
    )


def _failure_from_solver_payload(
    wf: Workflow,
    spec: SmartEGAblationSpec,
    options: dict[str, Any],
    payload: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None,
    transcript_refs: list[str],
    diagnostics_refs: list[str],
) -> AblationFailure:
    feedback = list(payload.get("feedback") or [])
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
        attempts=_attempt_count(feedback, payload),
        max_tool_turns=int(options["max_tool_turns"]),
        max_revisits=int(options["max_revisits"]),
        cost_budget_usd=float(options["cost_budget_usd"]),
        uses_evidence_gate=bool(options["use_evidence_gate"]),
        uses_counterexample=bool(options["use_counterexample"]),
        uses_value_grounding=bool(options["use_value_grounding"]),
        uses_relationship_probe=bool(options["use_relationship_probe"]),
        uses_prefix_execution=bool(options["use_prefix_execution"]),
        uses_revisit=bool(options["use_revisit"]),
        disclosure=_disclosure(spec, options, payload.get("disclosure") or {}),
        MQL=mql,
        feedback=feedback,
        static_feedback=static_mql_feedback(mql),
        last_candidate_ref=_optional_str(payload.get("last_candidate_ref")),
        unresolved_debts=_str_list(payload.get("unresolved_debts")),
        evidence_ledger_ref=_optional_str(payload.get("evidence_ledger_ref")),
        execution_trace_ref=_optional_str(payload.get("execution_trace_ref")),
        agent_session_ref=_optional_str(payload.get("agent_session_ref")),
        transcript_refs=transcript_refs,
        diagnostics_refs=diagnostics_refs,
        error_refs=_str_list(payload.get("error_refs")),
    )


def _failure_from_error(
    wf: Workflow,
    spec: SmartEGAblationSpec,
    options: dict[str, Any],
    err: TendError,
    *,
    db_id: str,
    record_id: int | str | None,
    local_data: dict[str, list[dict[str, Any]]] | None,
    transcript_refs: list[str],
    diagnostics_refs: list[str],
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
        max_tool_turns=int(options["max_tool_turns"]),
        max_revisits=int(options["max_revisits"]),
        cost_budget_usd=float(options["cost_budget_usd"]),
        uses_evidence_gate=bool(options["use_evidence_gate"]),
        uses_counterexample=bool(options["use_counterexample"]),
        uses_value_grounding=bool(options["use_value_grounding"]),
        uses_relationship_probe=bool(options["use_relationship_probe"]),
        uses_prefix_execution=bool(options["use_prefix_execution"]),
        uses_revisit=bool(options["use_revisit"]),
        disclosure=_disclosure(spec, options, {}),
        transcript_refs=transcript_refs,
        diagnostics_refs=diagnostics_refs,
    )


def _disclosure(
    spec: SmartEGAblationSpec,
    options: dict[str, Any],
    solver_disclosure: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ablation_id": spec.id,
        "ablation_title": spec.title,
        "solver_variant": options["solver_variant"],
        "options": options,
        "limitations": list(spec.limitations),
        "input_mode": options.get("input_mode"),
        "nlq_track": options.get("nlq_track"),
        "nlq_hash": options.get("nlq_hash"),
        "witness_k": options.get("witness_k"),
        "evaluation_skip_reason": options.get("evaluation_skip_reason"),
        "mechanism_claims": options.get("mechanism_claims"),
        "tool_exposure_intent": options.get("tool_exposure_intent"),
        "prompt_intent": options.get("prompt_intent"),
        "gate_flags": options.get("gate_flags"),
        "state_transition_intent": options.get("state_transition_intent"),
        "probe_scheduler_status": options.get("probe_scheduler_status"),
        "effective_budget_profile": options.get("effective_budget_profile"),
        "effective_tool_turn_count": options.get("effective_tool_turn_count"),
        "budget_disclosure": options.get("budget_disclosure"),
        "backbone": solver_disclosure.get("backbone"),
        "disjointness_ok": solver_disclosure.get("disjointness_ok"),
        "s_solver": solver_disclosure.get("s_solver"),
        "max_tool_turns": options["max_tool_turns"],
        "max_revisits": options["max_revisits"],
        "cost_budget_usd": options["cost_budget_usd"],
        "budget_profile": options.get("budget_profile"),
        "solver_reported_max_tool_turns": solver_disclosure.get("max_tool_turns"),
        "solver_reported_max_revisits": solver_disclosure.get("max_revisits"),
        "solver_reported_cost_budget_usd": solver_disclosure.get("cost_budget_usd"),
        "solver_reported_budget_profile": solver_disclosure.get("budget_profile"),
        "cost_budget_usd_source": options.get("cost_budget_usd_source"),
        "cost_budget_usd_unpriced_behavior": options.get(
            "cost_budget_usd_unpriced_behavior"
        ),
        "no_training": solver_disclosure.get("no_training"),
        "solver_disclosure": solver_disclosure,
    }


def _attempt_count(
    feedback: list[dict[str, Any]],
    payload: dict[str, Any] | None = None,
) -> int:
    if payload:
        for key in ("attempts", "tool_turns", "turns"):
            value = payload.get(key)
            if isinstance(value, int) and value > 0:
                return value
    if not feedback:
        return 1
    return len(feedback) + 1


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


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


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
    return f"solve-{slug}"


def _trace_part(value: Any) -> str:
    if value in (None, ""):
        return "na"
    return str(value)


def _trace_fields(
    spec: SmartEGAblationSpec,
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


def _llm_refs_for(
    wf: Workflow,
    ablation_id: str,
    db_id: str,
    record_id: int | str | None,
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
        if str(event.get("db_id") or "") != db_id:
            continue
        if not _record_id_matches(event.get("record_id"), record_id):
            continue
        transcript_ref = event.get("transcript_ref")
        diagnostics_ref = event.get("diagnostics_ref")
        if isinstance(transcript_ref, str) and transcript_ref not in transcript_refs:
            transcript_refs.append(transcript_ref)
        if isinstance(diagnostics_ref, str) and diagnostics_ref not in diagnostics_refs:
            diagnostics_refs.append(diagnostics_ref)
    return {"transcript_refs": transcript_refs, "diagnostics_refs": diagnostics_refs}


def _record_id_matches(event_record_id: Any, record_id: int | str | None) -> bool:
    if record_id is None:
        return event_record_id in (None, "")
    return str(event_record_id) == str(record_id)


__all__ = [
    "AblationFailure",
    "AblationPrediction",
    "run_ablation_record",
    "run_ablation_suite",
    "smart_solve_record_eg",
]
