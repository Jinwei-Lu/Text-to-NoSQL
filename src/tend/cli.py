"""TEND command-line entry point.

    tend construct --phase all --dbs financial --records 1 [--stub] [--quiet]
    tend validate --dataset-dir runs/<run_id>/dataset [--smoke]
    tend publish --dataset-dir runs/<run_id>/dataset --out release/TEND-dataset
    tend solve --db-id financial --record-id 1001 [--stub] [--quiet]

Assembles the runtime (logging + progress + BIRD source + LLM client + MongoDB executor),
runs the Phase A / Phase B workflow flows or the SMART solver, persists outputs, and
prints a run summary. The run id namespaces everything under ``runs/<run_id>/``.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .agents import AgentContext
from .baselines import BASELINE_IDS, run_baseline_suite
from .config import Settings
from .dataset import write_catalog, write_phase_a, write_records
from .errors import Anomaly, TendError, wrap_unexpected
from .execution.mongo import MongoExecutor
from .llm import LLMClient
from .observability import make_reporter, new_run_id, setup_logging
from .publish import ReleaseReport, validate_release
from .source import BirdSource
from .source.census import CoverageRequest, plan_coverage_slots, run_census
from .stubs import stub_fn
from .solver.workflow import (
    DEFAULT_R_MAX,
    DEFAULT_WITNESS_K,
    load_solver_release_inputs,
    smart_solve_record,
)
from .workflow import Workflow, run_phase_a, run_phase_b
from .workflow.flows import CoverageSlot, DbArtifacts

PRODUCTION_RELEASE_DIR = Path("release/TEND-dataset")
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


def build_solver_runtime(settings: Settings, *, run_kind: str = "solver") -> Runtime:
    run_dir = settings.run_dir
    log = setup_logging(run_dir, console=False)
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


def _slot_from_request(request: CoverageRequest, record_id: int) -> CoverageSlot:
    target_schema_flex = (
        "polymorphic"
        if request.sql_infeasibility_class == "structural_schema_flex"
        else "none"
    )
    return CoverageSlot(
        db_id=request.db_id,
        mechanism=request.mechanism,
        archetype=request.archetype,
        record_id=record_id,
        target_difficulty=request.target_difficulty,
        target_sql_infeasibility_class=request.sql_infeasibility_class,
        target_schema_flex=target_schema_flex,
    )


def _coverage_slots_for(
    source: BirdSource,
    db_ids: list[str],
    n_records: int,
    *,
    seed: int,
) -> list[CoverageSlot]:
    census = run_census(source, db_ids=db_ids)
    requests = plan_coverage_slots(census, n_records=n_records, seed=seed)
    return [
        _slot_from_request(request, record_id=1001 + i)
        for i, request in enumerate(requests)
    ]


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
                    if rt.source is None:
                        raise RuntimeError("construct requires a BIRD source")
                    slot_db_ids = sorted(artifacts)
                    non_query_bearing = sorted(
                        db_id for db_id, art in artifacts.items() if not art.query_bearing
                    )
                    if non_query_bearing:
                        rt.log.warning(
                            "phase_b_non_query_bearing_advisory",
                            dbs=non_query_bearing,
                            reason=(
                                "phase A SC marked artifacts as non query-bearing; "
                                "Phase B still uses deterministic census supply"
                            ),
                        )
                    slots = _coverage_slots_for(
                        rt.source,
                        slot_db_ids,
                        n_records,
                        seed=rt.settings.seed,
                    )
                    records = await run_phase_b(rt.workflow, artifacts, slots)
                    write_records(out_dir, records)
                    rt.log.info("phase_b_complete", records=len(records), slots=len(slots),
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
        if rt.source is not None:
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
    failures: list[dict] = []
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
                result = await smart_solve_record(
                    rt.workflow,
                    record,
                    schema,
                    local_data=data,
                    r_max=r_max,
                    witness_k=witness_k,
                )
                payload = result.to_json()
                if payload.get("result_type") == "solver_failure":
                    failures.append(payload)
                else:
                    predictions.append(payload)
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
        out_path = rt.settings.run_dir / "solver_predictions.jsonl"
        failures_path = rt.settings.run_dir / "solver_failures.jsonl"
        if predictions:
            import json

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as fp:
                for pred in predictions:
                    fp.write(json.dumps(pred, ensure_ascii=False, default=str) + "\n")
        if failures:
            import json

            failures_path.parent.mkdir(parents=True, exist_ok=True)
            with failures_path.open("w", encoding="utf-8") as fp:
                for item in failures:
                    fp.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or bool(failures)
            or not predictions
            or summary.get("anomaly_total", 0) > 0
        )
        rt.log.info("solver_run_done", status="failed" if failed_run else "ok",
                    predictions=len(predictions), failures=len(failures),
                    output=str(out_path), failures_output=str(failures_path), **summary)
        _print_solve_summary(rt, predictions, failures, summary, out_path, failures_path)
        if rt.source is not None:
            rt.source.close()
        rt.mongo.close()
        rt.log.close()

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
) -> int:
    outputs: list[dict] = []
    failed: TendError | None = None
    summary: dict = {}
    try:
        with rt.progress:
            outputs = await run_baseline_suite(
                rt.workflow,
                dataset_dir=dataset_dir,
                baseline_selection=baselines,
                db_id=db_id,
                record_id=record_id,
                limit=limit,
                witness_k=witness_k,
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
        out_path = rt.settings.run_dir / "baseline_predictions.jsonl"
        failures_path = rt.settings.run_dir / "baseline_failures.jsonl"
        predictions = [item for item in outputs if item.get("status") == "ok"]
        failures = [item for item in outputs if item.get("status") != "ok"]
        if predictions:
            import json

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as fp:
                for output in predictions:
                    fp.write(json.dumps(output, ensure_ascii=False, default=str) + "\n")
        if failures:
            import json

            failures_path.parent.mkdir(parents=True, exist_ok=True)
            with failures_path.open("w", encoding="utf-8") as fp:
                for output in failures:
                    fp.write(json.dumps(output, ensure_ascii=False, default=str) + "\n")
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or not predictions
            or bool(failures)
            or summary.get("anomaly_total", 0) > 0
        )
        rt.log.info("baseline_run_done", status="failed" if failed_run else "ok",
                    outputs=len(outputs), predictions=len(predictions),
                    failures=len(failures), output=str(out_path),
                    failures_output=str(failures_path), **summary)
        _print_baseline_summary(rt, predictions, failures, summary, out_path, failures_path)
        if rt.source is not None:
            rt.source.close()
        rt.mongo.close()
        rt.log.close()

    return 1 if failed_run else 0


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


def _print_solve_summary(rt, predictions, failures, summary, out_path, failures_path) -> None:
    print("\n" + "=" * 64)
    print(f"TEND solve · run {rt.settings.run_id} · "
          f"{'STUB' if rt.settings.stub else 'LIVE ' + rt.settings.llm.model}")
    print(f"  predictions : {len(predictions)}")
    for pred in predictions[:5]:
        print(f"    #{pred.get('record_id')} {pred.get('db_id')} "
              f"mql={str(pred.get('MQL', ''))[:96]}")
    print(f"  failures    : {len(failures)}")
    for item in failures[:5]:
        print(f"    #{item.get('record_id')} {item.get('db_id')} "
              f"{item.get('error_code')}: {str(item.get('message', ''))[:96]}")
    print(f"  anomalies : {summary.get('anomaly_total', 0)} {summary.get('anomalies_by_kind', {})}")
    print(f"  logs   : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl")
    print(f"  output : {out_path}")
    if failures:
        print(f"  failures output : {failures_path}")
    print("=" * 64)


def _print_baseline_summary(rt, predictions, failures, summary, out_path, failures_path) -> None:
    print("\n" + "=" * 64)
    print(f"TEND baselines · run {rt.settings.run_id} · "
          f"{'STUB' if rt.settings.stub else 'LIVE ' + rt.settings.llm.model}")
    print(f"  predictions : {len(predictions)}")
    by_baseline: dict[str, int] = {}
    for item in predictions:
        bid = str(item.get("baseline_id"))
        by_baseline[bid] = by_baseline.get(bid, 0) + 1
    print(f"  baselines : {by_baseline}")
    print(f"  failures  : {len(failures)}")
    for item in predictions[:5]:
        print(f"    {item.get('baseline_id')} #{item.get('record_id')} "
              f"{item.get('db_id')} status={item.get('status')} "
              f"mql={str(item.get('MQL', ''))[:80]}")
    for item in failures[:5]:
        print(f"    failure {item.get('baseline_id')} #{item.get('record_id')} "
              f"{item.get('error_code')}: {str(item.get('message', ''))[:80]}")
    print(f"  anomalies : {summary.get('anomaly_total', 0)} {summary.get('anomalies_by_kind', {})}")
    print(f"  logs   : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl")
    print(f"  output : {out_path}")
    if failures:
        print(f"  failures output : {failures_path}")
    print("=" * 64)


def _resolve_repo_path(settings: Settings, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else settings.paths.repo_root / p


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
        print(f"  records : {report.n_records}")
        print(f"  coverage: dbs={len(c.db_ids)} L4={c.l4_ratio:.0%} "
              f"L0={c.l0_ratio:.0%} flex={c.flex_ratio:.0%} ssf={c.ssf_ratio:.0%}")
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
    c.add_argument("--records", type=int, default=1, help="Phase B records to attempt")
    c.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    c.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    c.add_argument("--run-id", default=None)

    v = sub.add_parser("validate", help="validate a dataset directory")
    v.add_argument("--dataset-dir", required=True, help="dataset dir to validate")
    v.add_argument("--smoke", action="store_true",
                   help="smoke validation: relax all-DB composition only")

    p = sub.add_parser("publish", help="validate and copy a production release")
    p.add_argument("--dataset-dir", required=True, help="candidate dataset dir")
    p.add_argument("--out", default=str(PRODUCTION_RELEASE_DIR),
                   help="production release dir (default: release/TEND-dataset)")

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

    b = sub.add_parser("baseline", help="run constrained LLM baselines")
    b.add_argument("--dataset-dir", default=None,
                   help="release dataset dir (default: release/TEND-dataset)")
    b.add_argument("--baselines", default="all",
                   help=f"comma-separated baseline ids or all; known={','.join(BASELINE_IDS)}")
    b.add_argument("--db-id", default=None, help="optional db_id filter")
    b.add_argument("--record-id", type=int, default=None, help="optional record_id filter")
    b.add_argument("--limit", type=int, default=1, help="max records per baseline")
    b.add_argument("--witness-k", type=int, default=DEFAULT_WITNESS_K,
                   help="public witness sample count for baselines that use samples")
    b.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    b.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    b.add_argument("--run-id", default=None)

    args = parser.parse_args(argv)

    overrides = {}
    if getattr(args, "stub", False):
        overrides["TEND_LLM_STUB"] = "1"
    if getattr(args, "quiet", False):
        overrides["TEND_QUIET"] = "1"
    run_id = getattr(args, "run_id", None) or new_run_id()
    settings = Settings.from_env(
        run_id=run_id,
        overrides=overrides,
        require_bird=args.command == "construct",
        require_llm=args.command in {"construct", "solve", "baseline"},
    )

    if args.command == "validate":
        return _run_validate(
            settings,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
            smoke=args.smoke,
        )
    if args.command == "publish":
        return _run_publish(
            settings,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
            out_dir=_resolve_repo_path(settings, args.out),
        )
    if args.command == "construct":
        rt = build_runtime(settings)
        if rt.source is None:
            raise RuntimeError("construct requires a BIRD source")
        db_ids = list(rt.source.db_ids) if args.dbs == "all" else [
            db.strip() for db in args.dbs.split(",") if db.strip()
        ]
        return asyncio.run(_run_construct(rt, db_ids, args.phase, args.records))
    if args.command == "solve":
        rt = build_solver_runtime(settings)
        dataset_dir = _resolve_repo_path(
            settings,
            args.dataset_dir if args.dataset_dir else PRODUCTION_RELEASE_DIR,
        )
        return asyncio.run(_run_solve(
            rt,
            dataset_dir=dataset_dir,
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            r_max=args.r_max,
            witness_k=args.witness_k,
        ))
    if args.command == "baseline":
        rt = build_solver_runtime(settings, run_kind="baseline")
        dataset_dir = _resolve_repo_path(
            settings,
            args.dataset_dir if args.dataset_dir else PRODUCTION_RELEASE_DIR,
        )
        return asyncio.run(_run_baseline(
            rt,
            dataset_dir=dataset_dir,
            baselines=args.baselines,
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            witness_k=args.witness_k,
        ))
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
