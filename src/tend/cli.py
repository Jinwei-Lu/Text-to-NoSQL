"""TEND command-line entry point.

    tend construct --phase all --dbs financial --records 1 [--stub] [--quiet]
    tend validate --dataset-dir runs/<run_id>/dataset [--smoke]
    tend publish --dataset-dir runs/<run_id>/dataset --out release/tend-native-mongodb-v1
    tend solve --db-id financial --record-id 1001 [--stub] [--quiet]

Assembles the runtime (logging + progress + BIRD source + LLM client + MongoDB executor),
runs the Phase A / Phase B workflow flows or the SMART solver, persists outputs, and
prints a run summary. The run id namespaces everything under ``runs/<run_id>/``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._cli_summaries import (
    _count_by,
    _print_ablation_summary,
    _print_baseline_summary,
    _print_evaluation_block,
    _print_solve_summary,
    _print_summary,
)
from .agents import AgentContext
from .ablations import ABLATION_IDS, run_ablation_suite
from .ablations.strategies import (
    DEFAULT_COST_BUDGET_USD,
    DEFAULT_MAX_REVISITS,
    DEFAULT_MAX_TOOL_TURNS,
)
from .baselines import BASELINE_IDS, run_baseline_suite
from .config import Settings
from .construction.artifacts import write_native_phase_a, write_records
from .construction.phase_a import NativeDbArtifacts, run_native_phase_a
from .construction.phase_b import plan_native_slots, run_native_phase_b
from .errors import Anomaly, SourceError, TendError, wrap_unexpected
from .evaluation import EvaluationOutput, evaluate_predictions
from .execution.mongo import MongoExecutor
from .llm import LLMClient
from .observability import make_reporter, setup_logging
from .publish import (
    ReleaseQualityReport,
    ReleaseReport,
    apply_builtin_quality_repairs,
    run_release_quality_audit,
    validate_release,
)
from .release_layout import resolve_release_dataset_layout
from .run_ids import new_run_id, run_id_with_tag
from .source import BirdSource
from .source.census import run_census
from .stubs import stub_fn
from .solver.inputs import (
    DEFAULT_WITNESS_K,
    _canonical_nlq,
    load_solver_release_inputs,
    select_solver_release_records,
)
from .workflow import Workflow

PRODUCTION_RELEASE_DIR = Path("release/tend-native-mongodb-v1")
VALIDATION_ISSUE_LIMIT = 12


@dataclass
class Runtime:
    settings: Settings
    ctx: AgentContext
    workflow: Workflow
    progress: object
    log: object
    source: BirdSource | None
    mongo: MongoExecutor


def build_runtime(settings: Settings) -> Runtime:
    settings.paths.ensure()
    run_dir = settings.run_dir
    log = setup_logging(
        run_dir,
        console=False,
        write_llm_markdown_transcripts=settings.llm.write_markdown_transcripts,
    )
    log.info("run_start", run_id=settings.run_id, stub=settings.stub,
             model=settings.llm.model)
    progress = make_reporter(settings.run_id, log, enabled=not settings.quiet)
    source = BirdSource(settings.paths.bird_root)
    llm = LLMClient(settings, log)
    if settings.stub:
        llm.set_stub(stub_fn)
    mongo = MongoExecutor(settings, log)
    ctx = AgentContext(settings=settings, llm=llm, log=log, progress=progress,
                       source=source, mongo=mongo)
    return Runtime(settings, ctx, Workflow(ctx), progress, log, source, mongo)


def build_solver_runtime(settings: Settings, *, run_kind: str = "solver") -> Runtime:
    log = setup_logging(
        settings.run_dir,
        console=False,
        write_llm_markdown_transcripts=settings.llm.write_markdown_transcripts,
    )
    log.info(f"{run_kind}_run_start", run_id=settings.run_id, stub=settings.stub,
             model=settings.llm.model)
    progress = make_reporter(settings.run_id, log, enabled=not settings.quiet)
    llm = LLMClient(settings, log)
    if settings.stub:
        llm.set_stub(stub_fn)
    mongo = MongoExecutor(settings, log)
    ctx = AgentContext(settings=settings, llm=llm, log=log, progress=progress,
                       source=None, mongo=mongo)
    return Runtime(settings, ctx, Workflow(ctx), progress, log, None, mongo)


async def smart_solve_nlq_db_eg(*args: Any, **kwargs: Any) -> Any:
    from .solver.eg import SmartEGPolicy, smart_solve_nlq_db_eg as solve

    if kwargs.get("policy") is None:
        kwargs["policy"] = _smart_eg_policy(SmartEGPolicy, kwargs)
    _strip_policy_kwargs(kwargs)

    return await solve(*args, **kwargs)


async def smart_solve_record_eg(*args: Any, **kwargs: Any) -> Any:
    if len(args) < 3:
        raise TypeError("smart_solve_record_eg requires wf, record, schema")
    wf = args[0]
    record = args[1]
    if not isinstance(record, dict):
        raise TypeError("smart_solve_record_eg record must be a dict")
    db_id = str(record.get("db_id") or "")
    solve_kwargs: dict[str, Any] = {
        "db_id": db_id,
        "nlq": _canonical_nlq(record),
        "record_id": record.get("record_id"),
        "max_tool_turns": kwargs.get("max_tool_turns", DEFAULT_MAX_TOOL_TURNS),
        "max_revisits": kwargs.get("max_revisits", DEFAULT_MAX_REVISITS),
        "cost_budget_usd": kwargs.get("cost_budget_usd", DEFAULT_COST_BUDGET_USD),
        "policy": kwargs.get("policy"),
    }
    if isinstance(kwargs.get("options"), dict):
        solve_kwargs["options"] = kwargs["options"]
    return await smart_solve_nlq_db_eg(wf, **solve_kwargs)


def _smart_eg_policy(policy_cls: Any, kwargs: dict[str, Any]) -> Any:
    options = kwargs.get("options") if isinstance(kwargs.get("options"), dict) else {}
    return policy_cls(
        max_tool_turns=int(
            kwargs.get("max_tool_turns", options.get("max_tool_turns", DEFAULT_MAX_TOOL_TURNS))
        ),
        max_revisits=int(
            kwargs.get("max_revisits", options.get("max_revisits", DEFAULT_MAX_REVISITS))
        ),
        cost_budget_usd=float(
            kwargs.get("cost_budget_usd", options.get("cost_budget_usd", DEFAULT_COST_BUDGET_USD))
        ),
        evidence_gate=bool(options.get("use_evidence_gate", True)),
        counterexample_gate=bool(options.get("use_counterexample", True)),
        value_grounding=bool(options.get("use_value_grounding", True)),
        relationship_probe=bool(options.get("use_relationship_probe", True)),
        prefix_execution=bool(options.get("use_prefix_execution", True)),
        revisit=bool(options.get("use_revisit", True)),
        probe_scheduler=bool(options.get("use_probe_scheduler", True)),
    )


_SOLVER_OPTION_KEYS = {
    "use_evidence_gate",
    "use_counterexample",
    "use_value_grounding",
    "use_relationship_probe",
    "use_prefix_execution",
    "use_revisit",
    "use_probe_scheduler",
}


def _parse_solver_options(items: list[str] | None) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("--solver-option must use KEY=VALUE")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if key not in _SOLVER_OPTION_KEYS:
            allowed = ", ".join(sorted(_SOLVER_OPTION_KEYS))
            raise ValueError(f"unknown solver option {key!r}; allowed: {allowed}")
        options[key] = _parse_option_value(raw_value)
    return options


def _parse_option_value(value: str) -> Any:
    raw = value.strip()
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _strip_policy_kwargs(kwargs: dict[str, Any]) -> None:
    for key in (
        "max_tool_turns",
        "max_revisits",
        "cost_budget_usd",
        "options",
        "witness_preloaded",
    ):
        kwargs.pop(key, None)


def _close_runtime(rt: Runtime) -> None:
    """Release the run's source/mongo/log handles; safe to call once in finally."""
    if rt.source is not None:
        rt.source.close()
    rt.mongo.close()
    rt.log.close()

def _resolve_construct_records(source: BirdSource, db_ids: list[str], value: str) -> int:
    raw = value.strip().lower()
    if raw == "all":
        census = run_census(source, db_ids=db_ids)
        total = sum(db.query_count for db in census.databases.values())
        if total <= 0:
            raise ValueError(f"no source workload records found for dbs={db_ids}")
        return total
    try:
        n_records = int(raw)
    except ValueError as exc:
        raise ValueError("--records must be a positive integer or 'all'") from exc
    if n_records <= 0:
        raise ValueError("--records must be positive")
    return n_records


async def _run_construct(
    rt: Runtime,
    db_ids: list[str],
    phase: str,
    n_records: int,
    *,
    records_per_db: int | None = None,
) -> int:
    out_dir = rt.settings.paths.dataset_out
    artifacts: dict[str, NativeDbArtifacts] = {}
    records: list[dict[str, Any]] = []
    failed: TendError | None = None
    summary: dict[str, Any] = {}
    slot_count = 0
    try:
        with rt.progress:
            if phase in ("A", "all"):
                artifacts = await run_native_phase_a(rt.workflow, db_ids)
                write_native_phase_a(out_dir, artifacts)
                rt.log.info(
                    "native_phase_a_complete",
                    dbs=sorted(artifacts),
                    signatures={db_id: art.world_signature for db_id, art in artifacts.items()},
                    feature_counts={
                        db_id: len(getattr(art.native_feature_manifest, "features", []) or [])
                        for db_id, art in artifacts.items()
                    },
                )
            if phase in ("B", "all"):
                if not artifacts:
                    rt.log.anomaly(
                        kind=Anomaly.INTERNAL,
                        message="native phase B requested without Phase A artifacts",
                        phase=phase,
                        requested_records=n_records,
                    )
                else:
                    manifests = [art.native_feature_manifest for art in artifacts.values()]
                    slot_request_records = n_records
                    slot_cap = records_per_db if records_per_db is not None else None
                    if records_per_db is not None:
                        slot_request_records = n_records * 2
                        slot_cap = records_per_db * 2
                    slots = plan_native_slots(
                        manifests,
                        slot_request_records,
                        seed=rt.settings.seed,
                        records_per_db=slot_cap,
                    )
                    slot_count = len(slots)
                    rt.log.info(
                        "native_slot_plan",
                        requested_records=n_records,
                        oversampled_records=slot_request_records,
                        slots=slot_count,
                        dbs=sorted(artifacts),
                        records_per_db=records_per_db,
                    )
                    raw_records = await run_native_phase_b(rt.workflow, artifacts, slots)
                    records = _select_distinct_native_records(
                        raw_records,
                        db_ids=sorted(artifacts),
                        records_per_db=records_per_db,
                    )
                    write_records(out_dir, records)
                    built_by_db = dict(Counter(str(record.get("db_id")) for record in records))
                    raw_by_db = dict(Counter(str(record.get("db_id")) for record in raw_records))
                    unique_mql_by_db = {
                        db_id: len({
                            str(record.get("MQL") or "")
                            for record in raw_records
                            if record.get("db_id") == db_id
                        })
                        for db_id in sorted(artifacts)
                    }
                    rt.log.info(
                        "native_distinct_selection",
                        raw_records=len(raw_records),
                        selected_records=len(records),
                        raw_by_db=raw_by_db,
                        selected_by_db=built_by_db,
                        unique_mql_by_db=unique_mql_by_db,
                    )
                    rt.log.info(
                        "native_release_summary",
                        requested_records=n_records,
                        built_records=len(records),
                        slots=slot_count,
                        built_by_db=built_by_db,
                    )
                    if len(records) < n_records:
                        rt.log.anomaly(
                            kind=Anomaly.SUPPLY_EXHAUSTED,
                            message="native Phase B record target not met",
                            requested_records=n_records,
                            built_records=len(records),
                        )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error(
            "run_failed",
            error_type=type(err).__name__,
            message=err.message,
            anomaly=err.anomaly.value if err.anomaly else None,
        )
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="construct.native")
        rt.log.anomaly(failed)
        rt.log.error(
            "run_failed",
            error_type=type(failed).__name__,
            message=failed.message,
            anomaly=failed.anomaly.value if failed.anomaly else None,
        )
    finally:
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = failed is not None or summary.get("anomaly_total", 0) > 0
        rt.log.info(
            "run_done",
            status="failed" if failed_run else "ok",
            **summary,
            dbs=len(artifacts),
            records=len(records),
            construction_mode="native",
        )
        _print_summary(rt, artifacts, records, summary, out_dir)
        _close_runtime(rt)

    return 1 if failed or summary.get("anomaly_total", 0) else 0


def _select_distinct_native_records(
    records: list[dict[str, Any]],
    *,
    db_ids: list[str],
    records_per_db: int | None,
) -> list[dict[str, Any]]:
    """Select a release-sized prefix with distinct MQL and maximally distinct NL."""
    if records_per_db is None:
        return records

    selected: list[dict[str, Any]] = []
    by_db: dict[str, list[dict[str, Any]]] = {db_id: [] for db_id in db_ids}
    for record in records:
        db_id = str(record.get("db_id") or "")
        if db_id in by_db:
            by_db[db_id].append(record)

    for db_id in db_ids:
        candidates = by_db.get(db_id, [])
        chosen: list[dict[str, Any]] = []
        seen_mql: set[str] = set()
        seen_nl: set[str] = set()
        for require_new_nl in (True, False):
            for record in candidates:
                if len(chosen) >= records_per_db:
                    break
                mql = str(record.get("MQL") or "")
                nl_queries = record.get("nl_queries") if isinstance(record.get("nl_queries"), dict) else {}
                canonical = str(nl_queries.get("canonical") or "")
                if not mql or mql in seen_mql:
                    continue
                if require_new_nl and canonical in seen_nl:
                    continue
                seen_mql.add(mql)
                seen_nl.add(canonical)
                chosen.append(record)
            if len(chosen) >= records_per_db:
                break
        selected.extend(chosen)
    return selected


async def _preload_solver_witnesses(
    rt: Runtime,
    inputs: list[tuple[dict, dict, dict | None]],
) -> set[str]:
    """Load each db witness once before record-level solver fan-out."""
    if rt.settings.stub or rt.mongo is None or not rt.mongo.available():
        return set()
    by_db: dict[str, dict] = {}
    for record, _schema, data in inputs:
        db = str(record.get("db_id"))
        if db and data and db not in by_db:
            by_db[db] = data
    if not by_db:
        return set()

    async def load_one(db: str, data: dict) -> str:
        await asyncio.to_thread(rt.mongo.load_witness, db, data)
        return db

    loaded = await asyncio.gather(
        *(load_one(db, data) for db, data in sorted(by_db.items()))
    )
    rt.log.info("solver_witness_preloaded", db_ids=loaded, db_count=len(loaded))
    return set(loaded)


async def _evaluate_outputs(
    rt: Runtime,
    *,
    dataset_dir: Path,
    predictions_path: Path,
    experiment_kind: str,
    out_dir: Path | None = None,
    max_workers: int = 8,
) -> EvaluationOutput:
    """Run proposal-05 evaluation while keeping progress and logs in the run."""
    if out_dir is None:
        out_dir = rt.settings.run_dir / "evaluation" / experiment_kind
    return await asyncio.to_thread(
        evaluate_predictions,
        dataset_dir=dataset_dir,
        predictions_path=predictions_path,
        out_dir=out_dir,
        experiment_kind=experiment_kind,
        run_id=rt.settings.run_id,
        logger=rt.log,
        progress=rt.progress,
        executor=rt.mongo,
        max_workers=max_workers,
    )


async def _maybe_evaluate(
    rt: Runtime,
    *,
    predictions: list[dict],
    predictions_path: Path,
    dataset_dir: Path,
    experiment_kind: str,
    evaluate: bool,
    eval_out_dir: Path | None,
    eval_workers: int,
) -> EvaluationOutput | None:
    """Run post-run evaluation on the success path, or ``None`` when not applicable.

    Returns ``None`` when evaluation is disabled (``--no-eval``) or there are no
    predictions to score. Runs under the already-entered progress context, so the
    caller must advance ``rt.progress.phase("EVAL")`` before calling.
    """
    if not evaluate or not predictions:
        return None
    return await _evaluate_outputs(
        rt,
        dataset_dir=dataset_dir,
        predictions_path=predictions_path,
        experiment_kind=experiment_kind,
        out_dir=eval_out_dir,
        max_workers=eval_workers,
    )


def _evaluation_skip_reason(
    *,
    evaluate: bool,
    nlq: str | None,
    evaluation: EvaluationOutput | None,
    evaluation_rows: list[dict],
) -> str | None:
    if evaluation is not None:
        return None
    if not evaluate:
        return "disabled"
    if nlq is not None:
        return "no_release_dataset"
    if not evaluation_rows:
        return "no_predictions"
    return None


def _stage_dir(rt: Runtime, stage: str) -> Path:
    return rt.settings.run_dir / stage


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write ``rows`` one JSON object per line, creating parents; no-op when empty."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _write_jsonl_even_empty(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _materialize_evaluation_dataset_subset(
    dataset_dir: Path,
    records: list[dict[str, Any]],
    out_dir: Path,
) -> Path:
    """Build a run-local release-like dataset for the records this run attempted."""
    layout = resolve_release_dataset_layout(dataset_dir)

    unique_records: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for record in records:
        key = (str(record.get("db_id") or ""), record.get("record_id"))
        if not key[0] or key in seen:
            continue
        unique_records.append(record)
        seen.add(key)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "mongodb_data").mkdir(parents=True, exist_ok=True)
    (out_dir / "mongodb_schema").mkdir(parents=True, exist_ok=True)
    (out_dir / "agent_design_rationale").mkdir(parents=True, exist_ok=True)

    (out_dir / "test.json").write_text(
        json.dumps(unique_records, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tend_records = _filter_record_file(layout.tend_path, seen) or unique_records
    (out_dir / "TEND.json").write_text(
        json.dumps(tend_records, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / "evaluation_selection.json").write_text(
        json.dumps(
            {
                "source_dataset_dir": str(dataset_dir),
                "selected_record_count": len(unique_records),
                "selected_records": [
                    {"db_id": record.get("db_id"), "record_id": record.get("record_id")}
                    for record in unique_records
                ],
                "db_ids": sorted({str(record.get("db_id") or "") for record in unique_records}),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    if layout.catalog_path.exists():
        _link_or_copy_file(layout.catalog_path, out_dir / "bird_db_catalog.json")

    for db in sorted({str(record.get("db_id") or "") for record in unique_records}):
        if not db:
            continue
        layout_sources = (
            (layout.mongodb_data_dir / f"{db}.json", out_dir / "mongodb_data" / f"{db}.json"),
            (layout.mongodb_schema_dir / f"{db}.json", out_dir / "mongodb_schema" / f"{db}.json"),
        )
        for src, dst in layout_sources:
            if src.exists():
                _link_or_copy_file(src, dst)
        rationale_dir = layout.agent_design_rationale_dir
        for suffix in (".yaml", ".yml", ".json"):
            src = rationale_dir / f"{db}{suffix}"
            if src.exists():
                _link_or_copy_file(src, out_dir / "agent_design_rationale" / src.name)
    return out_dir


def _filter_record_file(path: Path, keys: set[tuple[str, Any]]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return [
        row for row in rows
        if isinstance(row, dict) and (str(row.get("db_id") or ""), row.get("record_id")) in keys
    ]


def _link_or_copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        dst.hardlink_to(src)
        return
    except OSError:
        pass
    try:
        dst.symlink_to(src.resolve())
        return
    except OSError:
        pass
    shutil.copy2(src, dst)


def _solver_exception_failure_payload(
    err: TendError,
    *,
    record: dict[str, Any],
    batch_index: int,
) -> dict[str, Any]:
    context = dict(err.context)
    db_id = str(context.get("db_id") or record.get("db_id") or "")
    record_id = context.get("record_id", record.get("record_id"))
    anomaly = err.anomaly.value if err.anomaly else None
    payload: dict[str, Any] = {
        "result_type": "solver_failure",
        "record_id": record_id,
        "db_id": db_id,
        "error_code": str(anomaly or type(err).__name__).upper(),
        "error_type": type(err).__name__,
        "message": err.message,
        "anomaly": anomaly,
        "retryable": err.retryable,
        "context": context,
        "batch_index": batch_index,
        "work_item_id": f"solve:{batch_index}:{db_id}:{record_id}",
    }
    for key, value in context.items():
        payload.setdefault(key, value)
    return payload


async def _run_solve(
    rt: Runtime,
    *,
    dataset_dir: Path,
    db_id: str | None,
    record_id: int | None,
    limit: int,
    max_tool_turns: int,
    max_revisits: int,
    cost_budget_usd: float,
    nlq_track: str = "record",
    nlq: str | None = None,
    evaluate: bool = True,
    eval_out_dir: Path | None = None,
    eval_workers: int = 8,
    solver_options: dict[str, Any] | None = None,
) -> int:
    predictions: list[dict] = []
    failures: list[dict] = []
    failed: TendError | None = None
    evaluation: EvaluationOutput | None = None
    summary: dict = {}
    solve_dir = _stage_dir(rt, "solve")
    out_path = solve_dir / "solver_predictions.jsonl"
    failures_path = solve_dir / "solver_failures.jsonl"
    eval_input_path = solve_dir / "solver_evaluation_inputs.jsonl"
    evaluate_outputs = evaluate and nlq is None
    evaluation_rows: list[dict] = []
    evaluation_dataset_dir: Path | None = None
    solver_options = dict(solver_options or {})
    try:
        with rt.progress:
            rt.workflow.phase("SOLVE")
            if solver_options:
                rt.log.info("solver_policy_options", options=solver_options)
            if nlq is not None:
                if not db_id:
                    raise SourceError("NLQ+DB solver mode requires --db-id")
                record_stub = {"db_id": str(db_id), "record_id": record_id}
                try:
                    solve_kwargs: dict[str, Any] = {
                        "db_id": str(db_id),
                        "nlq": nlq,
                        "record_id": record_id,
                        "max_tool_turns": max_tool_turns,
                        "max_revisits": max_revisits,
                        "cost_budget_usd": cost_budget_usd,
                    }
                    if solver_options:
                        solve_kwargs["options"] = solver_options
                    result = await smart_solve_nlq_db_eg(rt.workflow, **solve_kwargs)
                    payload = result.to_json()
                    payload["batch_index"] = 0
                    payload["work_item_id"] = f"solve:0:{db_id}:{record_id}"
                except Exception as exc:  # noqa: BLE001 - convert one record into a failure row
                    err = wrap_unexpected(exc, stage="solve_record", **record_stub)
                    if not err.logged:
                        rt.log.anomaly(err)
                    payload = _solver_exception_failure_payload(
                        err,
                        record=record_stub,
                        batch_index=0,
                    )
                if payload.get("result_type") == "solver_failure":
                    failures.append(payload)
                else:
                    predictions.append(payload)
            else:
                inputs = load_solver_release_inputs(
                    dataset_dir,
                    db_id=db_id,
                    record_id=record_id,
                    limit=limit,
                    nlq_track=nlq_track,
                )
                if evaluate_outputs and inputs:
                    evaluation_dataset_dir = _materialize_evaluation_dataset_subset(
                        dataset_dir,
                        [record for record, _schema, _data in inputs],
                        solve_dir / "evaluation_dataset",
                    )
                    rt.log.info(
                        "evaluation_dataset_subset_materialized",
                        source_dataset_dir=str(dataset_dir),
                        dataset_dir=str(evaluation_dataset_dir),
                        records=len(inputs),
                        db_ids=sorted({str(record.get("db_id") or "") for record, _, _ in inputs}),
                    )
                if not inputs:
                    rt.log.anomaly(
                        kind=Anomaly.SUPPLY_EXHAUSTED,
                        message="no solver records matched filters",
                        dataset_dir=str(dataset_dir),
                        db_id=db_id,
                        record_id=record_id,
                    )
                preloaded_dbs = await _preload_solver_witnesses(rt, inputs)

                async def solve_one(
                    batch_index: int,
                    record: dict,
                    schema: dict,
                    data: dict | None,
                ) -> tuple[int, dict]:
                    db = str(record.get("db_id"))
                    try:
                        solve_kwargs = {
                            "local_data": data,
                            "max_tool_turns": max_tool_turns,
                            "max_revisits": max_revisits,
                            "cost_budget_usd": cost_budget_usd,
                            "witness_preloaded": db in preloaded_dbs,
                        }
                        if solver_options:
                            solve_kwargs["options"] = solver_options
                        result = await smart_solve_record_eg(
                            rt.workflow,
                            record,
                            schema,
                            **solve_kwargs,
                        )
                        payload = result.to_json()
                        payload["batch_index"] = batch_index
                        payload["work_item_id"] = (
                            f"solve:{batch_index}:{db}:{record.get('record_id')}"
                        )
                        return batch_index, payload
                    except Exception as exc:  # noqa: BLE001 - preserve batch progress.
                        err = wrap_unexpected(
                            exc,
                            stage="solve_record",
                            db_id=db,
                            record_id=record.get("record_id"),
                        )
                        if not err.logged:
                            rt.log.anomaly(err)
                        return batch_index, _solver_exception_failure_payload(
                            err,
                            record=record,
                            batch_index=batch_index,
                        )

                tasks = [
                    asyncio.create_task(solve_one(index, record, schema, data))
                    for index, (record, schema, data) in enumerate(inputs)
                ]
                try:
                    solved = await asyncio.gather(*tasks)
                except Exception:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for exc in results:
                        if isinstance(exc, Exception) and not isinstance(exc, asyncio.CancelledError):
                            rt.log.anomaly(wrap_unexpected(exc, stage="solve_gather"))
                    raise
                for _, payload in sorted(solved, key=lambda item: item[0]):
                    if payload.get("result_type") == "solver_failure":
                        failures.append(payload)
                    else:
                        predictions.append(payload)
            _write_jsonl_even_empty(out_path, predictions)
            _write_jsonl_even_empty(failures_path, failures)
            evaluation_rows = [*predictions, *failures]
            if evaluation_rows and evaluate_outputs and evaluation_dataset_dir is not None:
                _write_jsonl_even_empty(eval_input_path, evaluation_rows)
                rt.progress.phase("EVAL")
                evaluation = await _maybe_evaluate(
                    rt,
                    predictions=evaluation_rows,
                    predictions_path=eval_input_path,
                    dataset_dir=evaluation_dataset_dir,
                    experiment_kind="solver",
                    evaluate=evaluate_outputs,
                    eval_out_dir=eval_out_dir,
                    eval_workers=eval_workers,
                )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("solver_run_failed", error_type=type(err).__name__,
                     message=err.message, anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="solve")
        rt.log.anomaly(failed)
        rt.log.error("solver_run_failed", error_type=type(failed).__name__,
                     message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        _write_jsonl_even_empty(out_path, predictions)
        _write_jsonl_even_empty(failures_path, failures)
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or bool(failures)
            or not predictions
            or (evaluation is not None and not evaluation.ok)
            or summary.get("anomaly_total", 0) > 0
        )
        rt.log.info("solver_run_done", status="failed" if failed_run else "ok",
                    predictions=len(predictions), failures=len(failures),
                    output=str(out_path), failures_output=str(failures_path), **summary)
        _print_solve_summary(
            rt,
            predictions,
            failures,
            summary,
            out_path,
            failures_path,
            evaluation,
            evaluate=evaluate,
            skip_reason=_evaluation_skip_reason(
                evaluate=evaluate,
                nlq=nlq,
                evaluation=evaluation,
                evaluation_rows=evaluation_rows,
            ),
        )
        _close_runtime(rt)

    return 1 if failed_run else 0


async def _run_baseline(
    rt: Runtime,
    *,
    dataset_dir: Path,
    baselines: str,
    db_id: str | None,
    record_id: int | None,
    limit: int,
    witness_k: int,
    nlq: str | None = None,
    nlq_track: str = "record",
    evaluate: bool = True,
    eval_out_dir: Path | None = None,
    eval_workers: int = 8,
) -> int:
    outputs: list[dict] = []
    predictions: list[dict] = []
    failures: list[dict] = []
    failed: TendError | None = None
    evaluation: EvaluationOutput | None = None
    summary: dict = {}
    baseline_dir = _stage_dir(rt, "baseline")
    out_path = baseline_dir / "baseline_predictions.jsonl"
    failures_path = baseline_dir / "baseline_failures.jsonl"
    eval_input_path = baseline_dir / "baseline_evaluation_inputs.jsonl"
    evaluate_outputs = evaluate and nlq is None
    evaluation_rows: list[dict] = []
    evaluation_dataset_dir: Path | None = None
    try:
        with rt.progress:
            if evaluate_outputs:
                selected_records = select_solver_release_records(
                    dataset_dir,
                    db_id=db_id,
                    record_id=record_id,
                    limit=limit,
                    nlq_track=nlq_track,
                )
                if selected_records:
                    evaluation_dataset_dir = _materialize_evaluation_dataset_subset(
                        dataset_dir,
                        selected_records,
                        baseline_dir / "evaluation_dataset",
                    )
                    rt.log.info(
                        "evaluation_dataset_subset_materialized",
                        source_dataset_dir=str(dataset_dir),
                        dataset_dir=str(evaluation_dataset_dir),
                        records=len(selected_records),
                        db_ids=sorted({str(record.get("db_id") or "") for record in selected_records}),
                    )
            outputs = await run_baseline_suite(
                rt.workflow,
                dataset_dir=dataset_dir,
                baseline_selection=baselines,
                db_id=db_id,
                nlq=nlq,
                nlq_track=nlq_track,
                record_id=record_id,
                limit=limit,
                witness_k=witness_k,
            )
            predictions = [item for item in outputs if item.get("status") == "ok"]
            failures = [item for item in outputs if item.get("status") != "ok"]
            _write_jsonl_even_empty(out_path, predictions)
            _write_jsonl_even_empty(failures_path, failures)
            evaluation_rows = [*predictions, *failures]
            if evaluation_rows and evaluate_outputs and evaluation_dataset_dir is not None:
                _write_jsonl_even_empty(eval_input_path, evaluation_rows)
                rt.progress.phase("EVAL")
                evaluation = await _maybe_evaluate(
                    rt,
                    predictions=evaluation_rows,
                    predictions_path=eval_input_path,
                    dataset_dir=evaluation_dataset_dir,
                    experiment_kind="baseline",
                    evaluate=evaluate_outputs,
                    eval_out_dir=eval_out_dir,
                    eval_workers=eval_workers,
                )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("baseline_run_failed", error_type=type(err).__name__,
                     message=err.message, anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="baseline")
        rt.log.anomaly(failed)
        rt.log.error("baseline_run_failed", error_type=type(failed).__name__,
                     message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        _write_jsonl_even_empty(out_path, predictions)
        _write_jsonl_even_empty(failures_path, failures)
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or not predictions
            or bool(failures)
            or (evaluation is not None and not evaluation.ok)
            or summary.get("anomaly_total", 0) > 0
        )
        rt.log.info("baseline_run_done", status="failed" if failed_run else "ok",
                    outputs=len(outputs), predictions=len(predictions),
                    failures=len(failures), output=str(out_path),
                    failures_output=str(failures_path), **summary)
        _print_baseline_summary(
            rt,
            predictions,
            failures,
            summary,
            out_path,
            failures_path,
            evaluation,
            evaluate=evaluate,
            skip_reason=_evaluation_skip_reason(
                evaluate=evaluate,
                nlq=nlq,
                evaluation=evaluation,
                evaluation_rows=evaluation_rows,
            ),
        )
        _close_runtime(rt)

    return 1 if failed_run else 0


async def _run_ablation(
    rt: Runtime,
    *,
    dataset_dir: Path,
    ablations: str,
    db_id: str | None,
    record_id: int | None,
    limit: int,
    max_tool_turns: int,
    max_revisits: int,
    cost_budget_usd: float,
    workers: int = 1,
    nlq: str | None = None,
    nlq_track: str = "record",
    evaluate: bool = True,
    eval_out_dir: Path | None = None,
    eval_workers: int = 8,
) -> int:
    outputs: list[dict] = []
    predictions: list[dict] = []
    failures: list[dict] = []
    failed: TendError | None = None
    evaluation: EvaluationOutput | None = None
    summary: dict = {}
    ablation_dir = _stage_dir(rt, "ablation")
    out_path = ablation_dir / "ablation_predictions.jsonl"
    failures_path = ablation_dir / "ablation_failures.jsonl"
    eval_input_path = ablation_dir / "ablation_evaluation_inputs.jsonl"
    summary_path = ablation_dir / "ablation_summary.json"
    evaluate_outputs = evaluate and nlq is None
    evaluation_rows: list[dict] = []
    evaluation_dataset_dir: Path | None = None
    try:
        with rt.progress:
            if evaluate_outputs:
                selected_records = select_solver_release_records(
                    dataset_dir,
                    db_id=db_id,
                    record_id=record_id,
                    limit=limit,
                    nlq_track=nlq_track,
                )
                if selected_records:
                    evaluation_dataset_dir = _materialize_evaluation_dataset_subset(
                        dataset_dir,
                        selected_records,
                        ablation_dir / "evaluation_dataset",
                    )
                    rt.log.info(
                        "evaluation_dataset_subset_materialized",
                        source_dataset_dir=str(dataset_dir),
                        dataset_dir=str(evaluation_dataset_dir),
                        records=len(selected_records),
                        db_ids=sorted({str(record.get("db_id") or "") for record in selected_records}),
                    )
            outputs = await run_ablation_suite(
                rt.workflow,
                dataset_dir=dataset_dir,
                ablation_selection=ablations,
                db_id=db_id,
                nlq=nlq,
                nlq_track=nlq_track,
                record_id=record_id,
                limit=limit,
                max_tool_turns=max_tool_turns,
                max_revisits=max_revisits,
                cost_budget_usd=cost_budget_usd,
                workers=workers,
            )
            predictions = [item for item in outputs if item.get("status") == "ok"]
            failures = [item for item in outputs if item.get("status") != "ok"]
            _write_jsonl_even_empty(out_path, predictions)
            _write_jsonl_even_empty(failures_path, failures)
            evaluation_rows = [*predictions, *failures]
            if evaluation_rows and evaluate_outputs and evaluation_dataset_dir is not None:
                _write_jsonl_even_empty(eval_input_path, evaluation_rows)
                rt.progress.phase("EVAL")
                evaluation = await _maybe_evaluate(
                    rt,
                    predictions=evaluation_rows,
                    predictions_path=eval_input_path,
                    dataset_dir=evaluation_dataset_dir,
                    experiment_kind="ablation",
                    evaluate=evaluate_outputs,
                    eval_out_dir=eval_out_dir,
                    eval_workers=eval_workers,
                )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("ablation_run_failed", error_type=type(err).__name__,
                     message=err.message, anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="ablation")
        rt.log.anomaly(failed)
        rt.log.error("ablation_run_failed", error_type=type(failed).__name__,
                     message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        _write_jsonl_even_empty(out_path, predictions)
        _write_jsonl_even_empty(failures_path, failures)
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or not outputs
            or (evaluation is not None and evaluation.status == "failed")
            or summary.get("anomaly_total", 0) > 0
        )
        all_variants_failed = bool(outputs) and bool(failures) and not predictions
        if not outputs:
            outcome_status = "no_outputs"
        elif all_variants_failed:
            outcome_status = "all_variants_failed"
        elif failures:
            outcome_status = "partial_variant_failures"
        else:
            outcome_status = "ok"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps({
            "run_id": rt.settings.run_id,
            "status": "failed" if failed_run else "ok",
            "experiment_status": "failed" if failed_run else "ok",
            "outcome_status": outcome_status,
            "all_variants_failed": all_variants_failed,
            "variant_failures_are_scored_outcomes": True,
            "outputs": len(outputs),
            "predictions": len(predictions),
            "failures": len(failures),
            "by_ablation": _count_by(predictions, "ablation_id"),
            "failed_by_ablation": _count_by(failures, "ablation_id"),
            "workers": max(1, int(workers)),
            "evaluation": evaluation.report if evaluation else None,
            "evaluation_headline": (
                evaluation.report.get("headline") if evaluation else None
            ),
            "progress": summary,
            "output": str(out_path),
            "failures_output": str(failures_path),
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        rt.log.info("ablation_run_done", status="failed" if failed_run else "ok",
                    outputs=len(outputs), predictions=len(predictions),
                    failures=len(failures), output=str(out_path),
                    failures_output=str(failures_path), summary_output=str(summary_path),
                    **summary)
        _print_ablation_summary(
            rt,
            predictions,
            failures,
            summary,
            out_path,
            failures_path,
            summary_path,
            evaluation,
            evaluate=evaluate,
            skip_reason=_evaluation_skip_reason(
                evaluate=evaluate,
                nlq=nlq,
                evaluation=evaluation,
                evaluation_rows=evaluation_rows,
            ),
        )
        _close_runtime(rt)

    return 1 if failed_run else 0


def _resolve_repo_path(settings: Settings, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else settings.paths.repo_root / p


def _release_dataset_dir(settings: Settings, dataset_dir: str | None) -> Path:
    """Resolve the solve/baseline/ablation dataset dir, defaulting to the release."""
    return _resolve_repo_path(settings, dataset_dir if dataset_dir else PRODUCTION_RELEASE_DIR)


def _collect_validation_issues(report: ReleaseReport) -> list[str]:
    return (
        report.composition.violations
        + report.record_violations
        + report.schema_violations
        + report.file_violations
    )


def _validate_dataset(
    settings: Settings,
    dataset_dir: Path,
    *,
    smoke: bool,
) -> tuple[ReleaseReport | None, str | None]:
    try:
        report = validate_release(
            dataset_dir,
            schemas_dir=settings.paths.schemas,
            require_all_dbs=not smoke,
        )
    except Exception as exc:  # noqa: BLE001 - CLI validation boundary
        return None, f"{type(exc).__name__}: {exc}"
    return report, None


def _print_validation_summary(
    *,
    title: str,
    dataset_dir: Path,
    mode: str,
    report: ReleaseReport | None,
    error: str | None = None,
    out_dir: Path | None = None,
) -> None:
    ok = bool(report and report.ok and error is None)
    status = "OK" if ok else "INVALID"
    print("\n" + "=" * 64)
    print(f"{title} · validation {status} · mode={mode}")
    print(f"  dataset : {dataset_dir}")
    if out_dir is not None:
        print(f"  out     : {out_dir}")
    if error is not None:
        print(f"  error   : {error}")
    if report is not None:
        c = report.composition
        d = report.diversity
        print(f"  records : {report.n_records}")
        print(f"  coverage: dbs={len(c.db_ids)} L4={c.l4_ratio:.0%} "
              f"L0={c.l0_ratio:.0%} flex={c.flex_ratio:.0%} ssf={c.ssf_ratio:.0%}")
        print(
            f"  diversity: mql={d.distinct_mql} skeletons={d.distinct_mql_skeletons} "
            f"canonical_nl={d.distinct_canonical_nl} "
            f"pairs={d.distinct_nl_mql_pairs} "
            f"max_skeleton_family={d.max_mql_skeleton_family}"
        )
        print(f"  status  : {'valid' if report.ok else 'invalid'}")
        issues = _collect_validation_issues(report)
        print(f"  issues  : {len(issues)}")
        for issue in issues[:VALIDATION_ISSUE_LIMIT]:
            print(f"    - {issue}")
        if len(issues) > VALIDATION_ISSUE_LIMIT:
            print(f"    - ... {len(issues) - VALIDATION_ISSUE_LIMIT} more")
    print("=" * 64)


def _run_validate(settings: Settings, *, dataset_dir: Path, smoke: bool) -> int:
    mode = "smoke" if smoke else "full"
    report, error = _validate_dataset(settings, dataset_dir, smoke=smoke)
    _print_validation_summary(
        title="TEND validate",
        dataset_dir=dataset_dir,
        mode=mode,
        report=report,
        error=error,
    )
    return 0 if report is not None and report.ok and error is None else 1


def _print_quality_summary(report: ReleaseQualityReport) -> None:
    print("\n" + "=" * 64)
    print(f"TEND quality-audit · {'OK' if report.ok else 'INVALID'}")
    print(f"  dataset : {report.dataset_dir}")
    print(f"  records : {report.records_checked}")
    print(f"  errors  : {report.errors}")
    print(f"  warnings: {report.warnings}")
    if report.paths:
        print(f"  report  : {report.paths.get('report_md')}")
        print(f"  issues  : {report.paths.get('issues_jsonl')}")
    if report.by_code:
        print("  by_code :")
        for code, count in sorted(report.by_code.items(), key=lambda item: (-item[1], item[0]))[:12]:
            print(f"    - {code}: {count}")
    for issue in report.issues[:VALIDATION_ISSUE_LIMIT]:
        track = f" track={issue.track}" if issue.track else ""
        print(
            f"    - [{issue.severity}] {issue.code} "
            f"db={issue.db_id} record={issue.record_id}{track}: {issue.message}"
        )
    if len(report.issues) > VALIDATION_ISSUE_LIMIT:
        print(f"    - ... {len(report.issues) - VALIDATION_ISSUE_LIMIT} more")
    print("=" * 64)


def _run_quality_audit(
    rt: Runtime,
    *,
    dataset_dir: Path,
    out_dir: Path,
    db_id: str | None,
    record_id: int | None,
    limit: int | None,
    repeat_order_sensitive: int,
    check_nlq: bool,
    check_field_paths: bool,
) -> int:
    try:
        report = run_release_quality_audit(
            dataset_dir,
            executor=rt.mongo,
            out_dir=out_dir,
            logger=rt.log,
            db_id=db_id,
            record_id=record_id,
            limit=limit,
            repeat_order_sensitive=repeat_order_sensitive,
            check_nlq=check_nlq,
            check_field_paths=check_field_paths,
        )
    finally:
        _close_runtime(rt)
    _print_quality_summary(report)
    return 0 if report.ok else 1


def _run_repair_release_quality(settings: Settings, *, dataset_dir: Path) -> int:
    summary = apply_builtin_quality_repairs(dataset_dir)
    print("\n" + "=" * 64)
    print("TEND repair-release-quality")
    print(f"  dataset        : {dataset_dir}")
    print(f"  records        : {summary.records}")
    print(f"  mql_changed    : {summary.mql_changed}")
    print(f"  cfs_recomputed : {summary.cfs_recomputed}")
    print(f"  nlq_changed    : {summary.nlq_changed}")
    print(f"  sort_stabilized: {summary.sort_stabilized}")
    print("  files:")
    for path in summary.output_files:
        print(f"    - {path}")
    print("=" * 64)
    return 0


def _copy_release_tree(dataset_dir: Path, out_dir: Path) -> None:
    if dataset_dir.resolve() == out_dir.resolve():
        return
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{out_dir.name}.",
        dir=str(out_dir.parent),
    ) as tmp:
        staged = Path(tmp) / out_dir.name
        shutil.copytree(
            dataset_dir,
            staged,
            ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"),
        )
        if out_dir.exists():
            shutil.rmtree(out_dir)
        shutil.move(str(staged), str(out_dir))


def _run_publish(settings: Settings, *, dataset_dir: Path, out_dir: Path) -> int:
    mode = "full"
    report, error = _validate_dataset(settings, dataset_dir, smoke=False)
    _print_validation_summary(
        title="TEND publish",
        dataset_dir=dataset_dir,
        mode=mode,
        report=report,
        error=error,
        out_dir=out_dir,
    )
    if report is None or not report.ok or error is not None:
        print("publish refused: input dataset did not pass full validation")
        return 1
    _copy_release_tree(dataset_dir, out_dir)
    print(f"published release: {out_dir}")
    return 0


async def _run_evaluate(
    rt: Runtime,
    *,
    dataset_dir: Path,
    predictions_path: Path,
    kind: str,
    out_dir: Path | None,
    workers: int,
) -> int:
    evaluation: EvaluationOutput | None = None
    failed: TendError | None = None
    try:
        with rt.progress:
            evaluation = await _evaluate_outputs(
                rt,
                dataset_dir=dataset_dir,
                predictions_path=predictions_path,
                experiment_kind=kind,
                out_dir=out_dir or rt.settings.run_dir / "evaluation" / kind,
                max_workers=workers,
            )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("evaluation_run_failed", error_type=type(err).__name__,
                     message=err.message, anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="evaluate")
        rt.log.anomaly(failed)
        rt.log.error("evaluation_run_failed", error_type=type(failed).__name__,
                     message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or evaluation is None
            or not evaluation.ok
            or summary.get("anomaly_total", 0) > 0
        )
        rt.log.info("evaluation_run_done", status="failed" if failed_run else "ok", **summary)
        print("\n" + "=" * 64)
        print(f"TEND evaluate · run {rt.settings.run_id}")
        print(f"  predictions : {predictions_path}")
        print(f"  dataset     : {dataset_dir}")
        print(f"  anomalies   : {summary.get('anomaly_total', 0)} "
              f"{summary.get('anomalies_by_kind', {})}")
        _print_evaluation_block(evaluation)
        print(f"  logs        : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl | progress.jsonl")
        print("=" * 64)
        _close_runtime(rt)
    return 1 if failed_run else 0


def _add_eval_args(parser: argparse.ArgumentParser) -> None:
    """Attach the shared automatic-evaluation flags to a solve-style subparser."""
    parser.add_argument("--no-eval", action="store_true",
                        help="skip automatic proposal-05 evaluation after generation")
    parser.add_argument("--eval-out", default=None,
                        help="override automatic evaluation output dir")
    parser.add_argument("--eval-workers", type=int, default=8,
                        help="parallel worker count for automatic evaluation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tend",
        description="TEND construction pipeline and SMART solver",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("construct", help="run the construction pipeline")
    c.add_argument("--phase", choices=["A", "B", "all"], default="all")
    c.add_argument("--dbs", default="financial",
                   help="comma-separated db_ids, or 'all' (default: financial)")
    c.add_argument(
        "--records",
        default="1",
        help="Phase B records to attempt, or 'all' for the selected source workload count",
    )
    c.add_argument(
        "--records-per-db",
        type=int,
        default=None,
        help="Phase B records to attempt for each selected db; useful for all-db 100+ runs",
    )
    c.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    c.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    c.add_argument("--run-id", default=None)

    v = sub.add_parser("validate", help="validate a dataset directory")
    v.add_argument("--dataset-dir", required=True, help="dataset dir to validate")
    v.add_argument("--smoke", action="store_true",
                   help="smoke validation: relax all-DB composition only")

    q = sub.add_parser(
        "quality-audit",
        help="strict Mongo-backed NLQ/MQL/DB release quality audit",
    )
    q.add_argument("--dataset-dir", default=str(PRODUCTION_RELEASE_DIR),
                   help="release dataset dir (default: release/tend-native-mongodb-v1)")
    q.add_argument("--out", default=None,
                   help="quality report output dir (default: runs/<run_id>/quality_audit)")
    q.add_argument("--db-id", default=None, help="optional db_id filter")
    q.add_argument("--record-id", type=int, default=None, help="optional record_id filter")
    q.add_argument("--limit", type=int, default=None, help="optional record limit after filters")
    q.add_argument("--repeat-order-sensitive", type=int, default=2,
                   help="repeat order-sensitive gold MQL executions to catch instability")
    q.add_argument("--no-nlq-check", action="store_true",
                   help="skip deterministic NLQ/MQL alignment warnings")
    q.add_argument("--no-field-check", action="store_true",
                   help="skip stage-level field-existence probes")
    q.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    q.add_argument("--run-id", default=None)

    rq = sub.add_parser(
        "repair-release-quality",
        help="apply deterministic release quality repairs and refresh derived files",
    )
    rq.add_argument("--dataset-dir", default=str(PRODUCTION_RELEASE_DIR),
                    help="release dataset dir (default: release/tend-native-mongodb-v1)")
    rq.add_argument("--run-id", default=None)

    p = sub.add_parser("publish", help="validate and copy a production release")
    p.add_argument("--dataset-dir", required=True, help="candidate dataset dir")
    p.add_argument("--out", default=str(PRODUCTION_RELEASE_DIR),
                   help="production release dir (default: release/tend-native-mongodb-v1)")

    s = sub.add_parser("solve", help="run the SMART-EG schema-less solver")
    s.add_argument("--dataset-dir", default=None,
                   help="release dataset dir (default: release/tend-native-mongodb-v1)")
    s.add_argument("--db-id", default=None, help="optional db_id filter")
    s.add_argument("--nlq-track", choices=["record", "canonical", "colloquial"],
                   default="record",
                   help="release-mode NLQ track: record keeps canonical+colloquial; "
                        "canonical/colloquial run that text as the sole query track")
    s.add_argument("--nlq", default=None,
                   help="solve one natural-language question against --db-id by querying MongoDB")
    s.add_argument("--record-id", type=int, default=None, help="optional record_id filter")
    s.add_argument("--limit", type=int, default=1, help="max records to solve")
    s.add_argument("--max-tool-turns", type=int, default=DEFAULT_MAX_TOOL_TURNS,
                   help="SMART-EG maximum provider tool turns")
    s.add_argument("--max-revisits", type=int, default=DEFAULT_MAX_REVISITS,
                   help="SMART-EG maximum milestone revisits")
    s.add_argument("--cost-budget-usd", type=float, default=DEFAULT_COST_BUDGET_USD,
                   help="SMART-EG cost budget in USD")
    s.add_argument("--solver-option", action="append", default=[],
                   help="SMART-EG policy option as KEY=VALUE; repeatable")
    s.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    s.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    _add_eval_args(s)
    s.add_argument("--run-id", default=None)

    b = sub.add_parser("baseline", help="run constrained LLM baselines")
    b.add_argument("--dataset-dir", default=None,
                   help="release dataset dir (default: release/tend-native-mongodb-v1)")
    b.add_argument("--baselines", default="all",
                   help=f"comma-separated baseline ids or all; known={','.join(BASELINE_IDS)}")
    b.add_argument("--db-id", default=None, help="optional db_id filter")
    b.add_argument("--nlq-track", choices=["record", "canonical", "colloquial"],
                   default="record",
                   help="release-mode NLQ track: record keeps canonical+colloquial; "
                        "canonical/colloquial run that text as the sole query track")
    b.add_argument("--nlq", default=None,
                   help="run baselines for one natural-language question against --db-id")
    b.add_argument("--record-id", type=int, default=None, help="optional record_id filter")
    b.add_argument("--limit", type=int, default=1, help="max records per baseline")
    b.add_argument("--witness-k", type=int, default=DEFAULT_WITNESS_K,
                   help="public witness sample count for baselines that use samples")
    b.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    b.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    _add_eval_args(b)
    b.add_argument("--run-id", default=None)

    a = sub.add_parser("ablation", help="run SMART-EG solver ablation study")
    a.add_argument("--dataset-dir", default=None,
                   help="release dataset dir (default: release/tend-native-mongodb-v1)")
    a.add_argument("--ablations", default="all",
                   help=f"comma-separated ablation ids or all; known={','.join(ABLATION_IDS)}")
    a.add_argument("--db-id", default=None, help="optional db_id filter")
    a.add_argument("--nlq-track", choices=["record", "canonical", "colloquial"],
                   default="record",
                   help="release-mode NLQ track: record keeps canonical+colloquial; "
                        "canonical/colloquial run that text as the sole query track")
    a.add_argument("--nlq", default=None,
                   help="run ablations for one natural-language question against --db-id")
    a.add_argument("--record-id", type=int, default=None, help="optional record_id filter")
    a.add_argument("--limit", type=int, default=1, help="max records per ablation")
    a.add_argument("--max-tool-turns", type=int, default=DEFAULT_MAX_TOOL_TURNS,
                   help="SMART-EG maximum provider tool turns")
    a.add_argument("--max-revisits", type=int, default=DEFAULT_MAX_REVISITS,
                   help="SMART-EG maximum milestone revisits")
    a.add_argument("--cost-budget-usd", type=float, default=DEFAULT_COST_BUDGET_USD,
                   help="SMART-EG cost budget in USD")
    a.add_argument("--workers", type=int, default=1,
                   help="parallel ablation run fan-out (does not affect evaluation workers)")
    a.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    a.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    _add_eval_args(a)
    a.add_argument("--run-id", default=None)

    e = sub.add_parser("evaluate", help="evaluate a prediction JSONL with proposal-05 metrics")
    e.add_argument("--dataset-dir", required=True, help="release dataset dir")
    e.add_argument("--predictions", required=True, help="prediction JSONL file")
    e.add_argument("--kind", default="manual",
                   help="experiment kind label: solver, baseline, ablation, or manual")
    e.add_argument("--out", default=None,
                   help="evaluation output dir (default: runs/<run_id>/evaluation/<kind>)")
    e.add_argument("--workers", type=int, default=8, help="parallel evaluator workers")
    e.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    e.add_argument("--run-id", default=None)

    args = parser.parse_args(argv)

    overrides = {}
    if getattr(args, "stub", False):
        overrides["TEND_LLM_STUB"] = "1"
    if getattr(args, "quiet", False):
        overrides["TEND_QUIET"] = "1"
    run_id_tag = getattr(args, "run_id", None)
    run_id = run_id_with_tag(run_id_tag) if run_id_tag else new_run_id()
    settings = Settings.from_env(
        run_id=run_id,
        overrides=overrides,
        require_bird=args.command == "construct",
        require_llm=args.command in {"construct", "solve", "baseline", "ablation"},
    )

    if args.command == "validate":
        return _run_validate(
            settings,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
            smoke=args.smoke,
        )
    if args.command == "quality-audit":
        rt = build_solver_runtime(settings, run_kind="quality_audit")
        return _run_quality_audit(
            rt,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
            out_dir=(
                _resolve_repo_path(settings, args.out)
                if args.out
                else settings.run_dir / "quality_audit"
            ),
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            repeat_order_sensitive=max(1, args.repeat_order_sensitive),
            check_nlq=not args.no_nlq_check,
            check_field_paths=not args.no_field_check,
        )
    if args.command == "repair-release-quality":
        return _run_repair_release_quality(
            settings,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
        )
    if args.command == "publish":
        return _run_publish(
            settings,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
            out_dir=_resolve_repo_path(settings, args.out),
        )
    if args.command == "evaluate":
        rt = build_solver_runtime(settings, run_kind="evaluation")
        return asyncio.run(_run_evaluate(
            rt,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
            predictions_path=_resolve_repo_path(settings, args.predictions),
            kind=args.kind,
            out_dir=_resolve_repo_path(settings, args.out) if args.out else None,
            workers=args.workers,
        ))
    if args.command == "construct":
        rt = build_runtime(settings)
        if rt.source is None:
            raise RuntimeError("construct requires a BIRD source")
        db_ids = list(rt.source.db_ids) if args.dbs == "all" else [
            db.strip() for db in args.dbs.split(",") if db.strip()
        ]
        if args.records_per_db is not None:
            if args.records_per_db <= 0:
                raise ValueError("--records-per-db must be positive")
            n_records = args.records_per_db * len(db_ids)
        else:
            n_records = _resolve_construct_records(rt.source, db_ids, args.records)
        return asyncio.run(_run_construct(
            rt,
            db_ids,
            args.phase,
            n_records,
            records_per_db=args.records_per_db,
        ))
    if args.command == "solve":
        rt = build_solver_runtime(settings)
        return asyncio.run(_run_solve(
            rt,
            dataset_dir=_release_dataset_dir(settings, args.dataset_dir),
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            max_tool_turns=args.max_tool_turns,
            max_revisits=args.max_revisits,
            cost_budget_usd=args.cost_budget_usd,
            nlq_track=args.nlq_track,
            nlq=args.nlq,
            evaluate=not args.no_eval,
            eval_out_dir=_resolve_repo_path(settings, args.eval_out) if args.eval_out else None,
            eval_workers=args.eval_workers,
            solver_options=_parse_solver_options(args.solver_option),
        ))
    if args.command == "baseline":
        rt = build_solver_runtime(settings, run_kind="baseline")
        return asyncio.run(_run_baseline(
            rt,
            dataset_dir=_release_dataset_dir(settings, args.dataset_dir),
            baselines=args.baselines,
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            witness_k=args.witness_k,
            nlq_track=args.nlq_track,
            nlq=args.nlq,
            evaluate=not args.no_eval,
            eval_out_dir=_resolve_repo_path(settings, args.eval_out) if args.eval_out else None,
            eval_workers=args.eval_workers,
        ))
    if args.command == "ablation":
        rt = build_solver_runtime(settings, run_kind="ablation")
        return asyncio.run(_run_ablation(
            rt,
            dataset_dir=_release_dataset_dir(settings, args.dataset_dir),
            ablations=args.ablations,
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            max_tool_turns=args.max_tool_turns,
            max_revisits=args.max_revisits,
            cost_budget_usd=args.cost_budget_usd,
            workers=args.workers,
            nlq_track=args.nlq_track,
            nlq=args.nlq,
            evaluate=not args.no_eval,
            eval_out_dir=_resolve_repo_path(settings, args.eval_out) if args.eval_out else None,
            eval_workers=args.eval_workers,
        ))
    parser.error("unknown command")  # raises SystemExit; subparsers are required


if __name__ == "__main__":
    sys.exit(main())
