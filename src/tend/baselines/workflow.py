"""Runtime workflow for constrained LLM baselines."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os as _os
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..errors import (
    Anomaly,
    ContractViolationError,
    SourceError,
    TendError,
    wrap_unexpected,
)
from ..execution.ast_check import render_mql, static_mql_feedback
from ..solver.inputs import (
    NlqTrack,
    _canonical_nlq,
    build_nlq_db_solver_input,
    build_witness_digest,
    load_solver_release_inputs,
)
from ..utils.logging import AgentTurnLogPayload, LogManager, TaskLogger
from ..workflow import Workflow
from .boundary import (
    PUBLIC_SCHEMA_VERSION,
    check_disjointness,
    load_solver_allow_list,
    public_schema_shape,
    sanitize_public_local_data,
    sanitize_public_record,
    sanitize_public_schema,
)
from .strategies import (
    REACT_ACTION_SCHEMA,
    BaselinePromptContext,
    BaselineSpec,
    build_react_system_prompt,
    resolve_baselines,
)

# Bound on the fair ReAct arms' JSON-action loop. Default 16 reproduces the published
# fair-comparison measurement (react_naive 4/110, react_informed 25/110 on financial).
REACT_MAX_STEPS = max(1, int(_os.environ.get("TEND_BASELINE_REACT_MAX_STEPS", "16")))
# Fair-contract arm: give the baselines the same six output conventions SAG's prompt
# states, so a measured margin reflects mechanism rather than instruction asymmetry.
# Read here because this module already reads its knobs from the environment; the
# prompt builders take it as data. Default off keeps frozen artifacts reproducible.
OUTPUT_CONTRACT = _os.environ.get("TEND_BASELINE_OUTPUT_CONTRACT", "").strip().lower() in {
    "1", "true", "yes"}

# The fair ReAct arms see RAW first-N rows (the published measurement's observation
# channel — disclosed in `_baseline_disclosure`, unlike every redacted baseline).
REACT_OBSERVATION_ROWS = 5
REACT_OBSERVATION_CHAR_CAP = 2000


# Bounded JSON/schema repair only — not execution feedback or extra LLM steps.
BASELINE_JSON_REPAIR_RETRIES = 2

# The step baselines infer structure from sampled documents only (no curated schema).
# `data_rich_direct` is the "more data, still no exploration" arm: it sees a larger
# document sample than the suite's `--witness-k`.
_BASELINE_WITNESS_K_OVERRIDE = {"data_rich_direct": 8}

# `log_exception_event` named parameters that TendError.context must never shadow.
_RESERVED_EXC_FIELDS = frozenset(
    {
        "stage",
        "task_id",
        "recoverable",
        "cached",
        "acceptance_phase",
        "log_path",
        "session_path",
        "_level",
        "event",
    }
)


@dataclass(frozen=True, slots=True)
class BaselineStepTrace:
    step_id: str
    agent: str
    title: str
    output: dict[str, Any]
    log_ref: str = ""
    call_id: str = ""
    llm_attempts: int = 0
    transport_retries: int = 0
    json_repair_retries: int = BASELINE_JSON_REPAIR_RETRIES


@dataclass(frozen=True, slots=True)
class BaselinePrediction:
    baseline_id: str
    baseline_title: str
    record_id: int | None
    db_id: str
    MQL: str
    disclosure: dict[str, Any]
    steps: list[BaselineStepTrace]
    agent_session_ref: str = ""
    witness_k: int = 0
    r_max: int = 0
    input_mode: str = "release"
    nlq_track: str = "record"
    nlq_hash: str = ""
    evaluation_skip_reason: str | None = None
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
    agent_session_ref: str = ""
    witness_k: int = 0
    r_max: int = 0
    input_mode: str = "release"
    nlq_track: str = "record"
    nlq_hash: str = ""
    evaluation_skip_reason: str | None = None
    steps: list[BaselineStepTrace] = field(default_factory=list)
    static_feedback: list[dict[str, Any]] = field(default_factory=list)
    result_type: str = "baseline_failure"
    status: str = "failed"

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [asdict(step) for step in self.steps]
        return payload


# --------------------------------------------------------------------------- #
# DynaDB-style logging plumbing
# --------------------------------------------------------------------------- #
def _rel_ref(log_mgr: LogManager, path: Any) -> str:
    """Run-dir-relative ref for a log artifact path ('' when path is missing)."""
    if not path:
        return ""
    try:
        return _os.path.relpath(Path(path), log_mgr.root).replace("\\", "/")
    except ValueError:
        return str(path)


def _task_log_ref(log_mgr: LogManager, task_log: TaskLogger) -> str:
    """Best current artifact ref for a task: agent session > last call log > task log."""
    path = (
        getattr(task_log, "_agent_session_path", None)
        or getattr(task_log, "_last_agent_session_path", None)
        or task_log.last_llm_call_path
        or task_log.log_path
    )
    return _rel_ref(log_mgr, path)


def _log_baseline_exception(
    log_mgr: LogManager,
    event: str,
    exc: BaseException,
    *,
    stage: str,
    task_id: str | None,
    **extra: Any,
) -> None:
    """Route an anomaly into run.log + errors.jsonl, TendError-aware."""
    fields = dict(extra)
    if isinstance(exc, TendError):
        if exc.anomaly is not None:
            fields.setdefault("anomaly", exc.anomaly.value)
        for key, value in exc.context.items():
            if key not in _RESERVED_EXC_FIELDS:
                fields.setdefault(key, value)
        exc.logged = True
    log_mgr.log_exception_event(event, exc, stage=stage, task_id=task_id, **fields)


def _turn_usage(usage: dict[str, Any] | None) -> tuple[dict[str, int], int]:
    """(integer usage dict for the turn payload, total tokens for accounting)."""
    clean: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = (usage or {}).get(key)
        if isinstance(value, (int, float)):
            clean[key] = int(value)
    total = clean.get("total_tokens") or (
        clean.get("prompt_tokens", 0) + clean.get("completion_tokens", 0)
    )
    return clean, total


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
    input_mode = "nlq_db" if nlq is not None else "release"
    effective_nlq_track = "canonical" if nlq is not None else nlq_track
    evaluation_skip_reason = "no_release_dataset" if nlq is not None else None
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
    log_mgr: LogManager = wf.ctx.log_mgr
    suite_log = log_mgr.get_stage_logger("baseline")
    suite_log.info(
        "baseline_suite_start",
        baselines=[spec.id for spec in specs],
        records=len(inputs),
        dataset_dir=str(dataset_dir),
        db_id=db_id,
        record_id=record_id,
        input_mode=input_mode,
        nlq_track=effective_nlq_track,
        evaluation_skip_reason=evaluation_skip_reason,
    )
    if not inputs:
        _log_baseline_exception(
            log_mgr,
            "baseline_no_records",
            SourceError(
                "no baseline records matched filters",
                anomaly=Anomaly.SUPPLY_EXHAUSTED,
                context={
                    "dataset_dir": str(dataset_dir),
                    "db_id": db_id,
                    "record_id": record_id,
                },
            ),
            stage="baseline",
            task_id=None,
        )
        return []

    if wf.ctx.progress:
        wf.ctx.progress.phase("BASELINE")

    work_by_baseline: list[
        tuple[str, BaselineSpec, list[tuple[int, BaselineSpec, dict, dict, dict | None]]]
    ] = []
    next_batch_index = 0
    for spec in specs:
        baseline_work: list[tuple[int, BaselineSpec, dict, dict, dict | None]] = []
        for record, schema, data in inputs:
            baseline_work.append((next_batch_index, spec, record, schema, data))
            next_batch_index += 1
        work_by_baseline.append((spec.id, spec, baseline_work))

    async def run_one(
        batch_index: int,
        spec: BaselineSpec,
        record: dict,
        schema: dict,
        data: dict | None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            result = await run_baseline_record(
                wf,
                spec,
                record,
                schema,
                local_data=data,
                witness_k=witness_k,
                batch_index=batch_index,
                input_mode=input_mode,
                nlq_track=effective_nlq_track,
                evaluation_skip_reason=evaluation_skip_reason,
            )
            payload = result.to_json()
        except Exception as exc:  # noqa: BLE001 - one record must NEVER abort the suite
            # run_baseline_record already converts TendError into a typed failure; this
            # backstop catches unexpected faults so the gather can't cancel the other
            # 100+ in-flight records of the arm (hours of work) over one record's bug.
            err = wrap_unexpected(
                exc,
                stage="baseline_record",
                baseline_id=spec.id,
                db_id=str(record.get("db_id") or ""),
                record_id=record.get("record_id"),
                batch_index=batch_index,
            )
            if not err.logged:
                _log_baseline_exception(
                    log_mgr,
                    "baseline_record_backstop",
                    err,
                    stage=spec.id,
                    task_id=f"{record.get('db_id')}/{record.get('record_id')}",
                )
            payload = BaselineFailure(
                baseline_id=spec.id,
                baseline_title=spec.title,
                record_id=record.get("record_id"),
                db_id=str(record.get("db_id") or ""),
                error_code=err.anomaly.value if err.anomaly else "internal",
                message=err.message,
                disclosure={"backstop": "baseline_suite_run_one", "uses_gold_mql": False},
                input_mode=input_mode,
                nlq_track=str(effective_nlq_track),
                evaluation_skip_reason=evaluation_skip_reason,
            ).to_json()
        payload["batch_index"] = batch_index
        payload["work_item_id"] = (
            f"baseline:{batch_index}:{spec.id}:{record.get('db_id')}:{record.get('record_id')}"
        )
        return batch_index, payload

    completed: list[tuple[int, dict[str, Any]]] = []
    for baseline_id, spec, baseline_work in work_by_baseline:
        stage_log = log_mgr.get_stage_logger(baseline_id)
        stage_log.info(
            "baseline_method_start",
            baseline_id=baseline_id,
            records=len(baseline_work),
            batch_indices=[item[0] for item in baseline_work],
        )
        tasks = [
            asyncio.create_task(run_one(batch_index, spec, record, schema, data))
            for batch_index, spec, record, schema, data in baseline_work
        ]
        try:
            baseline_completed = await asyncio.gather(*tasks)
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
                    _log_baseline_exception(
                        log_mgr,
                        "baseline_method_gather",
                        wrap_unexpected(exc, stage="baseline_method_gather"),
                        stage=baseline_id,
                        task_id=None,
                    )
            raise
        completed.extend(baseline_completed)
        stage_log.info(
            "baseline_method_done",
            baseline_id=baseline_id,
            outputs=len(baseline_completed),
        )
    outputs = [payload for _, payload in sorted(completed, key=lambda item: item[0])]
    suite_log.info("baseline_suite_done", outputs=len(outputs), baselines=len(specs))
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
    input_mode: str = "release",
    nlq_track: str = "record",
    evaluation_skip_reason: str | None = None,
) -> BaselinePrediction | BaselineFailure:
    ctx = wf.ctx
    log_mgr: LogManager = ctx.log_mgr
    stage = spec.id
    sanitized_record = sanitize_public_record(record)
    sanitized_schema = sanitize_public_schema(schema)
    sanitized_local_data = sanitize_public_local_data(local_data)
    safe = sanitized_record.value
    public_schema = sanitized_schema.value
    db_id = str(safe["db_id"])
    record_id = safe.get("record_id")
    task_key = f"{db_id}/{record_id if record_id is not None else 'no_record'}"
    task_log = log_mgr.get_task_logger(stage, task_key)
    if sanitized_record.stripped_fields:
        task_log.info("baseline_record_fields_stripped", fields=sanitized_record.stripped_fields)
    if sanitized_schema.stripped_fields:
        task_log.info("baseline_schema_fields_stripped", fields=sanitized_schema.stripped_fields)
    if sanitized_local_data.stripped_fields:
        task_log.info(
            "baseline_local_data_fields_stripped",
            fields=sanitized_local_data.stripped_fields,
        )
    actual_nlq_track = str(record.get("nlq_track") or nlq_track)
    # Baselines never receive a curated schema; they infer structure from sampled documents.
    # `data_rich_direct` is the larger-sample arm, but only in a sampled regime: a
    # `--witness-k 0` run gives every baseline an empty sample.
    effective_witness_k = (
        _BASELINE_WITNESS_K_OVERRIDE.get(spec.id, witness_k) if witness_k > 0 else 0
    )
    disclosure = _baseline_disclosure(
        wf,
        spec,
        witness_k=effective_witness_k,
        schema_stripped_fields=sanitized_schema.stripped_fields,
        record_stripped_fields=sanitized_record.stripped_fields,
        local_data_stripped_fields=sanitized_local_data.stripped_fields,
        schema_public_shape=public_schema_shape(public_schema),
    )
    # The schema is used only for disclosure/leakage accounting; it is NOT shown to the model.
    witness_digest = build_witness_digest(
        sanitized_local_data.value if local_data is not None else None,
        effective_witness_k,
    )
    # Downstream prediction/failure rows report the effective sample budget.
    witness_k = effective_witness_k
    nlq_hash = ""
    try:
        # Baselines expose only the canonical NLQ track after record sanitization.
        nlq = _canonical_nlq(safe, use_colloquial=False)
        nlq_hash = _hash_nlq(nlq)
    except TendError as err:
        err.with_context(baseline_id=spec.id, db_id=db_id, record_id=record_id)
        error_code = err.anomaly.value if err.anomaly else "prompt_error"
        _log_baseline_exception(
            log_mgr,
            "baseline_record_failed",
            err,
            stage=stage,
            task_id=task_key,
            agent_session_ref=_task_log_ref(log_mgr, task_log),
        )
        return BaselineFailure(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            error_code=error_code,
            message=err.message,
            disclosure=disclosure,
            agent_session_ref=_task_log_ref(log_mgr, task_log),
            witness_k=witness_k,
            r_max=0,
            input_mode=input_mode,
            nlq_track=actual_nlq_track,
            nlq_hash=nlq_hash,
            evaluation_skip_reason=evaluation_skip_reason,
        )
    group_prefix = (
        f"baseline:{batch_index}:{spec.id}" if batch_index is not None else f"baseline:{spec.id}"
    )
    group = (
        f"{group_prefix}:{db_id}:{record_id}"
        if record_id is not None
        else f"{group_prefix}:{db_id}"
    )
    if ctx.progress:
        ctx.progress.add_group(
            group,
            f"{spec.id} {db_id}/{record_id}",
            phase="BASELINE",
            total=len(spec.steps),
        )

    task_log.info(
        "baseline_record_start",
        baseline_id=spec.id,
        title=spec.title,
        batch_index=batch_index,
        limitations=list(spec.limitations),
        steps=[step.id for step in spec.steps],
    )

    prompt_ctx = BaselinePromptContext(
        record=safe,
        witness_digest=witness_digest,
        nlq=nlq,
        output_contract=OUTPUT_CONTRACT,
    )

    state: dict[str, Any] = {}
    traces: list[BaselineStepTrace] = []
    final_feedback: list[dict[str, Any]] = []
    try:
        if spec.react_arm:
            react_mql, react_traces = await _run_react_baseline(
                ctx,
                spec,
                prompt_ctx,
                db_id=db_id,
                sanitized_local_data=sanitized_local_data.value,
                group=group,
                batch_index=batch_index,
                task_log=task_log,
            )
            traces.extend(react_traces)
            state["MQL"] = react_mql
        else:
            for step in spec.steps:
                output, trace = await _run_step(
                    ctx,
                    spec,
                    step,
                    prompt_ctx,
                    state,
                    group,
                    batch_index=batch_index,
                    task_log=task_log,
                )
                traces.append(trace)
                # Flat keys keep the single-step arms working; the step-scoped copy is what
                # multi-step arms read, so a later step can name which earlier step it wants
                # instead of hoping the key did not collide.
                state.update(output)
                state[step.id] = dict(output)

        mql = _extract_mql(state)
        final_feedback = static_mql_feedback(mql)
        task_log.info("baseline_static_feedback", label="final", feedback=final_feedback)
        session_ref = _task_log_ref(log_mgr, task_log)
        if any(item["severity"] == "error" for item in final_feedback):
            err = ContractViolationError(
                "baseline produced statically invalid MQL",
                anomaly=Anomaly.PARSE_ERROR,
                context={
                    "baseline_id": spec.id,
                    "db_id": db_id,
                    "record_id": record_id,
                    "feedback": final_feedback,
                },
            )
            _log_baseline_exception(
                log_mgr,
                "baseline_static_invalid_mql",
                err,
                stage=stage,
                task_id=task_key,
                agent_session_ref=session_ref,
            )
            return BaselineFailure(
                baseline_id=spec.id,
                baseline_title=spec.title,
                record_id=record_id,
                db_id=db_id,
                error_code="STATIC_INVALID_MQL",
                message="baseline produced statically invalid MQL",
                disclosure=disclosure,
                agent_session_ref=session_ref,
                witness_k=witness_k,
                r_max=0,
                input_mode=input_mode,
                nlq_track=actual_nlq_track,
                nlq_hash=nlq_hash,
                evaluation_skip_reason=evaluation_skip_reason,
                steps=traces,
                static_feedback=final_feedback,
            )

        task_log.info(
            "baseline_record_done",
            status="ok",
            mql_preview=mql[:240],
            steps=len(traces),
            agent_session_ref=session_ref,
        )
        return BaselinePrediction(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            MQL=mql,
            disclosure=disclosure,
            agent_session_ref=session_ref,
            witness_k=witness_k,
            r_max=0,
            input_mode=input_mode,
            nlq_track=actual_nlq_track,
            nlq_hash=nlq_hash,
            evaluation_skip_reason=evaluation_skip_reason,
            steps=traces,
            static_feedback=final_feedback,
        )
    except TendError as err:
        if spec.react_arm and not traces:
            raw_traces = err.context.get("react_step_traces")
            if isinstance(raw_traces, list):
                traces = [
                    BaselineStepTrace(**trace) for trace in raw_traces if isinstance(trace, dict)
                ]
        err.with_context(baseline_id=spec.id, db_id=db_id, record_id=record_id)
        error_code = err.anomaly.value if err.anomaly else "tend_error"
        session_ref = _task_log_ref(log_mgr, task_log)
        if not err.logged:
            _log_baseline_exception(
                log_mgr,
                "baseline_record_failed",
                err,
                stage=stage,
                task_id=task_key,
                agent_session_ref=session_ref,
            )
        return BaselineFailure(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            error_code=error_code,
            message=err.message,
            disclosure=disclosure,
            agent_session_ref=session_ref,
            witness_k=witness_k,
            r_max=0,
            input_mode=input_mode,
            nlq_track=actual_nlq_track,
            nlq_hash=nlq_hash,
            evaluation_skip_reason=evaluation_skip_reason,
            steps=traces,
            static_feedback=final_feedback,
        )
    except Exception as exc:  # noqa: BLE001 - baseline runs should continue across records
        err = wrap_unexpected(
            exc,
            baseline_id=spec.id,
            db_id=db_id,
            record_id=record_id,
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        session_ref = _task_log_ref(log_mgr, task_log)
        _log_baseline_exception(
            log_mgr,
            "baseline_record_failed",
            err,
            stage=stage,
            task_id=task_key,
            agent_session_ref=session_ref,
        )
        return BaselineFailure(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            error_code="internal",
            message=err.message,
            disclosure=disclosure,
            agent_session_ref=session_ref,
            witness_k=witness_k,
            r_max=0,
            input_mode=input_mode,
            nlq_track=actual_nlq_track,
            nlq_hash=nlq_hash,
            evaluation_skip_reason=evaluation_skip_reason,
            steps=traces,
            static_feedback=final_feedback,
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
    task_log: TaskLogger,
) -> tuple[dict[str, Any], BaselineStepTrace]:
    prefix = (
        f"baseline:{batch_index}:{spec.id}" if batch_index is not None else f"baseline:{spec.id}"
    )
    task_id = (
        f"{prefix}:{prompt_ctx.record.get('db_id')}:{prompt_ctx.record.get('record_id')}:{step.id}"
    )
    if ctx.progress:
        ctx.progress.start_task(task_id, step.title, group=group)
    task_log.set_step_label(step.id)
    task_log.info(
        "baseline_step_start",
        baseline_id=spec.id,
        step=step.id,
        agent=step.agent,
        title=step.title,
    )
    try:
        messages = step.build_messages(prompt_ctx, state)
        result = await ctx.llm.complete(
            agent=step.agent,
            messages=messages,
            task_logger=task_log,
            schema=step.schema,
            temperature=0.0,
            json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
        )
        output = result.data
        log_ref = _rel_ref(ctx.log_mgr, task_log.last_llm_call_path)
        task_log.info(
            "baseline_step_done",
            step=step.id,
            call_log=log_ref,
            llm_attempts=result.attempts,
            transport_retries=max(0, result.attempts - 1),
            json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
        )
        if ctx.progress:
            ctx.progress.finish_task(task_id, ok=True)
        return output, BaselineStepTrace(
            step_id=step.id,
            agent=step.agent,
            title=step.title,
            output=output,
            log_ref=log_ref,
            call_id=result.call_id,
            llm_attempts=result.attempts,
            transport_retries=max(0, result.attempts - 1),
            json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
        )
    except Exception:
        if ctx.progress:
            ctx.progress.finish_task(task_id, ok=False)
        raise


async def _run_react_baseline(
    ctx: Any,
    spec: BaselineSpec,
    prompt_ctx: BaselinePromptContext,
    *,
    db_id: str,
    sanitized_local_data: dict[str, list[dict[str, Any]]] | None,
    group: str,
    batch_index: int | None,
    task_log: TaskLogger,
) -> tuple[str, list[BaselineStepTrace]]:
    """Run the fair multi-step ReAct JSON-action loop (the published comparison arms).

    Faithful port of the measured harness: STRICT-JSON actions (execute_mql | submit),
    RAW first-N-row observations capped at REACT_OBSERVATION_CHAR_CAP chars, and a
    REACT_MAX_STEPS budget. `react_informed` receives the real collection names
    (degrading to the name-free prompt when no Mongo handle is available). No induced structure, no gates,
    no repair gradient — that asymmetry versus the SAG solver is the experiment.

    Logged as ONE DynaDB-style agent session per record: open_agent_session →
    log_agent_turn per step → close_agent_session with an explicit outcome.
    """
    await _preload_agentic_witnesses(ctx, db_id, sanitized_local_data, task_log)
    mongo = getattr(ctx, "mongo", None)
    record_id = prompt_ctx.record.get("record_id")

    collection_names: list[str] | None = None
    if spec.react_arm == "informed":
        collection_names = _react_collection_names(mongo, db_id)
        if collection_names is None:
            task_log.info(
                "react_informed_degraded_to_naive",
                reason="collection listing unavailable",
                db_id=db_id,
            )

    agent_name = f"baseline_{spec.id}"
    system_prompt = build_react_system_prompt(
        db_id, steps=REACT_MAX_STEPS, collection_names=collection_names
    )
    user_message = f"Question: {prompt_ctx.nlq}\n\nReturn the JSON action."
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    task_log.set_step_label(spec.id)
    task_log.open_agent_session(
        model=ctx.settings.llm.model_for(agent_name),
        system_prompt=system_prompt,
        user_message=user_message,
    )
    session_ref = _task_log_ref(ctx.log_mgr, task_log)
    traces: list[BaselineStepTrace] = []
    final_mql = ""
    steps_taken = 0
    probes_made = 0
    total_tokens = 0
    submitted = False

    try:
        for step_index in range(1, REACT_MAX_STEPS + 1):
            steps_taken = step_index
            step_id = f"react_step_{step_index:02d}"
            prefix = (
                f"baseline:{batch_index}:{spec.id}"
                if batch_index is not None
                else f"baseline:{spec.id}"
            )
            progress_task_id = f"{prefix}:{db_id}:{record_id}:{step_id}"
            if ctx.progress:
                ctx.progress.start_task(progress_task_id, f"ReAct step {step_index}", group=group)
            try:
                result = await ctx.llm.complete(
                    agent=agent_name,
                    messages=messages,
                    schema=REACT_ACTION_SCHEMA,
                    task_logger=task_log,
                    temperature=0.0,
                    omit_max_tokens=True,
                    json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
                )
            except Exception:
                if ctx.progress:
                    ctx.progress.finish_task(progress_task_id, ok=False)
                raise
            action = result.data
            collection = str(action.get("collection") or "")
            pipeline = action.get("pipeline") if isinstance(action.get("pipeline"), list) else []
            submitted = action.get("action") == "submit"
            observation = ""
            if submitted:
                final_mql = render_mql(collection, pipeline)
            else:
                probes_made += 1
                observation = await _react_observation(mongo, db_id, collection, pipeline)
                remaining = REACT_MAX_STEPS - step_index
                # Final-stretch reminder only: the agent must submit on its own — the loop
                # never terminates it early or force-submits an exploratory probe.
                nudge = (
                    f" Only {remaining} step(s) remain — submit your best pipeline with "
                    f'the "submit" action when ready.'
                    if 0 < remaining <= 4
                    else ""
                )
                messages += [
                    {"role": "assistant", "content": json.dumps(action, ensure_ascii=False)},
                    {
                        "role": "user",
                        "content": (
                            f"Observation: {observation}\n"
                            f"(step {step_index}/{REACT_MAX_STEPS}) Continue: explore more "
                            f"or submit.{nudge}"
                        ),
                    },
                ]
            usage, step_tokens = _turn_usage(result.usage)
            total_tokens += step_tokens
            task_log.log_agent_turn(
                AgentTurnLogPayload(
                    turn=step_index,
                    max_turns=REACT_MAX_STEPS,
                    reasoning=(result.usage or {}).get("reasoning_preview"),
                    # The schema-constrained reply IS the action: it renders under
                    # Tool Calls; Content would only duplicate it, so it is omitted.
                    assistant_content=None,
                    tool_calls=[
                        {
                            "name": str(action.get("action") or "execute_mql"),
                            "arguments": {"collection": collection, "pipeline": pipeline},
                        }
                    ],
                    tool_results=(
                        [{"name": "execute_mql", "content": observation}] if observation else None
                    ),
                    usage=usage or None,
                    cost_usd=0.0,
                )
            )
            output = {
                "step": step_index,
                "action": action.get("action"),
                "collection": collection,
                "submitted_final": submitted,
                "observation_preview": observation[:400],
            }
            if ctx.progress:
                ctx.progress.finish_task(progress_task_id, ok=True)
            traces.append(
                BaselineStepTrace(
                    step_id=step_id,
                    agent=agent_name,
                    title=f"ReAct step {step_index}",
                    output=output,
                    log_ref=session_ref,
                    call_id=result.call_id,
                    llm_attempts=result.attempts,
                    transport_retries=max(0, result.attempts - 1),
                    json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
                )
            )
            if submitted:
                break
    except Exception as exc:
        task_log.close_agent_session(
            turns=steps_taken,
            tool_calls_made=probes_made,
            total_tokens=total_tokens,
            completed=False,
            outcome="error",
        )
        if isinstance(exc, TendError):
            exc.with_context(react_step_traces=[asdict(trace) for trace in traces])
        raise

    if not final_mql:
        # Budget exhausted without submit: fail honestly (typed zero-score failure),
        # never score an exploratory probe as the prediction.
        task_log.close_agent_session(
            turns=steps_taken,
            tool_calls_made=probes_made,
            total_tokens=total_tokens,
            completed=False,
            outcome="budget_exhausted",
        )
        raise ContractViolationError(
            "react baseline exhausted its step budget without submitting a final MQL",
            context={
                "baseline_id": spec.id,
                "db_id": db_id,
                "max_steps": REACT_MAX_STEPS,
                "react_step_traces": [asdict(trace) for trace in traces],
            },
        )
    task_log.close_agent_session(
        turns=steps_taken,
        tool_calls_made=probes_made,
        total_tokens=total_tokens,
        completed=True,
        outcome="submitted",
    )
    return final_mql, traces


def _react_collection_names(mongo: Any, db_id: str) -> list[str] | None:
    """Real collection names for the informed arm; None degrades to the naive prompt."""
    if mongo is None:
        return None
    try:
        raw = mongo.list_collections(db_id)
    except Exception:  # noqa: BLE001 - offline/stub Mongo degrades, never crashes
        return None
    names = [
        str(item.get("collection") if isinstance(item, dict) else item)
        for item in (raw or [])
        if item
    ]
    names = sorted(name for name in names if name)
    return names or None


async def _react_observation(
    mongo: Any,
    db_id: str,
    collection: str,
    pipeline: list[dict[str, Any]],
) -> str:
    """RAW first-N rows + total count, exactly the published harness's channel.

    Executor faults (banned operators, parse errors, connection loss) become an
    `ERROR: ...` observation the model can react to, never a crash.
    """
    if mongo is None:
        return "ERROR: no database handle available"
    try:
        mql = render_mql(collection, pipeline)
        rows = await asyncio.to_thread(mongo.norm_exec, db_id, mql)
    except Exception as exc:  # noqa: BLE001 - executor faults are model feedback
        detail = (
            getattr(exc, "context", {}).get("error")
            if isinstance(getattr(exc, "context", None), dict)
            else None
        )
        return f"ERROR: {str(detail or exc)[:200]}"
    head = json.dumps(rows[:REACT_OBSERVATION_ROWS], default=str)
    return (
        f"rows={len(rows)}; first {min(REACT_OBSERVATION_ROWS, len(rows))}: "
        + head[:REACT_OBSERVATION_CHAR_CAP]
    )


async def _preload_agentic_witnesses(
    ctx: Any,
    db_id: str,
    local_data: dict[str, list[dict[str, Any]]] | None,
    log: Any,
) -> None:
    mongo = getattr(ctx, "mongo", None)
    if mongo is None or not local_data or not hasattr(mongo, "load_witness"):
        return
    try:
        await asyncio.to_thread(mongo.load_witness, db_id, local_data)
    except Exception as exc:  # noqa: BLE001 - offline/stub Mongo is non-fatal for the loop
        log.info(
            "agentic_witness_preload_skipped",
            db_id=db_id,
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )


def _baseline_disclosure(
    wf: Workflow,
    spec: BaselineSpec,
    *,
    witness_k: int,
    schema_stripped_fields: list[str] | None = None,
    record_stripped_fields: list[str] | None = None,
    local_data_stripped_fields: list[str] | None = None,
    schema_public_shape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_ids = [wf.ctx.settings.llm.model, *wf.ctx.settings.llm.agent_models.values()]
    allow_list = load_solver_allow_list(wf.ctx.settings.paths.schemas)
    disjointness = check_disjointness(
        model_ids,
        allow_list,
        require_manifests=not wf.ctx.settings.stub,
    )
    disclosure: dict[str, Any] = {
        "baseline_id": spec.id,
        "baseline_title": spec.title,
        "backbone": wf.ctx.settings.llm.model,
        "s_solver": sorted(set(model_ids)),
        "no_training": True,
        "uses_train_json": False,
        "uses_gold_mql": False,
        # The ReAct arm self-acquires structure by running read-only execute_mql probes,
        # so it consumes execution feedback; the step baselines do not.
        "uses_execution_feedback": bool(spec.react_arm),
        "agentic": False,
        "prompt_channel": spec.prompt_channel,
        "schema_source": (
            "self_acquired_via_execute_mql"
            if spec.react_arm
            else {
                "relational_source_schema": "bird_relational_ddl_plus_witness_samples",
            }.get(spec.prompt_channel, "witness_samples_only")
        ),
        "schema_provided_to_model": spec.prompt_channel == "relational_source_schema",
        "disjointness_ok": disjointness["ok"],
        "disjointness_detail": disjointness,
        "limitations": list(spec.limitations),
        "r_max": 0,  # baselines have no retry loop
        "witness_k": witness_k,
        "json_repair_retries": BASELINE_JSON_REPAIR_RETRIES,
        "public_schema_version": PUBLIC_SCHEMA_VERSION,
        "schema_sanitizer_applied": True,
        "record_sanitizer_applied": True,
        "local_data_sanitizer_applied": True,
        "schema_stripped_fields": list(schema_stripped_fields or []),
        "record_stripped_fields": list(record_stripped_fields or []),
        "local_data_stripped_fields": list(local_data_stripped_fields or []),
        "schema_public_shape": schema_public_shape
        or {"format": "unknown", "collection_total": 0, "collections": []},
        "uses_public_witness_digest": True,
        "semantic_retry_budget": 0,
        "retry_contract": {
            "semantic_retry_budget": 0,
            "json_repair_retries": BASELINE_JSON_REPAIR_RETRIES,
            "format_transport_retries_are_semantic_retries": False,
            "format_transport_retry_scope": "LLM client JSON/transport only",
        },
    }
    if spec.react_arm:
        # The fair ReAct arms see RAW first-N rows (parity with the published fair
        # measurement and with the SAG solver's raw-sample visibility) — declared
        # explicitly because every other baseline observation channel is redacted.
        disclosure.update(
            {
                "react_arm": spec.react_arm,
                "max_steps": REACT_MAX_STEPS,
                "informed_collection_names": spec.react_arm == "informed",
                "raw_observation_rows": REACT_OBSERVATION_ROWS,
                "observation_char_cap": REACT_OBSERVATION_CHAR_CAP,
                "uses_public_witness_digest": False,
            }
        )
    return disclosure


def _extract_mql(state: dict[str, Any]) -> str:
    value = state.get("MQL") or state.get("mql")
    return str(value or "")


def _hash_nlq(nlq: str) -> str:
    return "sha256:" + hashlib.sha256(nlq.encode("utf-8")).hexdigest()
