"""SMART solver workflow from proposals/06_solution_design.md."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..workflow import Workflow
from . import agents as _agents  # noqa: F401 - import registers SMART agents
from .contracts import LogicalSpec, PhysicalPlan, SolverPrediction
from .guards import SolverBoundary, render_mql

DEFAULT_R_MAX = 2
DEFAULT_WITNESS_K = 3


async def smart_solve_record(
    wf: Workflow,
    record: dict[str, Any],
    schema: dict[str, Any],
    *,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    r_max: int = DEFAULT_R_MAX,
    witness_k: int = DEFAULT_WITNESS_K,
) -> SolverPrediction:
    """Solve one TEND record using the SMART four-stage reference workflow.

    The record may be a release ``test.json`` record. It is sanitized before any stage sees
    it so gold fields such as ``MQL`` and ``shape_policy`` cannot leak into prompts.
    """
    base_log = wf.ctx.log.bind(component="smart_solver")
    boundary = SolverBoundary.from_settings(wf.ctx.settings, logger=base_log)
    safe = boundary.sanitize_test_record(record)
    db_id = str(safe["db_id"])
    record_id = safe.get("record_id")
    nlq = _canonical_nlq(safe)
    colloquial = (safe.get("nl_queries") or {}).get("colloquial", "")
    disclosure = boundary.disclosure(wf.ctx.settings, r_max=r_max, witness_k=witness_k)
    base_log.info("smart_solver_start", db_id=db_id, record_id=record_id,
                  disclosure=disclosure.to_json())

    group = f"solve:{db_id}:{record_id}" if record_id is not None else f"solve:{db_id}"
    if wf.ctx.progress:
        wf.ctx.progress.add_group(group, f"solve {db_id}/{record_id}", phase="SOLVE", total=5)
    ctx = wf.context(db_id=db_id, record_id=record_id, group=group, phase="SOLVE")

    shape_model = await comprehend_shapes(wf, ctx, nlq, schema)
    witness_digest = build_witness_digest(local_data, witness_k)
    feedback: dict[str, Any] | None = None
    feedback_log: list[dict[str, Any]] = []
    logical_spec: dict[str, Any] = {}
    physical_plan: dict[str, Any] = {}

    _load_local_data_if_available(ctx, db_id, local_data)

    for attempt in range(r_max + 1):
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

        realization = realize_plan_per_stage(
            ctx,
            boundary,
            db_id=db_id,
            plan=plan,
            target_fields=spec.target_fields,
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
        feedback = realization["feedback"]
        feedback_log.append(dict(feedback or {}))
        ctx.log.warning("smart_solver_feedback", attempt=attempt, feedback=feedback)

    ctx.log.warning("smart_solver_abandon", attempts=r_max + 1, feedback=feedback_log)
    return SolverPrediction(
        record_id=record_id,
        db_id=db_id,
        MQL="[]",
        disclosure=disclosure,
        shape_model=shape_model,
        logical_spec=logical_spec,
        physical_plan=physical_plan,
        feedback=feedback_log,
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
        executor = _MongoPrefixExecutor(ctx.mongo)
    else:
        ctx.log.anomaly(
            kind="exec_error",
            message="local MongoDB unavailable for per-stage solver execution",
            db_id=db_id,
            collection=plan.collection,
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
            },
        }
    result = run_per_stage_check(
        db_id=db_id,
        mql=mql,
        executor=executor,
        checkpoint=checkpoint,
        logger=ctx.log,
    )
    if result.ok:
        mql = result.final_mql or mql
        boundary.assert_no_disabled(mql)
        return {"ok": True, "mql": mql, "feedback": None}
    feedback = result.feedback.to_log_context() if result.feedback else None
    return {"ok": False, "mql": None, "feedback": feedback}


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


def _canonical_nlq(record: dict[str, Any]) -> str:
    nl_queries = record.get("nl_queries")
    if isinstance(nl_queries, dict):
        return str(nl_queries.get("canonical") or nl_queries.get("colloquial") or "")
    return str(record.get("NLQ") or record.get("query") or "")


def _load_local_data_if_available(ctx: Any, db_id: str, data: dict[str, list[dict[str, Any]]] | None) -> None:
    if not data or ctx.mongo is None or ctx.settings.stub:
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


class _MongoPrefixExecutor:
    def __init__(self, mongo: Any) -> None:
        self._mongo = mongo

    def execute_prefix(self, request: Any) -> Any:
        docs = self._mongo.norm_exec(request.db_id, request.mql)
        try:
            input_count = self._mongo.count(request.db_id, request.collection)
        except Exception:  # noqa: BLE001 - count is diagnostic; prefix result still matters
            input_count = None
        from .per_stage import PrefixExecutionResult

        return PrefixExecutionResult.single_variant(docs, variant="local", input_count=input_count)


class _NoopPrefixExecutor:
    def __init__(self, required_fields_by_stage: dict[int, tuple[str, ...]]) -> None:
        self._required_fields_by_stage = required_fields_by_stage

    def execute_prefix(self, request: Any) -> Any:
        from .per_stage import PrefixExecutionResult

        fields = self._required_fields_by_stage.get(request.stage_index, ())
        doc = {"_id": 1}
        doc.update({field: 1 for field in fields})
        return PrefixExecutionResult.single_variant([doc], variant="stub", input_count=1)
