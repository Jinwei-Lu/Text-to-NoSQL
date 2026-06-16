"""Runtime workflow for constrained LLM baselines."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os as _os
import traceback
from dataclasses import asdict, dataclass, field, replace
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
from ..execution.mongo import equiv_rec_values
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
from .mongo_probe import ReadonlyMongoProbe
from .strategies import (
    REACT_ACTION_SCHEMA,
    BaselinePromptContext,
    BaselineSpec,
    build_react_system_prompt,
    normalize_react_think_output,
    resolve_baselines,
)

# Bound on the agentic baseline's ReAct tool-call loop. Default 8 keeps the original
# experiment; `TEND_BASELINE_AGENTIC_MAX_TURNS` raises it for a fair-exploration comparison
# (so the one baseline that *can* probe the DB gets a budget comparable to the solver's,
# rather than running out before it can both discover the schema and submit a query).
AGENTIC_MAX_TURNS = max(1, int(_os.environ.get("TEND_BASELINE_AGENTIC_MAX_TURNS", "8")))

# Bound on the fair ReAct arms' JSON-action loop. Default 16 reproduces the published
# fair-comparison measurement (react_naive 4/110, react_informed 25/110 on financial).
REACT_MAX_STEPS = max(1, int(_os.environ.get("TEND_BASELINE_REACT_MAX_STEPS", "16")))

# The fair ReAct arms see RAW first-N rows (the published measurement's observation
# channel — disclosed in `_baseline_disclosure`, unlike every redacted baseline).
REACT_OBSERVATION_ROWS = 5
REACT_OBSERVATION_CHAR_CAP = 2000


# Bounded JSON/schema repair only — not execution feedback or extra LLM steps.
BASELINE_JSON_REPAIR_RETRIES = 2

# Single self-acquired preprocessing exploration. When `--witness-k 0` (fairness mode) and
# a read-only Mongo handle exists, non-agentic baselines no longer receive proactively
# pre-fed witness samples; instead they emit ONE exploratory MQL whose (bounded, read-only,
# value-redacted) execution result becomes the prior context for generation — the same way
# the SMART-EG solver must induce structure by querying the DB. This is NOT agentic: exactly
# one probe, no tool loop. The agent name must not end in `_sql`/`_plan`/`_think` so the
# offline stub returns a bounded `MQL` field for it.
PREPROCESS_EXPLORE_AGENT = "baseline_preprocess_explore"
PREPROCESS_EXPLORE_LIMIT = 5
PREPROCESS_EXPLORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "MQL": {
            "type": "string",
            "description": "One read-only db.<collection>.aggregate([...]) exploration query.",
            "minLength": 8,
        },
        "rationale": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["MQL"],
    "additionalProperties": True,
}

# Non-agentic baselines self-acquire structure from sampled documents only (no curated
# schema). `data_rich_direct` is the "more data, still no exploration" contrast: it sees a
# larger document sample than the other one-shot baselines.
_BASELINE_WITNESS_K_OVERRIDE = {
    "data_rich_direct": 8,
    # Channel-pure arms: no sampled documents regardless of the suite's witness budget.
    "direct_nlq_only": 0,
    "schema_direct": 0,
}

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
            f"baseline:{batch_index}:{spec.id}:{record.get('db_id')}:"
            f"{record.get('record_id')}"
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
    # Non-agentic baselines never receive a curated schema; they must infer structure from
    # sampled documents. `data_rich_direct` is the larger-sample contrast -- but the boost
    # only applies in a sampled regime. A `--witness-k 0` (no-sample / fairness) run forces
    # every baseline to 0 so none keeps a privileged raw-document view the solver is denied.
    effective_witness_k = _BASELINE_WITNESS_K_OVERRIDE.get(spec.id, witness_k) if witness_k > 0 else 0
    disclosure = _baseline_disclosure(
        wf,
        spec,
        witness_k=effective_witness_k,
        schema_stripped_fields=sanitized_schema.stripped_fields,
        record_stripped_fields=sanitized_record.stripped_fields,
        local_data_stripped_fields=sanitized_local_data.stripped_fields,
        schema_public_shape=public_schema_shape(public_schema),
    )
    # Schema is computed only for disclosure/leakage accounting; it is NOT shown to the model.
    schema_summary: dict[str, Any] = {}
    # Fairness mode: redact scalar values so non-agentic baselines see document structure
    # (field paths, nesting, types) without raw answer values -- the exploration result a
    # tool-less baseline cannot obtain itself, but not the privileged raw rows.
    _redact_witness = _os.environ.get("TEND_BASELINE_REDACT_WITNESS_VALUES", "0") == "1"
    witness_digest = build_witness_digest(
        sanitized_local_data.value if local_data is not None else None,
        effective_witness_k,
        redact_values=_redact_witness,
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
        f"baseline:{batch_index}:{spec.id}"
        if batch_index is not None
        else f"baseline:{spec.id}"
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

    # Fairness preprocessing: when no samples were proactively fed (`--witness-k 0`) and the
    # baseline is non-agentic, let it self-acquire structure via ONE exploratory probe before
    # generation — instead of being handed raw witness rows the solver is denied. Agentic
    # baselines already self-explore; sampled (`witness_k > 0`) runs keep the legacy view.
    if (
        not spec.agentic
        and not spec.react_arm
        and spec.prompt_channel == "sampled_docs"  # channel-pure arms never self-acquire
        and effective_witness_k == 0
        and getattr(ctx, "mongo", None) is not None
    ):
        preprocess_digest = await _run_preprocess_exploration(
            ctx,
            spec,
            nlq=nlq,
            db_id=db_id,
            record_id=record_id,
            group=group,
            batch_index=batch_index,
            task_log=task_log,
        )
        if preprocess_digest.get("__self_acquired__"):
            witness_digest = preprocess_digest
            disclosure["schema_source"] = "self_acquired_via_preprocess_probe"
            disclosure["uses_preprocess_exploration"] = True
            disclosure["proactive_db_info"] = False
            disclosure["uses_execution_feedback"] = True
            disclosure["preprocess_probe_mql"] = preprocess_digest.get("__probe_mql__", "")
    prompt_ctx = BaselinePromptContext(
        record=safe,
        schema=public_schema,
        witness_digest=witness_digest,
        schema_summary=schema_summary,
        nlq=nlq,
    )

    state: dict[str, Any] = {}
    traces: list[BaselineStepTrace] = []
    static_feedback: list[dict[str, Any]] = []
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
        elif spec.consistency_k > 1:
            sc_mql, sc_traces = await _run_consistency_baseline(
                ctx,
                spec,
                prompt_ctx,
                db_id=db_id,
                sanitized_local_data=sanitized_local_data.value,
                group=group,
                batch_index=batch_index,
                task_log=task_log,
            )
            traces.extend(sc_traces)
            state["MQL"] = sc_mql
        else:
            for step in spec.steps:
                if spec.id == "static_self_debug" and step.id == "repair":
                    static_feedback = static_mql_feedback(_extract_mql(state))
                    state["static_feedback"] = static_feedback
                    task_log.info(
                        "baseline_static_feedback",
                        label="pre_repair",
                        feedback=static_feedback,
                    )
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
                state.update(output)

        mql = _extract_mql(state)
        final_feedback = static_mql_feedback(mql)
        static_feedback = static_feedback or final_feedback
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
            static_feedback=final_feedback or static_feedback,
        )


def _largest_result_cluster(results: list[list[dict[str, Any]] | None]) -> tuple[int, int]:
    """SAG v3's clustering rule over plain candidate results.

    Order-insensitive result-equivalence clusters; the largest cluster wins; ties and
    the representative both prefer (non-empty, earliest). ``None`` (execution failed)
    never merges — each failed candidate is its own singleton.
    """
    clusters: list[list[int]] = []
    for i, rows in enumerate(results):
        placed = False
        if rows is not None:
            for cl in clusters:
                ref = results[cl[0]]
                if ref is not None and equiv_rec_values(rows, ref, order_sensitive=False):
                    cl.append(i)
                    placed = True
                    break
        if not placed:
            clusters.append([i])

    def _empty(i: int) -> int:
        return 0 if results[i] else 1  # None or [] count as empty

    clusters.sort(key=lambda cl: (-len(cl), min((_empty(i), i) for i in cl)))
    members = clusters[0]
    best = min(members, key=lambda i: (_empty(i), i))
    return best, len(members)


async def _run_consistency_baseline(
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
    """Compute-matched k-sample self-consistency over a non-agentic step strategy.

    Mirrors SAG v3's selection exactly: k independent decodes of the SAME prompts;
    every candidate executes read-only once; candidates cluster by order-insensitive
    result equivalence and the largest cluster's representative wins. Execution is
    selection-only — nothing feeds back into any prompt. Without an executable world
    (stub / Mongo down) the arm degrades to k=1, like the solver's stub path.
    """
    mongo = getattr(ctx, "mongo", None)
    available = getattr(mongo, "available", None)
    can_execute = (
        not bool(getattr(ctx.settings, "stub", False))
        and mongo is not None
        and (not callable(available) or bool(available()))
    )
    k = spec.consistency_k if can_execute else 1
    if k > 1:
        await _preload_agentic_witnesses(ctx, db_id, sanitized_local_data, task_log)

    async def one_attempt(attempt: int) -> tuple[str, list[BaselineStepTrace]]:
        state: dict[str, Any] = {}
        traces: list[BaselineStepTrace] = []
        for step in spec.steps:
            attempt_step = (
                replace(
                    step,
                    id=f"{step.id}_k{attempt + 1}",
                    title=f"{step.title} (attempt {attempt + 1}/{k})",
                )
                if k > 1
                else step
            )
            output, trace = await _run_step(
                ctx,
                spec,
                attempt_step,
                prompt_ctx,
                state,
                group,
                batch_index=batch_index,
                task_log=task_log,
            )
            traces.append(trace)
            state.update(output)
        return _extract_mql(state), traces

    outcomes = await asyncio.gather(
        *[one_attempt(i) for i in range(k)], return_exceptions=True
    )
    candidates: list[tuple[int, str]] = []
    all_traces: list[BaselineStepTrace] = []
    first_error: BaseException | None = None
    for i, item in enumerate(outcomes):
        if isinstance(item, BaseException):
            first_error = first_error or item
            continue
        mql, traces = item
        candidates.append((i, mql))
        all_traces.extend(traces)
    if not candidates:
        assert first_error is not None  # gather returned only exceptions
        raise first_error
    if len(candidates) == 1:
        return candidates[0][1], all_traces

    results: list[list[dict[str, Any]] | None] = []
    for _attempt, mql in candidates:
        try:
            rows = await asyncio.to_thread(mongo.norm_exec, db_id, mql)
        except Exception:  # noqa: BLE001 - a failed candidate is a singleton cluster
            rows = None
        results.append(rows)
    chosen, cluster_size = _largest_result_cluster(results)
    task_log.info(
        "baseline_consistency_selected",
        k=k,
        decoded=len(candidates),
        executed=sum(1 for rows in results if rows is not None),
        chosen_attempt=candidates[chosen][0] + 1,
        cluster_size=cluster_size,
    )
    return candidates[chosen][1], all_traces


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
        if step.agent == "baseline_react_lite_think":
            output = normalize_react_think_output(output)
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
            llm_attempts=result.attempts,
            transport_retries=max(0, result.attempts - 1),
            json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
        )
    except Exception:
        if ctx.progress:
            ctx.progress.finish_task(task_id, ok=False)
        raise


async def _run_preprocess_exploration(
    ctx: Any,
    spec: BaselineSpec,
    *,
    nlq: str,
    db_id: str,
    record_id: Any,
    group: str,
    batch_index: int | None,
    task_log: TaskLogger,
) -> dict[str, Any]:
    """Run ONE self-acquired exploratory probe and return a witness-shaped digest.

    This is the fairness preprocessing step (not an agentic loop): given the NLQ and only
    the collection NAMES, the model emits a single read-only `aggregate` query; its bounded,
    value-redacted execution result becomes the prior context for generation — mirroring how
    the SMART-EG solver induces structure by querying the read-only DB. Any failure (no Mongo
    handle, banned operator, executor fault) degrades gracefully: generation proceeds with an
    empty/feedback-only digest rather than crashing.
    """
    mongo_tools = _agentic_mongo_tools(ctx, db_id)
    if mongo_tools is None:
        task_log.info("baseline_preprocess_skipped", db_id=db_id, reason="no_mongo_handle")
        return {}

    try:
        collection_names = _preprocess_collection_names(mongo_tools)
    except Exception as exc:  # noqa: BLE001 - offline/stub Mongo is a graceful skip
        task_log.info(
            "baseline_preprocess_skipped",
            db_id=db_id,
            reason="list_collections_failed",
            error=str(exc)[:200],
        )
        return {}

    prefix = (
        f"baseline:{batch_index}:{spec.id}"
        if batch_index is not None
        else f"baseline:{spec.id}"
    )
    progress_task_id = f"{prefix}:{db_id}:{record_id}:preprocess_explore"
    if ctx.progress:
        ctx.progress.start_task(progress_task_id, "Preprocess exploration", group=group)
    task_log.set_step_label("preprocess_explore")
    task_log.info(
        "baseline_step_start",
        baseline_id=spec.id,
        step="preprocess_explore",
        agent=PREPROCESS_EXPLORE_AGENT,
        title="Preprocess exploration",
    )

    messages = _preprocess_messages(nlq, db_id, collection_names)
    try:
        result = await ctx.llm.complete(
            agent=PREPROCESS_EXPLORE_AGENT,
            messages=messages,
            task_logger=task_log,
            schema=PREPROCESS_EXPLORE_SCHEMA,
            temperature=0.0,
            json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
        )
    except Exception as exc:
        if ctx.progress:
            ctx.progress.finish_task(progress_task_id, ok=False)
        task_log.info(
            "baseline_preprocess_skipped",
            db_id=db_id,
            reason="llm_failed",
            error=str(exc)[:200],
        )
        return {}

    probe_mql = str((result.data or {}).get("MQL") or "").strip()
    probe_result, probe_error = _run_agentic_probe(mongo_tools, {"MQL": probe_mql})
    digest = _preprocess_digest_from_probe(probe_mql, probe_result, probe_error)
    task_log.info(
        "baseline_step_done",
        step="preprocess_explore",
        call_log=_rel_ref(ctx.log_mgr, task_log.last_llm_call_path),
        probe_error=probe_error,
        probe_mql=probe_mql,
        collections_seen=collection_names,
        llm_attempts=result.attempts,
    )
    if ctx.progress:
        ctx.progress.finish_task(progress_task_id, ok=True)
    return digest


def _preprocess_collection_names(mongo_tools: ReadonlyMongoProbe) -> list[str]:
    """Collection NAMES only — the minimal scaffold to form a query (no structure/values)."""
    listing = mongo_tools.list_collections({})
    names: list[str] = []
    for item in listing.get("collections", []):
        if isinstance(item, dict) and item.get("collection"):
            names.append(str(item["collection"]))
        elif isinstance(item, str) and item:
            names.append(item)
    return names


def _preprocess_messages(nlq: str, db_id: str, collection_names: list[str]) -> list[dict[str, Any]]:
    body = "\n".join(
        [
            "# Self-acquired exploration (single step)",
            f"db_id: {db_id}",
            "",
            "## Natural language question",
            nlq,
            "",
            "## Available collection names",
            "No schema, document structure, or field values are provided — only the names "
            "below. Decide which collection's structure you most need to inspect to answer "
            "the question.",
            _json_block(sorted(collection_names)),
            "",
            "## Task",
            "Emit exactly ONE read-only MongoDB exploration query as a single "
            "`db.<collection>.aggregate([...])` expression. Keep it bounded with a small "
            f"`$limit` (<= {PREPROCESS_EXPLORE_LIMIT}). Never use `$sample`, `$rand`, "
            "`$out`, `$merge`, `$function`, or `$$NOW`. Its execution result will be the only "
            "structural information you receive before writing the final query.",
            "",
            "Return JSON with a single field `MQL`.",
        ]
    )
    return [{"role": "user", "content": body}]


def _preprocess_digest_from_probe(
    probe_mql: str,
    probe_result: dict[str, Any],
    probe_error: str | None,
) -> dict[str, Any]:
    """Shape the single probe result like ``build_witness_digest`` output.

    The bounded read-only probe returns a value-redacted structural shape (field paths,
    type counts, redacted scalar samples) — never raw answer rows — so the generation prompt
    consumes structure without the privileged witness values the solver is denied.
    """
    digest: dict[str, Any] = {"__self_acquired__": True, "__probe_mql__": probe_mql}
    if probe_error is not None:
        digest["__probe_error__"] = probe_error
        return digest
    collection = str(probe_result.get("collection") or "exploration")
    shape = probe_result.get("result_shape")
    sample_documents = (
        [shape] if isinstance(shape, dict) else []
    )
    entry: dict[str, Any] = {
        "sample_count": int(probe_result.get("result_count", 0) or 0),
        "sample_documents": sample_documents,
        "string_values_in_sample": {},
        "result_summary": probe_result.get("result_summary", {}),
    }
    digest[collection] = entry
    return digest


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
                ctx.progress.start_task(
                    progress_task_id, f"ReAct step {step_index}", group=group
                )
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
                        [{"name": "execute_mql", "content": observation}]
                        if observation
                        else None
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
                    llm_attempts=result.attempts,
                    transport_retries=max(0, result.attempts - 1),
                    json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
                )
            )
            if submitted:
                break
    except Exception:
        task_log.close_agent_session(
            turns=steps_taken,
            tool_calls_made=probes_made,
            total_tokens=total_tokens,
            completed=False,
            outcome="error",
        )
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
        detail = getattr(exc, "context", {}).get("error") if isinstance(
            getattr(exc, "context", None), dict
        ) else None
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


def _agentic_mongo_tools(ctx: Any, db_id: str) -> ReadonlyMongoProbe | None:
    mongo = getattr(ctx, "mongo", None)
    if mongo is None:
        return None
    return ReadonlyMongoProbe(mongo, db_id)


def _run_agentic_probe(
    mongo_tools: ReadonlyMongoProbe | None,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Run one redacted read-only probe; return (redacted_result, error_or_None).

    Banned-operator violations (ValueError) and any executor/connection fault are returned
    as a feedback string instead of crashing the loop.
    """
    if mongo_tools is None:
        return {}, "no read-only Mongo handle available"
    request: dict[str, Any] = {}
    collection = arguments.get("collection")
    pipeline = arguments.get("pipeline")
    if collection is not None:
        request["collection"] = collection
    if pipeline is not None:
        request["pipeline"] = pipeline
    if arguments.get("MQL"):
        request["MQL"] = arguments["MQL"]
    if arguments.get("limit") is not None:
        request["limit"] = arguments["limit"]
    try:
        return mongo_tools.run_readonly_probe(request), None
    except ValueError as exc:
        return {}, str(exc)
    except Exception as exc:  # noqa: BLE001 - any executor fault is feedback, not a crash
        return {}, f"{type(exc).__name__}: {str(exc)[:200]}"


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
        # Agentic/react baselines self-acquire structure by running read-only execute_mql
        # probes, so they consume execution feedback; one-shot/step baselines do not.
        "uses_execution_feedback": spec.agentic or bool(spec.react_arm),
        "agentic": spec.agentic,
        "prompt_channel": spec.prompt_channel,
        "schema_source": (
            "self_acquired_via_execute_mql"
            if (spec.agentic or spec.react_arm)
            else {
                "nlq_only": "none_nlq_only",
                "public_schema": "released_public_schema_file",
            }.get(spec.prompt_channel, "witness_samples_only")
        ),
        "schema_provided_to_model": spec.prompt_channel == "public_schema",
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
        "uses_public_witness_digest": spec.prompt_channel == "sampled_docs",
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
    if spec.consistency_k > 1:
        # Compute-matched contrast for SAG v3's k-sample consistency. Candidate
        # execution is SELECTION-ONLY (cluster + pick) — nothing re-enters a prompt,
        # exactly like the solver's clustering stage.
        disclosure.update(
            {
                "uses_result_space_consistency": True,
                "consistency_k": spec.consistency_k,
                "consistency_selection": (
                    "k independent decodes; candidates execute read-only and cluster "
                    "by order-insensitive result equivalence; largest cluster wins "
                    "(SAG v3's rule); no execution feedback enters any prompt"
                ),
            }
        )
    return disclosure


def _extract_mql(state: dict[str, Any]) -> str:
    value = state.get("MQL") or state.get("mql")
    return str(value or "")


def _hash_nlq(nlq: str) -> str:
    return "sha256:" + hashlib.sha256(nlq.encode("utf-8")).hexdigest()


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    ) + "\n```"
