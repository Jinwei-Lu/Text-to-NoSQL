"""Runtime workflow for constrained LLM baselines."""
from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import Anomaly, SourceError, TendError, wrap_unexpected
from ..execution.ast_check import static_mql_feedback
from .boundary import (
    PUBLIC_SCHEMA_VERSION,
    check_disjointness,
    load_solver_allow_list,
    public_schema_shape,
    sanitize_public_local_data,
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


BASELINE_JSON_REPAIR_RETRIES = 0


@dataclass(frozen=True, slots=True)
class BaselineStepTrace:
    step_id: str
    agent: str
    title: str
    transcript_ref: str
    diagnostics_ref: str
    output: dict[str, Any]
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
    transcript_refs: list[str] = field(default_factory=list)
    diagnostics_refs: list[str] = field(default_factory=list)
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
    transcript_refs: list[str] = field(default_factory=list)
    diagnostics_refs: list[str] = field(default_factory=list)
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


class _BaselineSessionRecorder:
    """One markdown session transcript for a baseline record."""

    def __init__(
        self,
        *,
        run_dir: Path,
        spec: BaselineSpec,
        record: dict[str, Any],
        db_id: str,
        record_id: Any,
        input_mode: str,
        nlq_track: str,
        witness_k: int,
        batch_index: int | None,
        disclosure: dict[str, Any],
        schema_summary: dict[str, Any],
        witness_digest: dict[str, Any],
    ) -> None:
        self.run_dir = run_dir
        self.spec = spec
        self.record = record
        self.db_id = db_id
        self.record_id = record_id
        self.input_mode = input_mode
        self.nlq_track = nlq_track
        self.witness_k = witness_k
        self.batch_index = batch_index
        self.disclosure = disclosure
        self.schema_summary = schema_summary
        self.witness_digest = witness_digest
        self.started_at = _utc_now()
        self.session_id = self._build_session_id()
        self.agent_session_ref = f"baseline/sessions/{self.session_id}/agent.md"
        self.transcript_refs: list[str] = []
        self.diagnostics_refs: list[str] = []
        self.steps: list[dict[str, Any]] = []
        self.static_feedback: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.final_outcome: dict[str, Any] = {}

    @property
    def last_transcript_ref(self) -> str | None:
        return self.transcript_refs[-1] if self.transcript_refs else None

    @property
    def last_diagnostics_ref(self) -> str | None:
        return self.diagnostics_refs[-1] if self.diagnostics_refs else None

    def add_step_success(
        self,
        *,
        step: Any,
        messages: list[dict[str, Any]],
        result: Any,
        output: dict[str, Any],
    ) -> None:
        self._remember_refs(result.transcript_ref, result.diagnostics_ref)
        self.steps.append(
            {
                "step_id": step.id,
                "agent": step.agent,
                "title": step.title,
                "status": "ok",
                "model": result.model,
                "messages": messages,
                "model_output": result.text,
                "parsed_output": output,
                "transcript_ref": result.transcript_ref,
                "diagnostics_ref": result.diagnostics_ref,
                "llm_attempts": result.attempts,
                "transport_retries": max(0, result.attempts - 1),
                "json_repair_retries": BASELINE_JSON_REPAIR_RETRIES,
            }
        )

    def add_step_error(
        self,
        *,
        step: Any,
        messages: list[dict[str, Any]],
        error: Exception,
    ) -> None:
        transcript_ref = _error_context_value(error, "transcript_ref")
        diagnostics_ref = _error_context_value(error, "diagnostics_ref")
        self._remember_refs(transcript_ref, diagnostics_ref)
        error_record = _error_record(error)
        self.steps.append(
            {
                "step_id": step.id,
                "agent": step.agent,
                "title": step.title,
                "status": "failed",
                "messages": messages,
                "model_output": error_record.get("model_output")
                or "Unavailable; see diagnostics_ref.",
                "parsed_output": None,
                "transcript_ref": transcript_ref,
                "diagnostics_ref": diagnostics_ref,
                "error": error_record,
            }
        )

    def add_static_feedback(self, label: str, feedback: list[dict[str, Any]]) -> None:
        self.static_feedback.append({"label": label, "feedback": feedback})

    def add_error(self, error_code: str, message: str, **fields: Any) -> None:
        self.errors.append({"error_code": error_code, "message": message, **fields})

    def finish(self, **fields: Any) -> None:
        self.final_outcome = fields

    def write(self) -> None:
        path = self.run_dir / self.agent_session_ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")

    def _build_session_id(self) -> str:
        record_part = "no_record" if self.record_id is None else str(self.record_id)
        batch_part = "single" if self.batch_index is None else f"batch_{self.batch_index:04d}"
        ts_part = _slug(self.started_at.replace("+00:00", "Z"))
        return "_".join(
            _slug(part)
            for part in (batch_part, self.spec.id, self.db_id, record_part, ts_part)
            if part
        )

    def _remember_refs(
        self,
        transcript_ref: str | None,
        diagnostics_ref: str | None,
    ) -> None:
        if transcript_ref and transcript_ref not in self.transcript_refs:
            self.transcript_refs.append(transcript_ref)
        if diagnostics_ref and diagnostics_ref not in self.diagnostics_refs:
            self.diagnostics_refs.append(diagnostics_ref)

    def render(self) -> str:
        lines: list[str] = [
            f"# Baseline Session: {self.spec.title}",
            "",
            "## Metadata",
            "- Stage: BASELINE",
            "- Task: baseline_record",
            f"- Model: {self.disclosure.get('backbone', 'unknown')}",
            f"- Started: {self.started_at}",
            f"- Input Mode: {self.input_mode}",
            f"- NLQ Track: {self.nlq_track}",
            f"- Witness K: {self.witness_k}",
            "",
            "## Record Metadata",
            f"- Baseline ID: {self.spec.id}",
            f"- Baseline Title: {self.spec.title}",
            f"- DB ID: {self.db_id}",
            f"- Record ID: {self.record_id}",
            f"- Batch Index: {self.batch_index}",
            "",
            "## Public Input Summary",
            _json_block(
                {
                    "record": self.record,
                    "schema_summary": self.schema_summary,
                    "witness_digest": self.witness_digest,
                    "schema_public_shape": self.disclosure.get("schema_public_shape"),
                    "stripped_fields": {
                        "record": self.disclosure.get("record_stripped_fields", []),
                        "schema": self.disclosure.get("schema_stripped_fields", []),
                        "local_data": self.disclosure.get("local_data_stripped_fields", []),
                    },
                }
            ),
            "",
            "## Steps",
        ]
        if not self.steps:
            lines.append("No model steps completed.")
        for index, step in enumerate(self.steps, start=1):
            lines.extend(
                [
                    "",
                    f"### Step {index}: {step['title']}",
                    f"- Step ID: {step['step_id']}",
                    f"- Agent: {step['agent']}",
                    f"- Status: {step['status']}",
                    f"- Model: {step.get('model', 'unknown')}",
                    "### Messages",
                    _json_block(step.get("messages", [])),
                    "### Model Output",
                    _json_block(step.get("model_output")),
                    "### Parsed Output",
                    _json_block(step.get("parsed_output")),
                    "### Diagnostics",
                    _json_block(
                        {
                            "transcript_ref": step.get("transcript_ref"),
                            "diagnostics_ref": step.get("diagnostics_ref"),
                            "llm_attempts": step.get("llm_attempts"),
                            "transport_retries": step.get("transport_retries"),
                            "json_repair_retries": step.get("json_repair_retries"),
                            "error": step.get("error"),
                        }
                    ),
                ]
            )
        lines.extend(
            [
                "",
                "## Static Feedback",
                _json_block(self.static_feedback),
                "",
                "## Errors",
                _json_block(self.errors),
                "",
                "## Diagnostics Refs",
                _json_block(
                    {
                        "transcript_refs": self.transcript_refs,
                        "diagnostics_refs": self.diagnostics_refs,
                    }
                ),
                "",
                "## Final Outcome",
                _json_block(self.final_outcome),
                "",
            ]
        )
        return "\n".join(lines)


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
    log = wf.ctx.log.bind(component="baseline_suite")
    log.info(
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
            input_mode=input_mode,
            nlq_track=effective_nlq_track,
            evaluation_skip_reason=evaluation_skip_reason,
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
    input_mode: str = "release",
    nlq_track: str = "record",
    evaluation_skip_reason: str | None = None,
) -> BaselinePrediction | BaselineFailure:
    base_log = wf.ctx.log.bind(
        component="baseline_runner", baseline_id=spec.id, batch_index=batch_index
    )
    sanitized_record = sanitize_public_record(record)
    sanitized_schema = sanitize_public_schema(schema)
    sanitized_local_data = sanitize_public_local_data(local_data)
    if sanitized_record.stripped_fields:
        base_log.info("baseline_record_fields_stripped", fields=sanitized_record.stripped_fields)
    if sanitized_schema.stripped_fields:
        base_log.info("baseline_schema_fields_stripped", fields=sanitized_schema.stripped_fields)
    if sanitized_local_data.stripped_fields:
        base_log.info(
            "baseline_local_data_fields_stripped",
            fields=sanitized_local_data.stripped_fields,
        )
    safe = sanitized_record.value
    public_schema = sanitized_schema.value
    db_id = str(safe["db_id"])
    record_id = safe.get("record_id")
    actual_nlq_track = str(record.get("nlq_track") or nlq_track)
    disclosure = _baseline_disclosure(
        wf,
        spec,
        witness_k=witness_k,
        schema_stripped_fields=sanitized_schema.stripped_fields,
        record_stripped_fields=sanitized_record.stripped_fields,
        local_data_stripped_fields=sanitized_local_data.stripped_fields,
        schema_public_shape=public_schema_shape(public_schema),
    )
    schema_summary = summarize_schema(public_schema)
    witness_digest = build_witness_digest(
        sanitized_local_data.value if local_data is not None else None,
        witness_k,
    )
    session = _BaselineSessionRecorder(
        run_dir=wf.ctx.log.run_dir,
        spec=spec,
        record=safe,
        db_id=db_id,
        record_id=record_id,
        input_mode=input_mode,
        nlq_track=actual_nlq_track,
        witness_k=witness_k,
        batch_index=batch_index,
        disclosure=disclosure,
        schema_summary=schema_summary,
        witness_digest=witness_digest,
    )
    nlq_hash = ""
    try:
        # Baselines expose only the canonical NLQ track after record sanitization.
        nlq = _canonical_nlq(safe, use_colloquial=False)
        nlq_hash = _hash_nlq(nlq)
    except TendError as err:
        err.with_context(baseline_id=spec.id, db_id=db_id, record_id=record_id)
        error_code = err.anomaly.value if err.anomaly else "prompt_error"
        session.add_error(error_code, err.message, error=_error_record(err))
        session.finish(
            result_type="baseline_failure",
            status="failed",
            error_code=error_code,
            message=err.message,
        )
        session.write()
        base_log.anomaly(err, **_session_ref_fields(session))
        return BaselineFailure(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            error_code=error_code,
            message=err.message,
            disclosure=disclosure,
            agent_session_ref=session.agent_session_ref,
            transcript_refs=list(session.transcript_refs),
            diagnostics_refs=list(session.diagnostics_refs),
            witness_k=witness_k,
            r_max=0,
            input_mode=input_mode,
            nlq_track=actual_nlq_track,
            nlq_hash=nlq_hash,
            evaluation_skip_reason=evaluation_skip_reason,
        )
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
                       batch_index=batch_index,
                       agent_session_ref=session.agent_session_ref)
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
                session.add_static_feedback("pre_repair", static_feedback)
            output, trace = await _run_step(
                ctx,
                spec,
                step,
                prompt_ctx,
                state,
                group,
                batch_index=batch_index,
                session=session,
            )
            traces.append(trace)
            state.update(output)

        mql = _extract_mql(state)
        final_feedback = static_mql_feedback(mql)
        static_feedback = static_feedback or final_feedback
        session.add_static_feedback("final", final_feedback)
        if any(item["severity"] == "error" for item in final_feedback):
            session.add_error(
                "STATIC_INVALID_MQL",
                "baseline produced statically invalid MQL",
                feedback=final_feedback,
            )
            session.finish(
                result_type="baseline_failure",
                status="failed",
                error_code="STATIC_INVALID_MQL",
                message="baseline produced statically invalid MQL",
                static_feedback=final_feedback,
                mql_preview=mql[:240],
            )
            session.write()
            log.anomaly(
                kind=Anomaly.PARSE_ERROR,
                message="baseline produced statically invalid MQL",
                baseline_id=spec.id,
                feedback=final_feedback,
                **_session_ref_fields(session),
            )
            return BaselineFailure(
                baseline_id=spec.id,
                baseline_title=spec.title,
                record_id=record_id,
                db_id=db_id,
                error_code="STATIC_INVALID_MQL",
                message="baseline produced statically invalid MQL",
                disclosure=disclosure,
                agent_session_ref=session.agent_session_ref,
                transcript_refs=list(session.transcript_refs),
                diagnostics_refs=list(session.diagnostics_refs),
                witness_k=witness_k,
                r_max=0,
                input_mode=input_mode,
                nlq_track=actual_nlq_track,
                nlq_hash=nlq_hash,
                evaluation_skip_reason=evaluation_skip_reason,
                steps=traces,
                static_feedback=final_feedback,
            )

        log.info(
            "baseline_record_done",
            status="ok",
            mql_preview=mql[:240],
            steps=len(traces),
            transcript_refs=list(session.transcript_refs),
            diagnostics_refs=list(session.diagnostics_refs),
        )
        session.finish(
            result_type="baseline_prediction",
            status="ok",
            MQL=mql,
            static_feedback=final_feedback,
            steps=len(traces),
        )
        session.write()
        return BaselinePrediction(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            MQL=mql,
            disclosure=disclosure,
            agent_session_ref=session.agent_session_ref,
            transcript_refs=list(session.transcript_refs),
            diagnostics_refs=list(session.diagnostics_refs),
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
        session.add_error(
            error_code,
            err.message,
            error=_error_record(err),
            transcript_ref=session.last_transcript_ref,
            diagnostics_ref=session.last_diagnostics_ref,
        )
        session.finish(
            result_type="baseline_failure",
            status="failed",
            error_code=error_code,
            message=err.message,
            static_feedback=final_feedback or static_feedback,
        )
        session.write()
        if not err.logged:
            log.anomaly(err, **_session_ref_fields(session))
        return BaselineFailure(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            error_code=error_code,
            message=err.message,
            disclosure=disclosure,
            agent_session_ref=session.agent_session_ref,
            transcript_refs=list(session.transcript_refs),
            diagnostics_refs=list(session.diagnostics_refs),
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
        session.add_error(
            "internal",
            err.message,
            error=_error_record(err),
            transcript_ref=session.last_transcript_ref,
            diagnostics_ref=session.last_diagnostics_ref,
        )
        session.finish(
            result_type="baseline_failure",
            status="failed",
            error_code="internal",
            message=err.message,
            static_feedback=final_feedback or static_feedback,
        )
        session.write()
        log.anomaly(err, **_session_ref_fields(session))
        return BaselineFailure(
            baseline_id=spec.id,
            baseline_title=spec.title,
            record_id=record_id,
            db_id=db_id,
            error_code="internal",
            message=err.message,
            disclosure=disclosure,
            agent_session_ref=session.agent_session_ref,
            transcript_refs=list(session.transcript_refs),
            diagnostics_refs=list(session.diagnostics_refs),
            witness_k=witness_k,
            r_max=0,
            input_mode=input_mode,
            nlq_track=actual_nlq_track,
            nlq_hash=nlq_hash,
            evaluation_skip_reason=evaluation_skip_reason,
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
    session: _BaselineSessionRecorder | None = None,
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
    log_fields = {
        "agent": step.agent,
        "baseline_id": spec.id,
        "baseline_step": step.id,
        "batch_index": batch_index,
    }
    if session is not None:
        log_fields["agent_session_ref"] = session.agent_session_ref
    log = ctx.log.bind(**log_fields)
    log.info("baseline_step_start", title=step.title)
    messages: list[dict[str, Any]] = []
    try:
        messages = step.build_messages(prompt_ctx, state)
        result = await ctx.llm.complete(
            agent=step.agent,
            messages=messages,
            logger=log,
            schema=step.schema,
            temperature=0.0,
            json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
        )
        output = result.data
        log.info(
            "baseline_step_done",
            transcript_ref=result.transcript_ref,
            diagnostics_ref=result.diagnostics_ref,
            llm_attempts=result.attempts,
            transport_retries=max(0, result.attempts - 1),
            json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
        )
        if session is not None:
            session.add_step_success(
                step=step,
                messages=messages,
                result=result,
                output=output,
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
            llm_attempts=result.attempts,
            transport_retries=max(0, result.attempts - 1),
            json_repair_retries=BASELINE_JSON_REPAIR_RETRIES,
        )
    except Exception as exc:
        if session is not None:
            session.add_step_error(step=step, messages=messages, error=exc)
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


def _extract_mql(state: dict[str, Any]) -> str:
    value = state.get("MQL") or state.get("mql")
    return str(value or "")


def _hash_nlq(nlq: str) -> str:
    return "sha256:" + hashlib.sha256(nlq.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _slug(value: Any) -> str:
    text = str(value)
    cleaned = [char.lower() if char.isalnum() else "_" for char in text]
    slug = "_".join(part for part in "".join(cleaned).split("_") if part)
    return slug[:96] or "unknown"


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    ) + "\n```"


def _error_context_value(error: Exception, key: str) -> str | None:
    if isinstance(error, TendError):
        value = error.context.get(key)
        return str(value) if value else None
    return None


def _error_record(error: Exception) -> dict[str, Any]:
    if isinstance(error, TendError):
        return error.to_record()
    return {
        "error_type": type(error).__name__,
        "message": str(error),
        "anomaly": Anomaly.INTERNAL.value,
    }


def _session_ref_fields(session: _BaselineSessionRecorder) -> dict[str, Any]:
    fields: dict[str, Any] = {"agent_session_ref": session.agent_session_ref}
    if session.last_transcript_ref:
        fields["transcript_ref"] = session.last_transcript_ref
    if session.last_diagnostics_ref:
        fields["diagnostics_ref"] = session.last_diagnostics_ref
    return fields
