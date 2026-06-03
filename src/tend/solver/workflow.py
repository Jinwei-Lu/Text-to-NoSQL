"""SMART solver workflow from proposals/06_solution_design.md."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from ..errors import Anomaly, DisabledOperatorError, PromptAnomalyError, TendError
from ..workflow import Workflow
from . import agents as _agents  # noqa: F401 - import registers SMART agents
from .contracts import LogicalSpec, PhysicalPlan, SolverDisclosure, SolverPrediction
from .guards import SolverBoundary, render_mql
from .introspection import introspect_solver_database

DEFAULT_R_MAX = 2
DEFAULT_WITNESS_K = 3
_PER_STAGE_PREFIX_INPUT_LIMIT = 200
_PER_STAGE_PREFIX_OUTPUT_LIMIT = 200
_WITNESS_MAX_STRING_CHARS = 160
_WITNESS_MAX_LIST_ITEMS = 8
_WITNESS_MAX_DICT_ITEMS = 24
_WITNESS_MAX_DEPTH = 5
ExecutionMode = Literal["per_stage", "whole_query", "static"]
NlqTrack = Literal["record", "canonical", "colloquial"]

_NATIVE_QUERY_PATTERN_COLLECTION_HINTS: dict[str, str] = {
    "counterparty_operation_symbol_matrix": "counterparty_flow_profiles",
    "disposition_role_card_network": "party_relationship_graphs",
    "district_salary_frequency_segments": "district_market_contexts",
    "financial.activity_orders": "account_ledgers",
    "financial.district_frequency_gender_loan_mix": "district_market_contexts",
    "financial.loan_schedule": "account_ledgers",
    "financial.party_role_card_loan_mix": "party_relationship_graphs",
    "loan_status_repayment_schedule": "account_ledgers",
    "monthly_account_cashflow_matrix": "account_ledgers",
}

_NATIVE_QUERY_PATTERN_PATH_HINTS: dict[str, tuple[str, ...]] = {
    "counterparty_operation_symbol_matrix": ("flows_by_symbol", "operation_by_month"),
    "disposition_role_card_network": ("relationships.members_by_role", "cards"),
    "district_salary_frequency_segments": (
        "accounts_by_frequency",
        "clients_by_gender",
        "district",
    ),
    "financial.activity_orders": ("ledger.standing_orders_by_symbol", "cashflow"),
    "financial.district_frequency_gender_loan_mix": (
        "accounts_by_frequency",
        "clients_by_gender",
        "district",
        "salary_band",
    ),
    "financial.loan_schedule": (
        "loan.repayment_schedule.by_due_month",
        "loan.contract",
        "district_context",
        "identity.service_plan",
    ),
    "financial.party_role_card_loan_mix": (
        "relationships.members_by_role",
        "loan_link",
        "account.district",
    ),
    "loan_status_repayment_schedule": ("loan.repayment_schedule.by_due_month", "loan.contract"),
    "monthly_account_cashflow_matrix": ("cashflow.activity_by_month", "cashflow.monthly_flows"),
}


@dataclass(frozen=True)
class SmartSolveOptions:
    """Runtime switches used by SMART ablations.

    Defaults preserve the reference solver. Ablation runners pass explicit options so
    every run records which mechanism was disabled without forking the solver path.
    """

    solver_variant: str = "full_smart"
    execution_mode: ExecutionMode = "per_stage"
    use_shape_comprehension: bool = True
    use_schema_variants: bool = True
    use_colloquial_nlq: bool = True
    use_intent_contracts: bool = True
    use_preserve_guard: bool = True
    require_variant_handling: bool = True
    use_variant_stratification: bool = True
    allow_local_witness_strata: bool = True
    r_max: int | None = None
    witness_k: int | None = None
    progress_group_prefix: str = "solve"
    progress_work_item_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NlqDbSolverInput:
    record: dict[str, Any]
    schema: dict[str, Any]
    local_data: dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class SolverFailure:
    """Typed terminal solver result that is not a prediction."""

    record_id: int | None
    db_id: str
    error_code: str
    message: str
    disclosure: SolverDisclosure
    shape_model: dict[str, Any]
    logical_spec: dict[str, Any]
    physical_plan: dict[str, Any]
    feedback: list[dict[str, Any]] = field(default_factory=list)
    terminal_feedback: dict[str, Any] | None = None
    result_type: str = "solver_failure"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


async def build_nlq_db_solver_input(
    wf: Workflow,
    *,
    db_id: str,
    nlq: str,
    record_id: int | None = None,
    witness_k: int = DEFAULT_WITNESS_K,
) -> NlqDbSolverInput:
    """Derive the public solver input from only a MongoDB database and an NLQ."""
    snapshot = await asyncio.to_thread(
        introspect_solver_database,
        wf.ctx.mongo,
        db_id,
        sample_size=max(1, witness_k),
    )
    record: dict[str, Any] = {
        "db_id": db_id,
        "nl_queries": {"canonical": nlq},
    }
    if record_id is not None:
        record["record_id"] = record_id
    return NlqDbSolverInput(
        record=record,
        schema=snapshot.schema,
        local_data=snapshot.local_data,
    )


async def smart_solve_nlq_db(
    wf: Workflow,
    *,
    db_id: str,
    nlq: str,
    record_id: int | None = None,
    r_max: int = DEFAULT_R_MAX,
    witness_k: int = DEFAULT_WITNESS_K,
    options: SmartSolveOptions | None = None,
) -> SolverPrediction | SolverFailure:
    """Solve from only NLQ + DB by deriving public context from MongoDB itself."""
    runtime_input = await build_nlq_db_solver_input(
        wf,
        db_id=db_id,
        nlq=nlq,
        record_id=record_id,
        witness_k=witness_k,
    )
    return await smart_solve_record(
        wf,
        runtime_input.record,
        runtime_input.schema,
        local_data=runtime_input.local_data,
        r_max=r_max,
        witness_k=witness_k,
        options=options,
        witness_preloaded=True,
    )


async def smart_solve_record(
    wf: Workflow,
    record: dict[str, Any],
    schema: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    r_max: int = DEFAULT_R_MAX,
    witness_k: int = DEFAULT_WITNESS_K,
    options: SmartSolveOptions | None = None,
    witness_preloaded: bool = False,
) -> SolverPrediction | SolverFailure:
    """Solve one TEND record using the SMART four-stage reference workflow.

    The record may be a release ``test.json`` record. It is sanitized before any stage sees
    it so gold fields such as ``MQL`` and ``shape_policy`` cannot leak into prompts.
    """
    options = options or SmartSolveOptions()
    effective_r_max = _effective_r_max(options, r_max)
    effective_witness_k = _effective_witness_k(options, witness_k)
    schema_for_solver = _strip_schema_variants(schema) if not options.use_schema_variants else schema
    base_log = wf.ctx.log.bind(component="smart_solver", solver_variant=options.solver_variant)
    boundary = SolverBoundary.from_settings(wf.ctx.settings, logger=base_log)
    safe = boundary.sanitize_test_record(record)
    db_id = str(safe["db_id"])
    record_id = safe.get("record_id")
    native_task_context = _solver_native_task_context(safe)
    nlq = _canonical_nlq(safe, use_colloquial=options.use_colloquial_nlq)
    colloquial = (
        (safe.get("nl_queries") or {}).get("colloquial", "")
        if options.use_colloquial_nlq
        else ""
    )
    disclosure = boundary.disclosure(
        wf.ctx.settings,
        r_max=effective_r_max,
        witness_k=effective_witness_k,
    )
    base_log.info("smart_solver_start", db_id=db_id, record_id=record_id,
                  disclosure=disclosure.to_json(), solver_options=options.to_json())

    prefix = options.progress_group_prefix.strip(":") or "solve"
    group = f"{prefix}:{db_id}:{record_id}" if record_id is not None else f"{prefix}:{db_id}"
    if wf.ctx.progress:
        wf.ctx.progress.add_group(group, f"solve {db_id}/{record_id}", phase="SOLVE", total=5)
    ctx = wf.context(
        db_id=db_id,
        record_id=record_id,
        group=group,
        phase="SOLVE",
        work_item_id=options.progress_work_item_id,
        extra={
            **wf.ctx.extra,
            "solver_options": options.to_json(),
            "solver_use_intent_contracts": options.use_intent_contracts,
            "solver_use_preserve_guard": options.use_preserve_guard,
            "solver_require_variant_handling": options.require_variant_handling,
        },
    )

    if options.use_shape_comprehension:
        shape_model = await comprehend_shapes(wf, ctx, nlq, schema_for_solver)
    else:
        shape_model = collapsed_shape_model(schema_for_solver)
        ctx.log.info(
            "smart_solver_shape_ablation",
            reason="shape comprehension disabled; using collapsed schema view",
            collections=sorted(shape_model.get("collections", {})),
        )
    witness_data = local_data if effective_witness_k > 0 else None
    witness_digest = build_witness_digest(witness_data, effective_witness_k)
    agent_native_task_context = _solver_agent_native_task_context(native_task_context)
    agent_shape_model = _focus_native_collection_shape_model(shape_model, agent_native_task_context)
    agent_witness_digest = _focus_native_collection_witness_digest(
        witness_digest,
        agent_native_task_context,
    )
    if effective_witness_k == 0:
        ctx.log.info("smart_solver_witness_ablation", reason="prompt witness digest disabled")
    feedback: dict[str, Any] | None = None
    feedback_log: list[dict[str, Any]] = []
    logical_spec: dict[str, Any] = {}
    physical_plan: dict[str, Any] = {}

    _load_local_data_if_available(ctx, db_id, local_data, witness_preloaded=witness_preloaded)

    for attempt in range(effective_r_max + 1):
        ctx.log.info("smart_solver_attempt", attempt=attempt, feedback=feedback)
        logical_spec = await wf.agent(
            "smart_intent",
            {"nlq": nlq, "colloquial": colloquial, "shape_model": agent_shape_model,
             "feedback": feedback, "native_task_context": agent_native_task_context},
            ctx=ctx,
        )
        assert logical_spec is not None
        spec = LogicalSpec.from_json(logical_spec)

        physical_plan = await wf.agent(
            "smart_plan",
            {
                "logical_spec": spec.to_json(),
                "shape_model": agent_shape_model,
                "witness_digest": agent_witness_digest,
                "feedback": feedback,
                "native_task_context": agent_native_task_context,
            },
            ctx=ctx,
        )
        assert physical_plan is not None
        plan = PhysicalPlan.from_json(physical_plan)

        if options.execution_mode == "per_stage":
            realization = await asyncio.to_thread(
                realize_plan_per_stage,
                ctx,
                boundary,
                db_id=db_id,
                plan=plan,
                shape_policy=spec.shape_policy,
                target_fields=spec.target_fields,
                schema=schema_for_solver,
                shape_model=shape_model,
                local_data=local_data,
                attempt=attempt,
                variant_stratification=options.use_variant_stratification,
                allow_local_witness_strata=options.allow_local_witness_strata,
            )
        elif options.execution_mode == "whole_query":
            realization = await asyncio.to_thread(
                realize_plan_whole_query,
                ctx,
                boundary,
                db_id=db_id,
                plan=plan,
                attempt=attempt,
            )
        else:
            realization = realize_plan_static(
                ctx,
                boundary,
                db_id=db_id,
                plan=plan,
                attempt=attempt,
            )
        if realization["ok"]:
            mql = realization["mql"]
            ctx.log.info("smart_solver_done", attempts=attempt + 1, mql_preview=mql[:300])
            return SolverPrediction(
                record_id=record_id,
                db_id=db_id,
                MQL=mql,
                disclosure=disclosure,
                shape_model=shape_model,
                logical_spec=spec.to_json(),
                physical_plan=plan.to_json(),
                feedback=feedback_log,
            )
        feedback_entry = dict(realization["feedback"] or {})
        feedback_entry.setdefault("attempt", attempt)
        feedback = feedback_entry
        feedback_log.append(feedback_entry)
        ctx.log.warning("smart_solver_feedback", attempt=attempt, feedback=feedback_entry)
        if feedback_entry.get("boundary_failure"):
            return SolverFailure(
                record_id=record_id,
                db_id=db_id,
                error_code=str(feedback_entry.get("error_code") or "EXEC_ERROR"),
                message=str(feedback_entry.get("message") or "solver execution boundary failed"),
                disclosure=disclosure,
                shape_model=shape_model,
                logical_spec=spec.to_json(),
                physical_plan=plan.to_json(),
                feedback=feedback_log,
                terminal_feedback=feedback_entry,
            )

    terminal_feedback = feedback_log[-1] if feedback_log else None
    ctx.log.warning("smart_solver_abandon", attempts=effective_r_max + 1, feedback=feedback_log)
    ctx.log.anomaly(
        kind=Anomaly.SOLVER_EXHAUSTED,
        message="solver exhausted all realization attempts",
        attempts=effective_r_max + 1,
        feedback=feedback_log,
        terminal_feedback=terminal_feedback,
    )
    return SolverFailure(
        record_id=record_id,
        db_id=db_id,
        error_code="SOLVER_EXHAUSTED",
        message="solver exhausted all realization attempts",
        disclosure=disclosure,
        shape_model=shape_model,
        logical_spec=logical_spec,
        physical_plan=physical_plan,
        feedback=feedback_log,
        terminal_feedback=terminal_feedback,
    )


async def comprehend_shapes(
    wf: Workflow,
    ctx: Any,
    nlq: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Stage 1: fan out one schema-only probe per collection, then reduce."""
    wf.phase("SOLVE-1")
    collections = _schema_collections(schema)
    work = [
        (
            ctx,
            {"nlq": nlq, "collection": name, "schema": coll_schema},
        )
        for name, coll_schema in sorted(collections.items())
    ]
    fragments = await wf.map_agent("smart_shape_probe", work, isolate=False)
    reduced = await wf.agent("smart_shape_reduce", {"fragments": fragments}, ctx=ctx)
    assert reduced is not None
    return reduced


def _schema_collections(schema: dict[str, Any]) -> dict[str, Any]:
    collections = schema.get("collections")
    if isinstance(collections, dict):
        base = collections
    else:
        base = {
            key: value for key, value in schema.items()
            if (
                isinstance(value, dict)
                and key not in {"db_id", "metadata", "structure_audit", "structure_gate"}
            )
        }
    audit = schema.get("structure_audit")
    if not isinstance(audit, dict):
        return base
    return {
        name: _merge_structure_audit(coll_schema, collection=name, audit=audit)
        for name, coll_schema in base.items()
    }


def _merge_structure_audit(
    coll_schema: Any,
    *,
    collection: str,
    audit: dict[str, Any],
) -> dict[str, Any]:
    out = dict(coll_schema) if isinstance(coll_schema, dict) else {}
    collection_counts = audit.get("collection_counts")
    if isinstance(collection_counts, dict) and "document_count" not in out and "doc_count" not in out:
        if collection in collection_counts:
            out["document_count"] = collection_counts[collection]

    # Global dynamic-key sample map (path -> sample keys); used by both branches below.
    global_samples: dict[str, list[str]] = {}
    for item in audit.get("dynamic_key_paths") or []:
        if isinstance(item, dict):
            path = str(item.get("path") or "")
            samples = item.get("sample_keys")
            if path and isinstance(samples, list):
                global_samples[path] = [str(sample) for sample in samples]

    per_collection = audit.get("per_collection_paths")
    if isinstance(per_collection, dict) and per_collection:
        # New artifacts: merge ONLY this collection's paths, so one collection is never
        # polluted with another collection's structure (review fix H4 re-architecture).
        per = per_collection.get(collection, {})
        dynamic_paths = [str(path) for path in per.get("dynamic_key_paths") or []]
        array_paths_src = per.get("nested_array_paths")
        dynamic_array_object_src = per.get("dynamic_array_object_paths")
        array_object_dynamic_src = per.get("array_object_dynamic_paths")
        presence = _presence_state_counts_from_per(per.get("presence_state_counts"))
    else:
        # Backward-compat: pre-per_collection artifacts fall back to the global projection.
        # Single-collection native designs are exact; legacy multi-collection ones stay
        # imperfect until release artifacts are rebuilt with per_collection_paths.
        # NOTE: changes measured behavior; affected ablation/leaderboard numbers need re-run (review fix H4 re-architecture)
        dynamic_paths = [
            str(item.get("path") or "")
            for item in audit.get("dynamic_key_paths") or []
            if isinstance(item, dict) and item.get("path")
        ]
        array_paths_src = audit.get("nested_array_paths")
        dynamic_array_object_src = audit.get("dynamic_array_object_paths")
        array_object_dynamic_src = audit.get("array_object_dynamic_paths")
        raw_presence = audit.get("presence_state_counts")
        presence = dict(raw_presence) if isinstance(raw_presence, dict) else {}

    dynamic_samples = {
        path: global_samples[path] for path in dynamic_paths if path in global_samples
    }

    out["dynamic_key_paths"] = _merge_str_lists(out.get("dynamic_key_paths"), dynamic_paths)
    out["dynamic_key_samples"] = {
        **{
            str(path): [str(sample) for sample in samples or []]
            for path, samples in dict(out.get("dynamic_key_samples") or {}).items()
        },
        **dynamic_samples,
    }
    out["array_paths"] = _merge_str_lists(out.get("array_paths"), array_paths_src)
    out["dynamic_array_object_paths"] = _merge_str_lists(
        out.get("dynamic_array_object_paths"),
        dynamic_array_object_src,
    )
    out["array_object_dynamic_paths"] = _merge_str_lists(
        out.get("array_object_dynamic_paths"),
        array_object_dynamic_src,
    )
    if not out.get("presence_state_counts") and presence:
        out["presence_state_counts"] = presence
    return out


def _merge_str_lists(left: Any, right: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*(left or []), *(right or [])]:
        text = str(value)
        if text and text not in seen:
            merged.append(text)
            seen.add(text)
    return merged


def _presence_state_counts_from_per(raw: Any) -> dict[str, int]:
    """Parse per-collection presence-state ``state:count`` entries into a count map."""
    counts: dict[str, int] = {}
    for entry in raw or []:
        text = str(entry)
        name, _sep, count = text.rpartition(":")
        if not name:
            continue
        try:
            counts[name] = int(count)
        except ValueError:
            continue
    return counts


def realize_plan_per_stage(
    ctx: Any,
    boundary: SolverBoundary,
    *,
    db_id: str,
    plan: PhysicalPlan,
    target_fields: list[str],
    shape_policy: str = "reshape",
    schema: dict[str, Any] | None = None,
    shape_model: dict[str, Any] | None = None,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    attempt: int | None = None,
    variant_stratification: bool = True,
    allow_local_witness_strata: bool = True,
) -> dict[str, Any]:
    """Stage 4: AST-filter and execute each growing prefix when a local executor exists."""
    from .per_stage import CheckpointSpec, run_per_stage_check

    boundary.assert_stage_can_use_tool("query_realization", "mongo_executor")
    stages = [stage.stage for stage in plan.stages]
    mql = render_mql(plan.collection, stages)
    checkpoint = CheckpointSpec(
        target_fields=tuple(target_fields),
        required_fields_by_stage=_required_fields_by_stage(
            stages,
            target_fields,
            shape_policy=shape_policy,
        ),
        collapse_to_zero=shape_policy == "preserve",
    )
    if ctx.settings.stub:
        executor = _NoopPrefixExecutor(checkpoint.required_fields_by_stage)
    elif ctx.mongo is not None and ctx.mongo.available():
        executor = _MongoPrefixExecutor(
            ctx.mongo,
            schema=schema,
            shape_model=shape_model,
            local_data=local_data,
            variant_stratification=variant_stratification,
            allow_local_witness_strata=allow_local_witness_strata,
        )
    else:
        ctx.log.anomaly(
            kind="exec_error",
            message="local MongoDB unavailable for per-stage solver execution",
            db_id=db_id,
            collection=plan.collection,
            attempt=attempt,
        )
        return {
            "ok": False,
            "mql": None,
            "feedback": {
                "error_code": "EXEC_ERROR",
                "stage_index": 0,
                "failing_variant": None,
                "suspect_field": None,
                "message": "local MongoDB unavailable for per-stage solver execution",
                "boundary_failure": True,
                "attempt": attempt,
            },
        }
    logger = ctx.log.bind(solver_attempt=attempt) if attempt is not None else ctx.log
    result = run_per_stage_check(
        db_id=db_id,
        mql=mql,
        executor=executor,
        checkpoint=checkpoint,
        logger=logger,
    )
    if result.ok:
        mql = result.final_mql or mql
        try:
            boundary.assert_no_disabled(mql)
        except TendError as err:
            return _realization_boundary_failure(err, attempt=attempt)
        return {"ok": True, "mql": mql, "feedback": None}
    feedback = result.feedback.to_log_context() if result.feedback else None
    return {"ok": False, "mql": None, "feedback": feedback}


def realize_plan_whole_query(
    ctx: Any,
    boundary: SolverBoundary,
    *,
    db_id: str,
    plan: PhysicalPlan,
    attempt: int | None = None,
) -> dict[str, Any]:
    """Ablation realization: execute only the full query, without prefix checkpoints."""
    boundary.assert_stage_can_use_tool("query_realization", "mongo_executor")
    mql = render_mql(plan.collection, [stage.stage for stage in plan.stages])
    try:
        boundary.assert_no_disabled(mql)
    except TendError as err:
        return _realization_boundary_failure(err, attempt=attempt)
    if ctx.settings.stub:
        ctx.log.info(
            "smart_solver_whole_query_stub",
            attempt=attempt,
            collection=plan.collection,
            stages=len(plan.stages),
        )
        return {"ok": True, "mql": mql, "feedback": None}
    if ctx.mongo is None or not ctx.mongo.available():
        ctx.log.anomaly(
            kind=Anomaly.EXEC_ERROR,
            message="local MongoDB unavailable for whole-query solver execution",
            db_id=db_id,
            collection=plan.collection,
            attempt=attempt,
        )
        return {
            "ok": False,
            "mql": None,
            "feedback": {
                "error_code": "EXEC_ERROR",
                "stage_index": len(plan.stages),
                "failing_variant": None,
                "suspect_field": None,
                "message": "local MongoDB unavailable for whole-query solver execution",
                "boundary_failure": True,
                "attempt": attempt,
            },
        }
    try:
        docs = ctx.mongo.norm_exec(db_id, mql)
    except DisabledOperatorError as err:
        # Disabled-operator / boundary violations remain terminal, matching the other modes.
        return _realization_boundary_failure(err, attempt=attempt)
    except Exception as exc:  # noqa: BLE001 - executor feedback is an ablation signal
        # NOTE: changes measured behavior; affected ablation/leaderboard numbers need re-run (review fix H1)
        # A transient Mongo execution error (e.g. ExecutionError from norm_exec) is retryable
        # feedback (boundary_failure=False) so the main loop retries up to r_max, symmetric with
        # the per_stage realization mode.
        return {
            "ok": False,
            "mql": None,
            "feedback": {
                "error_code": "EXEC_ERROR",
                "stage_index": len(plan.stages),
                "failing_variant": None,
                "suspect_field": None,
                "message": str(exc)[:500],
                "boundary_failure": False,
                "attempt": attempt,
            },
        }
    ctx.log.info(
        "smart_solver_whole_query_done",
        attempt=attempt,
        collection=plan.collection,
        stages=len(plan.stages),
        output_rows=len(docs),
    )
    return {"ok": True, "mql": mql, "feedback": None}


def realize_plan_static(
    ctx: Any,
    boundary: SolverBoundary,
    *,
    db_id: str,
    plan: PhysicalPlan,
    attempt: int | None = None,
) -> dict[str, Any]:
    """Ablation realization: render MQL and apply only static disabled-operator guards."""
    mql = render_mql(plan.collection, [stage.stage for stage in plan.stages])
    try:
        boundary.assert_no_disabled(mql)
    except TendError as err:
        return _realization_boundary_failure(err, attempt=attempt)
    ctx.log.info(
        "smart_solver_static_realization",
        db_id=db_id,
        attempt=attempt,
        collection=plan.collection,
        stages=len(plan.stages),
    )
    return {"ok": True, "mql": mql, "feedback": None}


def _realization_boundary_failure(err: TendError, *, attempt: int | None) -> dict[str, Any]:
    return {
        "ok": False,
        "mql": None,
        "feedback": {
            "error_code": err.anomaly.value if err.anomaly else "BOUNDARY_ERROR",
            "stage_index": err.context.get("stage_index"),
            "failing_variant": None,
            "suspect_field": None,
            "message": err.message,
            "boundary_failure": True,
            "attempt": attempt,
        },
    }


def load_solver_release_inputs(
    dataset_dir: Path,
    *,
    db_id: str | None = None,
    record_id: int | None = None,
    limit: int | None = None,
    nlq_track: NlqTrack = "record",
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]]:
    """Load release records plus public schema/data assets.

    Gold fields remain on the raw record here; ``smart_solve_record`` sanitizes them before
    any solver stage sees the input. Keeping this loader dumb makes the sanitizer testable.
    """
    out: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    for record in select_solver_release_records(
        dataset_dir,
        db_id=db_id,
        record_id=record_id,
        limit=limit,
        nlq_track=nlq_track,
    ):
        rid = record["db_id"]
        schema = json.loads((dataset_dir / "mongodb_schema" / f"{rid}.json").read_text(
            encoding="utf-8"
        ))
        data_path = dataset_dir / "mongodb_data" / f"{rid}.json"
        data = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else None
        out.append((record, schema, data))
    return out


def select_solver_release_records(
    dataset_dir: Path,
    *,
    db_id: str | None = None,
    record_id: int | None = None,
    limit: int | None = None,
    nlq_track: NlqTrack = "record",
) -> list[dict[str, Any]]:
    """Select release records without loading schema/data assets."""

    records = json.loads((dataset_dir / "test.json").read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for record in records:
        if db_id and record.get("db_id") != db_id:
            continue
        if record_id is not None and record.get("record_id") != record_id:
            continue
        out.append(_record_for_nlq_track(record, nlq_track))
        if limit is not None and len(out) >= limit:
            break
    return out


def _record_for_nlq_track(record: dict[str, Any], nlq_track: NlqTrack) -> dict[str, Any]:
    if nlq_track == "record":
        return record
    nl_queries = record.get("nl_queries")
    selected = nl_queries.get(nlq_track) if isinstance(nl_queries, dict) else None
    if not isinstance(selected, str) or not selected.strip():
        raise PromptAnomalyError(
            f"record missing {nlq_track} NLQ track",
            context={"record_id": record.get("record_id"), "db_id": record.get("db_id")},
        )
    out = dict(record)
    out["nl_queries"] = {"canonical": selected}
    out["nlq_track"] = nlq_track
    return out


def build_witness_digest(
    data: dict[str, list[dict[str, Any]]] | None,
    witness_k: int,
) -> dict[str, Any]:
    """Build the small prompt-visible witness digest allowed in SMART stages 3/4."""
    if not data:
        return {}
    k = max(0, witness_k)
    digest: dict[str, Any] = {}
    for collection, docs in sorted(data.items()):
        if not isinstance(docs, list):
            continue
        sample = [_compact_witness_value(doc) for doc in docs[:k]]
        digest[collection] = {
            "sample_count": len(sample),
            "sample_documents": sample,
            "string_values_in_sample": _string_values_in_sample(sample),
        }
    return digest


def _compact_witness_value(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) <= _WITNESS_MAX_STRING_CHARS:
            return value
        return value[: _WITNESS_MAX_STRING_CHARS - 3] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _WITNESS_MAX_DEPTH:
        if isinstance(value, dict):
            return {
                "__truncated_depth__": True,
                "__keys__": sorted(str(key) for key in value)[:_WITNESS_MAX_DICT_ITEMS],
            }
        if isinstance(value, list):
            return {
                "__truncated_depth__": True,
                "__item_count__": len(value),
            }
        return str(value)[:_WITNESS_MAX_STRING_CHARS]
    if isinstance(value, list):
        preview = [
            _compact_witness_value(item, depth=depth + 1)
            for item in value[:_WITNESS_MAX_LIST_ITEMS]
        ]
        if len(value) > _WITNESS_MAX_LIST_ITEMS:
            preview.append({"__truncated_items__": len(value) - _WITNESS_MAX_LIST_ITEMS})
        return preview
    if isinstance(value, dict):
        items = list(value.items())
        preview = {
            str(key): _compact_witness_value(child, depth=depth + 1)
            for key, child in items[:_WITNESS_MAX_DICT_ITEMS]
        }
        if len(items) > _WITNESS_MAX_DICT_ITEMS:
            preview["__truncated_keys__"] = len(items) - _WITNESS_MAX_DICT_ITEMS
            preview["__keys__"] = sorted(str(key) for key in value)[:_WITNESS_MAX_DICT_ITEMS]
        return preview
    return str(value)[:_WITNESS_MAX_STRING_CHARS]


def _string_values_in_sample(docs: list[dict[str, Any]]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {}
    for doc in docs:
        for key, value in doc.items():
            if isinstance(value, str):
                values.setdefault(key, set()).add(value)
            elif isinstance(value, dict):
                for subkey, subvalue in value.items():
                    if isinstance(subvalue, str):
                        values.setdefault(f"{key}.{subkey}", set()).add(subvalue)
    # NOTE: changes measured behavior; affected ablation/leaderboard numbers need re-run (review fix F9)
    return {key: sorted(vals)[:24] for key, vals in sorted(values.items())}


def collapsed_shape_model(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a flat, variant-free shape model for shape-ablation runs."""
    collections: dict[str, Any] = {}
    for name, raw in sorted(_schema_collections(schema).items()):
        fields = raw.get("fields") if isinstance(raw, Mapping) else None
        if isinstance(fields, Mapping):
            field_names = sorted(str(field) for field in fields)
        elif isinstance(fields, list):
            field_names = sorted(str(field) for field in fields)
        elif isinstance(raw, Mapping):
            field_names = sorted(
                str(key)
                for key in raw
                if not str(key).startswith("__") and key not in {"doc_count", "schema_flex"}
            )
        else:
            field_names = []
        collections[str(name)] = {
            "variants": [{"id": "*", "discriminator": {}, "coverage": 1.0, "fields": {}}],
            "field_locus": {
                field: [{"variant": "*", "path": field, "type": "unknown", "presence": "always"}]
                for field in field_names
            },
            "doc_count": raw.get("doc_count") if isinstance(raw, Mapping) else None,
        }
    return {
        "collections": collections,
        "coverage_gaps": ["ablation:shape_comprehension_disabled"],
        "shape_flex_signature": [],
    }


def _canonical_nlq(record: dict[str, Any], *, use_colloquial: bool = True) -> str:
    nl_queries = record.get("nl_queries")
    if isinstance(nl_queries, dict):
        candidates = [nl_queries.get("canonical")]
        if use_colloquial:
            candidates.append(nl_queries.get("colloquial"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    for key in ("NLQ", "query"):
        candidate = record.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    raise PromptAnomalyError(
        "solver record missing natural language question",
        context={"record_id": record.get("record_id"), "db_id": record.get("db_id")},
    )


def _strip_schema_variants(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_schema_variants(child)
            for key, child in value.items()
            if key not in {"__variants", "schema_flex"}
        }
    if isinstance(value, list):
        return [_strip_schema_variants(item) for item in value]
    return value


def _effective_r_max(options: SmartSolveOptions, fallback: int) -> int:
    return max(0, options.r_max if options.r_max is not None else fallback)


def _effective_witness_k(options: SmartSolveOptions, fallback: int) -> int:
    return max(0, options.witness_k if options.witness_k is not None else fallback)


def _solver_native_task_context(record: dict[str, Any]) -> dict[str, Any]:
    """Extract solver-visible native schema-flex hints without gold or template leakage."""

    out: dict[str, Any] = {}
    # NOTE: anti_sql_transfer_level / anti_sql_transfer_evidence are intentionally omitted:
    # they are operator-hint leakage not on the solver allow_list (review fix F6).
    for key in (
        "schema_flex",
        "schema_feature",
        "native_feature_id",
        "native_feature_type",
        "native_query_pattern",
        "mongo_native_constructs",
    ):
        value = record.get(key)
        if value not in (None, "", [], {}):
            out[key] = value

    metadata = record.get("native_metadata")
    if isinstance(metadata, Mapping):
        for source, target in (
            ("feature_id", "feature_id"),
            ("feature_type", "feature_type"),
            ("feature_field", "feature_field"),
            ("query_pattern", "query_pattern"),
            ("target_shape_policy", "target_shape_policy"),
            ("required_native_constructs", "required_native_constructs"),
            ("mongo_native_constructs", "metadata_mongo_native_constructs"),
            ("anti_sql_transfer", "anti_sql_transfer"),
            ("anti_sql_transfer_target", "anti_sql_transfer_target"),
            ("rationale", "native_rationale"),
        ):
            value = metadata.get(source)
            if value not in (None, "", [], {}):
                out[target] = value
    return out


def _focus_native_collection_shape_model(
    shape_model: dict[str, Any],
    native_task_context: dict[str, Any],
) -> dict[str, Any]:
    collection = _native_context_collection(native_task_context)
    if not collection:
        return shape_model
    collections = shape_model.get("collections")
    if not isinstance(collections, Mapping) or collection not in collections:
        return shape_model
    focused = dict(shape_model)
    focused["collections"] = {
        collection: _prune_shape_collection_for_native_context(
            collections[collection],
            native_task_context,
        )
    }
    return focused


def _solver_agent_native_task_context(
    native_task_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize public native hints before they reach LLM agents.

    Some release records carry a construction feature id/field that is useful provenance but
    not the actual query root. For known public ``native_query_pattern`` values, prefer the
    pattern's root collection and relevant paths so the prompt does not direct the planner
    toward an unrelated dynamic map.
    """

    out = dict(native_task_context)
    collection = _native_context_collection(native_task_context)
    if collection:
        out["root_collection"] = collection
    hints = _native_query_path_hints(native_task_context)
    if hints:
        out["relevant_paths"] = list(hints)
    primary = _primary_native_query_path(native_task_context)
    if primary:
        current = out.get("feature_field")
        if isinstance(current, str) and current.strip() and current.strip() != primary:
            out.setdefault("source_feature_field", current.strip())
        out["feature_field"] = primary
    return out


def _focus_native_collection_witness_digest(
    witness_digest: dict[str, Any],
    native_task_context: dict[str, Any],
) -> dict[str, Any]:
    collection = _native_context_collection(native_task_context)
    if not collection or collection not in witness_digest:
        return witness_digest
    return {
        collection: _prune_witness_collection_for_native_context(
            witness_digest[collection],
            native_task_context,
        )
    }


def _prune_witness_collection_for_native_context(
    collection_digest: Any,
    native_task_context: Mapping[str, Any],
) -> Any:
    if not isinstance(collection_digest, Mapping):
        return collection_digest
    hints = _native_query_path_hints(native_task_context)
    if not hints:
        return collection_digest
    out = dict(collection_digest)
    samples = collection_digest.get("sample_documents")
    if not isinstance(samples, list):
        return out
    pruned_samples = [
        _prune_witness_document_for_paths(sample, hints) if isinstance(sample, Mapping) else sample
        for sample in samples
    ]
    out["sample_documents"] = pruned_samples
    out["sample_count"] = len(pruned_samples)
    out["string_values_in_sample"] = _string_values_in_sample(
        [dict(sample) for sample in pruned_samples if isinstance(sample, Mapping)]
    )
    return out


_MISSING = object()


def _prune_witness_document_for_paths(
    sample: Mapping[str, Any],
    hints: tuple[str, ...],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "_id" in sample:
        out["_id"] = sample["_id"]
    for hint in hints:
        value = _get_path(sample, hint)
        if value is _MISSING:
            continue
        _set_path(out, hint, value)
    return out


def _get_path(value: Mapping[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _set_path(out: dict[str, Any], path: str, value: Any) -> None:
    current = out
    parts = path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _native_context_collection(native_task_context: Mapping[str, Any]) -> str | None:
    pattern = _native_query_pattern(native_task_context)
    if pattern and pattern in _NATIVE_QUERY_PATTERN_COLLECTION_HINTS:
        return _NATIVE_QUERY_PATTERN_COLLECTION_HINTS[pattern]
    for key in ("root_collection", "collection"):
        value = native_task_context.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("feature_id", "schema_feature", "native_feature_id"):
        value = native_task_context.get(key)
        if isinstance(value, str) and "." in value:
            collection, _sep, _rest = value.partition(".")
            if collection:
                return collection
    return None


def _prune_shape_collection_for_native_context(
    collection_shape: Any,
    native_task_context: Mapping[str, Any],
) -> Any:
    if not isinstance(collection_shape, Mapping):
        return collection_shape
    hints = _native_query_path_hints(native_task_context)
    if not hints:
        return collection_shape
    out = dict(collection_shape)
    # Hints are non-empty here; for keys that ARE present, reassign the filtered list
    # (so a no-match prunes to empty rather than leaking the full unpruned list) — but do
    # NOT introduce keys the collection did not already carry (review fix F2/ADDED).
    for key in ("dynamic_key_paths", "array_paths", "dynamic_array_object_paths", "array_object_dynamic_paths"):
        if key in out:
            out[key] = [str(path) for path in out.get(key) or [] if _path_matches_any_hint(str(path), hints)]
    samples = out.get("dynamic_key_samples")
    if isinstance(samples, Mapping):
        out["dynamic_key_samples"] = {
            str(path): sample
            for path, sample in samples.items()
            if _path_matches_any_hint(str(path), hints)
        }
    loci = out.get("field_locus")
    if isinstance(loci, Mapping):
        out["field_locus"] = {
            str(path): entries
            for path, entries in loci.items()
            if _path_matches_any_hint(str(path), hints)
        }
    return out


def _native_query_path_hints(native_task_context: Mapping[str, Any]) -> tuple[str, ...]:
    pattern = _native_query_pattern(native_task_context)
    if pattern and pattern in _NATIVE_QUERY_PATTERN_PATH_HINTS:
        return _NATIVE_QUERY_PATTERN_PATH_HINTS[pattern]
    feature_field = native_task_context.get("feature_field")
    if isinstance(feature_field, str) and feature_field.strip():
        return (feature_field.strip(),)
    return ()


def _primary_native_query_path(native_task_context: Mapping[str, Any]) -> str | None:
    hints = _native_query_path_hints(native_task_context)
    if hints:
        return hints[0]
    feature_field = native_task_context.get("feature_field")
    if isinstance(feature_field, str) and feature_field.strip():
        return feature_field.strip()
    return None


def _native_query_pattern(native_task_context: Mapping[str, Any]) -> str | None:
    for key in ("query_pattern", "native_query_pattern"):
        value = native_task_context.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _path_matches_any_hint(path: str, hints: tuple[str, ...]) -> bool:
    return any(_path_matches_hint(path, hint) for hint in hints)


def _path_matches_hint(path: str, hint: str) -> bool:
    return (
        path == hint
        or path.startswith(f"{hint}.")
        or path.startswith(f"{hint}[")
        or hint.startswith(f"{path}.")
        or hint.startswith(f"{path}[")
    )


def _load_local_data_if_available(
    ctx: Any,
    db_id: str,
    data: dict[str, list[dict[str, Any]]] | None,
    *,
    witness_preloaded: bool = False,
) -> None:
    if not data or ctx.mongo is None or ctx.settings.stub:
        return
    if witness_preloaded:
        ctx.log.info("smart_solver_witness_reuse", db_id=db_id)
        return
    if not ctx.mongo.available():
        ctx.log.warning("smart_solver_mongo_unavailable", db_id=db_id)
        return
    ctx.mongo.load_witness(db_id, data)


def _required_fields_by_stage(
    stages: list[dict[str, Any]],
    target_fields: list[str],
    *,
    shape_policy: str = "preserve",
) -> dict[int, tuple[str, ...]]:
    target_fields = [field for field in target_fields if field != "*"]
    if not target_fields:
        return {}
    if shape_policy != "preserve":
        return {
            idx: tuple(target_fields) if idx == len(stages) else ()
            for idx in range(1, len(stages) + 1)
        }
    first_by_field: dict[str, int] = {}
    pending = set(target_fields)
    for idx, stage in enumerate(stages, start=1):
        body = stage.get("$addFields") or stage.get("$set") or stage.get("$project")
        if not isinstance(body, dict):
            continue
        for field in sorted(pending.intersection(body)):
            first_by_field[field] = idx
        pending.difference_update(first_by_field)
        if not pending:
            break
    final_stage = len(stages)
    return {
        idx: tuple(
            field
            for field in target_fields
            if idx >= first_by_field.get(field, final_stage)
        )
        for idx in range(1, final_stage + 1)
    }


_PRESENT_MARKERS = {"present", "exists", "true"}
_MISSING_MARKERS = {"missing", "absent", "false"}


@dataclass(frozen=True)
class _VariantStratum:
    variant: str
    discriminator: dict[str, Any]
    selector: dict[str, Any]
    source: str

    def to_log_context(self) -> dict[str, Any]:
        return {
            "discriminator": self.discriminator,
            "selector": self.selector,
            "source": self.source,
        }


class _MongoPrefixExecutor:
    def __init__(
        self,
        mongo: Any,
        *,
        schema: dict[str, Any] | None = None,
        shape_model: dict[str, Any] | None = None,
        local_data: dict[str, list[dict[str, Any]]] | None = None,
        variant_stratification: bool = True,
        allow_local_witness_strata: bool = True,
    ) -> None:
        self._mongo = mongo
        self._schema = schema or {}
        self._shape_model = shape_model or {}
        self._local_data = local_data or {}
        self._variant_stratification = variant_stratification
        self._allow_local_witness_strata = allow_local_witness_strata

    def execute_prefix(self, request: Any) -> Any:
        from .per_stage import PrefixExecutionResult, VariantExecution

        strata = (
            _variant_strata(
                request.collection,
                schema=self._schema,
                shape_model=self._shape_model,
                local_data=self._local_data,
                allow_local_witness=self._allow_local_witness_strata,
            )
            if self._variant_stratification
            else ()
        )
        if not strata:
            docs = self._mongo.norm_exec(request.db_id, _bounded_prefix_mql(request))
            input_count = self._count_variant(request, None)
            return PrefixExecutionResult.single_variant(
                docs,
                variant="unstratified",
                input_count=input_count,
            )

        def execute_stratum(stratum: _VariantStratum) -> VariantExecution:
            input_count = self._count_variant(request, stratum)
            try:
                docs = self._mongo.norm_exec(request.db_id, _stratified_mql(request, stratum))
                return VariantExecution(
                    stratum.variant,
                    tuple(docs),
                    input_count,
                    context=stratum.to_log_context(),
                )
            except Exception as exc:  # noqa: BLE001 - report variant-scoped executor feedback
                return VariantExecution(
                    stratum.variant,
                    (),
                    input_count,
                    str(exc)[:500],
                    {
                        **stratum.to_log_context(),
                        "exception_type": type(exc).__name__,
                    },
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(strata), 8)) as pool:
            variants = list(pool.map(execute_stratum, strata))
        return PrefixExecutionResult(tuple(variants))

    def _count_variant(self, request: Any, stratum: _VariantStratum | None) -> int | None:
        local_count = _local_variant_count(
            self._local_data,
            request.collection,
            stratum.discriminator if stratum else {},
        )
        if local_count is not None:
            return local_count
        try:
            if stratum is None:
                # NOTE: changes measured behavior; affected ablation/leaderboard numbers need re-run (review fix DOC_COUNT_COLLAPSE)
                # The executed prefix is $limit(_PER_STAGE_PREFIX_INPUT_LIMIT)-bounded, so the
                # comparable input size is capped; returning the full-collection count caused
                # false DOC_COUNT_COLLAPSE feedback.
                count = int(self._mongo.count(request.db_id, request.collection))
                return min(count, _PER_STAGE_PREFIX_INPUT_LIMIT)
            connect = getattr(self._mongo, "_connect", None)
            db_name = getattr(self._mongo, "_db_name", None)
            if callable(connect) and callable(db_name):
                client = connect()
                # NOTE: changes measured behavior; affected ablation/leaderboard numbers need re-run (review fix DOC_COUNT_COLLAPSE)
                count = int(
                    client[db_name(request.db_id)][request.collection].count_documents(
                        stratum.selector
                    )
                )
                return min(count, _PER_STAGE_PREFIX_INPUT_LIMIT)
        except Exception:  # noqa: BLE001 - counts are diagnostic, not execution truth
            return None
        return None


def _variant_strata(
    collection: str,
    *,
    schema: dict[str, Any],
    shape_model: dict[str, Any],
    local_data: dict[str, list[dict[str, Any]]],
    allow_local_witness: bool = True,
) -> tuple[_VariantStratum, ...]:
    raw_variants = _shape_model_variants(shape_model, collection)
    source = "shape_model"
    if not raw_variants:
        raw_variants = _schema_variants(schema, collection)
        source = "schema"
    strata = _normalize_variant_strata(raw_variants, source=source)
    if strata:
        return strata
    if allow_local_witness:
        return _local_witness_strata(local_data, collection)
    return ()


def _shape_model_variants(shape_model: dict[str, Any], collection: str) -> list[Mapping[str, Any]]:
    collections = shape_model.get("collections")
    if not isinstance(collections, Mapping):
        return []
    node = collections.get(collection)
    if not isinstance(node, Mapping):
        return []
    variants = node.get("variants") or []
    return [v for v in variants if isinstance(v, Mapping)]


def _schema_variants(schema: dict[str, Any], collection: str) -> list[Mapping[str, Any]]:
    node = _schema_collections(schema).get(collection)
    if not isinstance(node, Mapping):
        return []
    variants = node.get("__variants") or []
    return [v for v in variants if isinstance(v, Mapping)]


def _normalize_variant_strata(
    raw_variants: list[Mapping[str, Any]],
    *,
    source: str,
) -> tuple[_VariantStratum, ...]:
    explicit_missing_fields = {
        str(field)
        for raw in raw_variants
        for discriminator in [dict(raw.get("discriminator") or {})]
        if len(discriminator) == 1
        for field, marker in discriminator.items()
        if _marker(marker) in _MISSING_MARKERS
    }
    strata: list[_VariantStratum] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for index, raw in enumerate(raw_variants):
        discriminator = {str(k): v for k, v in dict(raw.get("discriminator") or {}).items()}
        if not discriminator:
            continue
        variant = str(raw.get("id") or f"v{index}")
        _append_stratum(strata, seen, variant, discriminator, source)
        if len(discriminator) == 1:
            field, marker = next(iter(discriminator.items()))
            if _marker(marker) in _PRESENT_MARKERS and field not in explicit_missing_fields:
                _append_stratum(
                    strata,
                    seen,
                    f"{variant}_missing",
                    {field: "missing"},
                    source,
                )
    return tuple(strata)


def _local_witness_strata(
    local_data: dict[str, list[dict[str, Any]]],
    collection: str,
) -> tuple[_VariantStratum, ...]:
    docs = [doc for doc in local_data.get(collection, []) if isinstance(doc, Mapping)]
    if len(docs) < 2:
        return ()
    fields = sorted(
        field
        for field in {key for doc in docs for key in doc if isinstance(key, str)}
        if field != "_id"
    )
    variable_fields = [
        field
        for field in fields
        if 0 < sum(1 for doc in docs if _path_values(doc, (field,))) < len(docs)
    ][:8]
    if not variable_fields:
        return ()

    strata_by_key: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    for doc in docs:
        discriminator = {
            field: "present" if _path_values(doc, (field,)) else "missing"
            for field in variable_fields
        }
        key = tuple(sorted((field, str(marker)) for field, marker in discriminator.items()))
        strata_by_key.setdefault(key, discriminator)

    strata: list[_VariantStratum] = []
    for index, discriminator in enumerate(strata_by_key.values()):
        strata.append(
            _VariantStratum(
                variant=f"witness-shape-{index}",
                discriminator=discriminator,
                selector=_selector_for_discriminator(discriminator),
                source="local_witness",
            )
        )
    return tuple(strata)


def _append_stratum(
    strata: list[_VariantStratum],
    seen: set[tuple[tuple[str, str], ...]],
    variant: str,
    discriminator: dict[str, Any],
    source: str,
) -> None:
    key = tuple(sorted((field, str(marker)) for field, marker in discriminator.items()))
    if key in seen:
        return
    seen.add(key)
    strata.append(
        _VariantStratum(
            variant=variant,
            discriminator=discriminator,
            selector=_selector_for_discriminator(discriminator),
            source=source,
        )
    )


def _selector_for_discriminator(discriminator: Mapping[str, Any]) -> dict[str, Any]:
    selector: dict[str, Any] = {}
    for field, marker in discriminator.items():
        normalized = _marker(marker)
        if normalized in _PRESENT_MARKERS:
            selector[str(field)] = {"$exists": True}
        elif normalized in _MISSING_MARKERS:
            selector[str(field)] = {"$exists": False}
        else:
            selector[str(field)] = marker
    return selector


def _stratified_mql(request: Any, stratum: _VariantStratum) -> str:
    return render_mql(
        request.collection,
        [
            {"$match": stratum.selector},
            {"$limit": _PER_STAGE_PREFIX_INPUT_LIMIT},
            *request.pipeline,
            {"$limit": _PER_STAGE_PREFIX_OUTPUT_LIMIT},
        ],
    )


def _bounded_prefix_mql(request: Any) -> str:
    return render_mql(
        request.collection,
        [
            {"$limit": _PER_STAGE_PREFIX_INPUT_LIMIT},
            *request.pipeline,
            {"$limit": _PER_STAGE_PREFIX_OUTPUT_LIMIT},
        ],
    )


def _local_variant_count(
    local_data: dict[str, list[dict[str, Any]]],
    collection: str,
    discriminator: Mapping[str, Any],
) -> int | None:
    docs = local_data.get(collection)
    if docs is None:
        return None
    return sum(1 for doc in docs if _doc_matches_discriminator(doc, discriminator))


def _doc_matches_discriminator(doc: Mapping[str, Any], discriminator: Mapping[str, Any]) -> bool:
    for field, marker in discriminator.items():
        values = _path_values(doc, tuple(part for part in str(field).split(".") if part))
        normalized = _marker(marker)
        if normalized in _PRESENT_MARKERS:
            if not values:
                return False
        elif normalized in _MISSING_MARKERS:
            if values:
                return False
        elif marker not in values:
            return False
    return True


def _path_values(current: Any, parts: tuple[str, ...]) -> list[Any]:
    if not parts:
        return [current]
    part, remaining = parts[0], parts[1:]
    if isinstance(current, Mapping):
        if part not in current:
            return []
        return _path_values(current[part], remaining)
    if isinstance(current, list):
        values: list[Any] = []
        for item in current:
            values.extend(_path_values(item, parts))
        return values
    return []


def _marker(value: Any) -> str:
    return str(value).strip().lower()


class _NoopPrefixExecutor:
    def __init__(self, required_fields_by_stage: dict[int, tuple[str, ...]]) -> None:
        self._required_fields_by_stage = required_fields_by_stage

    def execute_prefix(self, request: Any) -> Any:
        from .per_stage import PrefixExecutionResult

        fields = self._required_fields_by_stage.get(request.stage_index, ())
        doc: dict[str, Any] = {"_id": 1}
        for field in fields:
            if "." in field:
                _set_path(doc, field, 1)
            else:
                doc[field] = 1
        return PrefixExecutionResult.single_variant([doc], variant="stub", input_count=1)
