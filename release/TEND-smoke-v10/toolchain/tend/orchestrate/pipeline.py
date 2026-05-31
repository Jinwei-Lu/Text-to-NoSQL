"""End-to-end TEND pipeline driver with logging and progress hooks."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tend.core import logging as log_module
from tend.core import progress as progress_module
from tend.errors import RetryBudgetExhausted, TENDError
from tend.orchestrate.coverage import CoverageController
from tend.orchestrate.paths import tend_root
from tend.orchestrate.publish import publish_dataset
from tend.orchestrate.seed import db_seed, global_seed, record_seed


REFLOW_LIMITS = {
    "pv_ms": 2,
    "rtv_nlp": 2,
    "nnc_qps": 2,
    "ra_ms": 2,
}
AUGMENT_BUDGET_PER_DB = 1


@dataclass
class PipelineConfig:
    out_root: Path
    db_ids: list[str] = field(default_factory=list)
    records_per_db: int = 1
    test_ratio: float = 0.20
    workers: int = 1
    quiet: bool = False


@dataclass
class PipelineResult:
    published: dict[str, Any] | None = None
    phase_a: dict[str, Any] = field(default_factory=dict)
    phase_b: dict[str, Any] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)


def _import_phase_a_runner() -> Callable[..., dict[str, Any]] | None:
    try:
        from tend.phase_a.pipeline import run_phase_a  # type: ignore import-not-found

        return run_phase_a
    except Exception:
        return None


def _import_phase_b_runner() -> Callable[..., dict[str, Any]] | None:
    try:
        from tend.phase_b.pipeline import run_phase_b  # type: ignore import-not-found

        return run_phase_b
    except Exception:
        return None


def run_phase_a_for_db(db_id: str, out_root: Path, *, seed: int | None = None) -> dict[str, Any]:
    from tend.cli.build_phase_a import build_phase_a

    seed = seed if seed is not None else db_seed(db_id, "phase_a")
    log_module.bind(db_id=db_id, stage="phase_a")
    log_module.emit("phase_a.start", seed=seed)
    paths = build_phase_a(db_id, out_root, seed=seed)
    log_module.emit("phase_a.done", status="ok")
    return {"db_id": db_id, "seed": seed, "status": "ok", "paths": {k: str(v) for k, v in paths.items()}}


def run_phase_b_for_record(
    db_id: str,
    record_id: int | str,
    out_root: Path,
    coverage: CoverageController,
    *,
    seed: int | None = None,
) -> dict[str, Any]:
    from tend.cli.build_phase_b_synth import run_phase_b_synth
    from tend.cli.build_phase_b_valid import run_phase_b_valid

    seed = seed if seed is not None else record_seed(db_id, record_id, "phase_b.synth")
    log_module.bind(db_id=db_id, record_id=record_id, stage="phase_b")
    log_module.emit("record.start", seed=seed)

    synth = run_phase_b_synth(db_id, out_root=out_root, record_id=int(record_id))
    valid = run_phase_b_valid(db_id, int(record_id), out_root=out_root)
    status = "ok" if valid.get("status") == "ok" else "fail"
    log_module.emit("record.publish", status=status)
    return {
        "db_id": db_id,
        "record_id": record_id,
        "seed": seed,
        "status": status,
        "synth": synth,
        "valid": valid,
    }


def _run_db_job(
    db_id: str,
    out_root: Path,
    records_per_db: int,
    *,
    record_workers: int = 1,
) -> dict[str, Any]:
    phase_a = run_phase_a_for_db(db_id, out_root)
    coverage = CoverageController.with_defaults(target_records=records_per_db)
    records: list[dict[str, Any]] = []

    def _one_record(idx: int) -> dict[str, Any]:
        record_id = db_seed(db_id, "record") + idx
        progress_module.status(record_id, "pipeline", "phase_b", attempt=1)
        return run_phase_b_for_record(db_id, record_id, out_root, coverage)

    if record_workers <= 1:
        for idx in range(records_per_db):
            try:
                records.append(_one_record(idx))
            except RetryBudgetExhausted as exc:
                log_module.emit("record.reject", reason=str(exc), level="ERROR")
    else:
        with ThreadPoolExecutor(max_workers=record_workers) as pool:
            futures = {pool.submit(_one_record, idx): idx for idx in range(records_per_db)}
            for future in as_completed(futures):
                try:
                    records.append(future.result())
                except RetryBudgetExhausted as exc:
                    log_module.emit("record.reject", reason=str(exc), level="ERROR")

    return {"db_id": db_id, "phase_a": phase_a, "records": records}


def run_pipeline(
    config: PipelineConfig,
    *,
    input_root: Path | str | None = None,
    publish: bool = True,
) -> PipelineResult:
    run_dir = log_module.init_run_dir()
    log_module.configure_logging(quiet=config.quiet or os.getenv("TEND_QUIET") == "1")
    progress_module.init(run_dir=run_dir)

    out_root = tend_root(config.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    db_ids = config.db_ids or ["orchestra"]
    workers = max(1, config.workers)
    log_module.emit("pipeline.start", db_count=len(db_ids), seed=global_seed(), workers=workers)
    progress_module.outer(len(db_ids))

    phase_results: dict[str, Any] = {}
    if workers > 1 and len(db_ids) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _run_db_job,
                    db_id,
                    out_root,
                    config.records_per_db,
                    record_workers=workers,
                ): db_id
                for db_id in db_ids
            }
            for future in as_completed(futures):
                db_id = futures[future]
                inner_task = progress_module.inner(db_id, config.records_per_db)
                result = future.result()
                phase_results[db_id] = result
                progress_module.advance(inner_task, config.records_per_db)
                progress_module.advance(progress_module._outer_task)
    else:
        for db_id in db_ids:
            inner_task = progress_module.inner(db_id, config.records_per_db)
            phase_results[db_id] = _run_db_job(
                db_id,
                out_root,
                config.records_per_db,
                record_workers=workers,
            )
            progress_module.advance(inner_task, config.records_per_db)
            progress_module.advance(progress_module._outer_task)

    published = None
    if publish:
        if input_root is None:
            raise TENDError("publish=True requires input_root for MVP snapshot publish")
        published = publish_dataset(
            input_root,
            out_root,
            test_ratio=config.test_ratio,
        )

    log_module.emit("pipeline.done", published=publish is not None)
    progress_module.close()
    return PipelineResult(published=published, phase_a=phase_results)


def build_phase_a(db_id: str, out_root: Path | str) -> dict[str, Any]:
    run_dir = log_module.init_run_dir()
    log_module.configure_logging(quiet=os.getenv("TEND_QUIET") == "1")
    progress_module.init(run_dir=run_dir)
    progress_module.outer(1)
    task = progress_module.inner(db_id, 1)
    result = run_phase_a_for_db(db_id, tend_root(out_root))
    progress_module.advance(task)
    progress_module.advance(progress_module._outer_task)
    progress_module.close()
    return result


def build_phase_b(db_id: str, out_root: Path | str, *, record_id: int = 1001) -> dict[str, Any]:
    run_dir = log_module.init_run_dir()
    log_module.configure_logging(quiet=os.getenv("TEND_QUIET") == "1")
    progress_module.init(run_dir=run_dir)
    progress_module.outer(1)
    task = progress_module.inner(db_id, 1)
    coverage = CoverageController.with_defaults()
    cell = coverage.pick_next_cell()
    if cell:
        log_module.emit("coverage.cell.pick", axis=cell[0], value=cell[1])
    result = run_phase_b_for_record(db_id, record_id, tend_root(out_root), coverage)
    progress_module.advance(task)
    progress_module.advance(progress_module._outer_task)
    progress_module.close()
    return result
