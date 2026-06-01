"""SMART solver workflow from proposals/06_solution_design.md."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from ..errors import Anomaly, PromptAnomalyError, TendError
from ..workflow import Workflow
from . import agents as _agents  # noqa: F401 - import registers SMART agents
from .contracts import LogicalSpec, PhysicalPlan, SolverDisclosure, SolverPrediction
from .guards import SolverBoundary, render_mql

DEFAULT_R_MAX = 2
DEFAULT_WITNESS_K = 3
ExecutionMode = Literal["per_stage", "whole_query", "static"]


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
            {"nlq": nlq, "colloquial": colloquial, "shape_model": shape_model,
             "feedback": feedback},
            ctx=ctx,
        )
        assert logical_spec is not None
        spec = LogicalSpec.from_json(logical_spec)

        physical_plan = await wf.agent(
            "smart_plan",
            {
                "logical_spec": spec.to_json(),
                "shape_model": shape_model,
                "witness_digest": witness_digest,
                "feedback": feedback,
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
        return collections
    return {
        key: value for key, value in schema.items()
        if isinstance(value, dict) and key not in {"db_id", "metadata"}
    }


def realize_plan_per_stage(
    ctx: Any,
    boundary: SolverBoundary,
    *,
    db_id: str,
    plan: PhysicalPlan,
    target_fields: list[str],
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
        required_fields_by_stage=_required_fields_by_stage(stages, target_fields)
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
        boundary.assert_no_disabled(mql)
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
    except Exception as exc:  # noqa: BLE001 - executor feedback is an ablation signal
        return {
            "ok": False,
            "mql": None,
            "feedback": {
                "error_code": "EXEC_ERROR",
                "stage_index": len(plan.stages),
                "failing_variant": None,
                "suspect_field": None,
                "message": str(exc)[:500],
                "boundary_failure": True,
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
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]]:
    """Load release records plus public schema/data assets.

    Gold fields remain on the raw record here; ``smart_solve_record`` sanitizes them before
    any solver stage sees the input. Keeping this loader dumb makes the sanitizer testable.
    """
    records = json.loads((dataset_dir / "test.json").read_text(encoding="utf-8"))
    out: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    for record in records:
        if db_id and record.get("db_id") != db_id:
            continue
        if record_id is not None and record.get("record_id") != record_id:
            continue
        rid = record["db_id"]
        schema = json.loads((dataset_dir / "mongodb_schema" / f"{rid}.json").read_text(
            encoding="utf-8"
        ))
        data_path = dataset_dir / "mongodb_data" / f"{rid}.json"
        data = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else None
        out.append((record, schema, data))
        if limit is not None and len(out) >= limit:
            break
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
        sample = docs[:k]
        digest[collection] = {
            "sample_count": len(sample),
            "sample_documents": sample,
            "string_values_in_sample": _string_values_in_sample(sample),
        }
    return digest


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
    return {key: sorted(vals)[:8] for key, vals in sorted(values.items())}


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
    stages: list[dict[str, Any]], target_fields: list[str]
) -> dict[int, tuple[str, ...]]:
    if not target_fields:
        return {}
    first_materialized: int | None = None
    targets = set(target_fields)
    for idx, stage in enumerate(stages, start=1):
        body = stage.get("$addFields") or stage.get("$set") or stage.get("$project")
        if isinstance(body, dict) and targets.intersection(body):
            first_materialized = idx
            break
    if first_materialized is None:
        first_materialized = len(stages)
    return {
        idx: tuple(target_fields) if idx >= first_materialized else ()
        for idx in range(1, len(stages) + 1)
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
            docs = self._mongo.norm_exec(request.db_id, request.mql)
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

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(strata)) as pool:
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
                return int(self._mongo.count(request.db_id, request.collection))
            connect = getattr(self._mongo, "_connect", None)
            db_name = getattr(self._mongo, "_db_name", None)
            if callable(connect) and callable(db_name):
                client = connect()
                return int(
                    client[db_name(request.db_id)][request.collection].count_documents(
                        stratum.selector
                    )
                )
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
    return render_mql(request.collection, [{"$match": stratum.selector}, *request.pipeline])


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
        doc = {"_id": 1}
        doc.update({field: 1 for field in fields})
        return PrefixExecutionResult.single_variant([doc], variant="stub", input_count=1)
