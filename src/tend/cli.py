"""TEND command-line entry point.

    tend construct --phase all --dbs financial --records 1 [--stub] [--quiet]
    tend solve --db-id financial --record-id 1001 [--stub] [--quiet]

Assembles the runtime (logging + progress + BIRD source + LLM client + MongoDB executor),
runs the Phase A / Phase B workflow flows or the SMART solver, persists outputs, and
prints a run summary. The run id namespaces everything under ``runs/<run_id>/``.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from .agents import AgentContext
from .config import Settings
from .dataset import write_catalog, write_phase_a, write_records
from .errors import Anomaly, TendError, wrap_unexpected
from .execution.mongo import MongoExecutor
from .llm import LLMClient
from .observability import make_reporter, new_run_id, setup_logging
from .source import BirdSource
from .stubs import stub_fn
from .solver.workflow import (
    DEFAULT_R_MAX,
    DEFAULT_WITNESS_K,
    load_solver_release_inputs,
    smart_solve_record,
)
from .workflow import Workflow, run_phase_a, run_phase_b
from .workflow.flows import CoverageSlot, DbArtifacts


@dataclass
class Runtime:
    settings: Settings
    ctx: AgentContext
    workflow: Workflow
    progress: object
    log: object
    source: BirdSource
    mongo: MongoExecutor


def build_runtime(settings: Settings) -> Runtime:
    settings.paths.ensure()
    run_dir = settings.run_dir
    log = setup_logging(run_dir, console=False)
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


def _slots_for(artifacts: dict[str, DbArtifacts], n_records: int) -> list[CoverageSlot]:
    """Build coverage slots for the composition-critical L4 schema-flex cell first."""
    eligible = [
        a for a in artifacts.values()
        if any(isinstance(node, dict) and node.get("__variants")
               for node in a.mongodb_schema.values())
    ] or [a for a in artifacts.values() if a.query_bearing] or list(artifacts.values())
    slots: list[CoverageSlot] = []
    rid = 1001
    i = 0
    while len(slots) < n_records and eligible:
        art = eligible[i % len(eligible)]
        slots.append(CoverageSlot(db_id=art.db_id, mechanism="optional_embed",
                                  archetype="schema_flex_variant_summary", record_id=rid,
                                  target_difficulty="L4",
                                  target_sql_infeasibility_class="structural_schema_flex",
                                  target_schema_flex="polymorphic"))
        rid += 1
        i += 1
    return slots


async def _run_construct(rt: Runtime, db_ids: list[str], phase: str, n_records: int) -> int:
    out_dir = rt.settings.paths.dataset_out
    artifacts: dict[str, DbArtifacts] = {}
    records: list[dict] = []
    failed: TendError | None = None
    summary: dict = {}
    try:
        with rt.progress:
            if phase in ("A", "all"):
                artifacts = await run_phase_a(rt.workflow, db_ids)
                write_phase_a(out_dir, artifacts)
                write_catalog(out_dir, artifacts)
                rt.log.info("phase_a_complete", dbs=sorted(artifacts),
                            signatures={d: a.world_signature for d, a in artifacts.items()})
            if phase in ("B", "all"):
                if not artifacts:
                    rt.log.anomaly(
                        kind=Anomaly.INTERNAL,
                        message="phase B requested without Phase A artifacts",
                        phase=phase,
                        requested_records=n_records,
                    )
                else:
                    slots = _slots_for(artifacts, n_records)
                    records = await run_phase_b(rt.workflow, artifacts, slots)
                    write_records(out_dir, records)
                    rt.log.info("phase_b_complete", records=len(records),
                                requested_records=n_records)
                    if len(records) < n_records:
                        rt.log.anomaly(
                            kind=Anomaly.SUPPLY_EXHAUSTED,
                            message="phase B record target not met",
                            requested_records=n_records,
                            built_records=len(records),
                        )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("run_failed", error_type=type(err).__name__, message=err.message,
                     anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="construct")
        rt.log.anomaly(failed)
        rt.log.error("run_failed", error_type=type(failed).__name__, message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = failed is not None or summary.get("anomaly_total", 0) > 0
        rt.log.info("run_done", status="failed" if failed_run else "ok", **summary,
                    dbs=len(artifacts), records=len(records))
        _print_summary(rt, artifacts, records, summary, out_dir)
        rt.source.close()
        rt.mongo.close()
        rt.log.close()

    return 1 if failed or summary.get("anomaly_total", 0) else 0


async def _run_solve(
    rt: Runtime,
    *,
    dataset_dir: Path,
    db_id: str | None,
    record_id: int | None,
    limit: int,
    r_max: int,
    witness_k: int,
) -> int:
    predictions: list[dict] = []
    failed: TendError | None = None
    summary: dict = {}
    try:
        inputs = load_solver_release_inputs(
            dataset_dir,
            db_id=db_id,
            record_id=record_id,
            limit=limit,
        )
        if not inputs:
            rt.log.anomaly(
                kind=Anomaly.SUPPLY_EXHAUSTED,
                message="no solver records matched filters",
                dataset_dir=str(dataset_dir),
                db_id=db_id,
                record_id=record_id,
            )
        with rt.progress:
            rt.workflow.phase("SOLVE")
            for record, schema, data in inputs:
                pred = await smart_solve_record(
                    rt.workflow,
                    record,
                    schema,
                    local_data=data,
                    r_max=r_max,
                    witness_k=witness_k,
                )
                predictions.append(pred.to_json())
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
                     message=failed.message, anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        out_path = rt.settings.run_dir / "solver_predictions.jsonl"
        if predictions:
            import json

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as fp:
                for pred in predictions:
                    fp.write(json.dumps(pred, ensure_ascii=False, default=str) + "\n")
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or not predictions
            or summary.get("anomaly_total", 0) > 0
        )
        rt.log.info("solver_run_done", status="failed" if failed_run else "ok",
                    predictions=len(predictions), output=str(out_path), **summary)
        _print_solve_summary(rt, predictions, summary, out_path)
        rt.source.close()
        rt.mongo.close()
        rt.log.close()

    return 1 if failed or not predictions else 0


def _print_summary(rt, artifacts, records, summary, out_dir) -> None:
    print("\n" + "=" * 64)
    print(f"TEND construct · run {rt.settings.run_id} · "
          f"{'STUB' if rt.settings.stub else 'LIVE ' + rt.settings.llm.model}")
    print(f"  Phase A dbs : {len(artifacts)}  -> {sorted(artifacts)}")
    for db_id, art in sorted(artifacts.items()):
        coll = art.mongodb_data
        print(f"    {db_id:24} sig={art.world_signature[:20]}.. "
              f"collections={len(coll)} query_bearing={art.query_bearing}")
    print(f"  Phase B records : {len(records)}")
    for r in records[:5]:
        print(f"    #{r['record_id']} {r['db_id']} {r['difficulty']} "
              f"{r.get('sql_infeasibility_class')}")
    print(f"  anomalies : {summary.get('anomaly_total', 0)} {summary.get('anomalies_by_kind', {})}")
    print(f"  logs   : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl")
    print(f"  output : {out_dir}")
    print("=" * 64)


def _print_solve_summary(rt, predictions, summary, out_path) -> None:
    print("\n" + "=" * 64)
    print(f"TEND solve · run {rt.settings.run_id} · "
          f"{'STUB' if rt.settings.stub else 'LIVE ' + rt.settings.llm.model}")
    print(f"  predictions : {len(predictions)}")
    for pred in predictions[:5]:
        print(f"    #{pred.get('record_id')} {pred.get('db_id')} "
              f"mql={str(pred.get('MQL', ''))[:96]}")
    print(f"  anomalies : {summary.get('anomaly_total', 0)} {summary.get('anomalies_by_kind', {})}")
    print(f"  logs   : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl")
    print(f"  output : {out_path}")
    print("=" * 64)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tend", description="TEND construction pipeline and SMART solver")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("construct", help="run the construction pipeline")
    c.add_argument("--phase", choices=["A", "B", "all"], default="all")
    c.add_argument("--dbs", default="financial",
                   help="comma-separated db_ids, or 'all' (default: financial)")
    c.add_argument("--records", type=int, default=1, help="Phase B records to attempt")
    c.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    c.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    c.add_argument("--run-id", default=None)

    s = sub.add_parser("solve", help="run the SMART schema-less reference solver")
    s.add_argument("--dataset-dir", default=None,
                   help="release dataset dir (default: release/TEND-dataset)")
    s.add_argument("--db-id", default=None, help="optional db_id filter")
    s.add_argument("--record-id", type=int, default=None, help="optional record_id filter")
    s.add_argument("--limit", type=int, default=1, help="max records to solve")
    s.add_argument("--r-max", type=int, default=DEFAULT_R_MAX, help="SMART fallback limit")
    s.add_argument("--witness-k", type=int, default=DEFAULT_WITNESS_K,
                   help="prompt witness disclosure limit")
    s.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    s.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    s.add_argument("--run-id", default=None)

    args = parser.parse_args(argv)

    overrides = {}
    if args.stub:
        overrides["TEND_LLM_STUB"] = "1"
    if args.quiet:
        overrides["TEND_QUIET"] = "1"
    run_id = args.run_id or new_run_id()
    settings = Settings.from_env(run_id=run_id, overrides=overrides)

    rt = build_runtime(settings)
    if args.command == "construct":
        db_ids = list(rt.source.db_ids) if args.dbs == "all" else [
            db.strip() for db in args.dbs.split(",") if db.strip()
        ]
        return asyncio.run(_run_construct(rt, db_ids, args.phase, args.records))
    if args.command == "solve":
        dataset_dir = Path(args.dataset_dir) if args.dataset_dir else settings.paths.dataset_out
        return asyncio.run(_run_solve(
            rt,
            dataset_dir=dataset_dir,
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            r_max=args.r_max,
            witness_k=args.witness_k,
        ))
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
