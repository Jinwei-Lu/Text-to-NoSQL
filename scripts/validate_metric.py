"""Metric-validation harness CLI: M1 / M2.

Thin runner over ``tend.evaluation.metric_validation`` (the testable core) wired to the
live ``MongoExecutor`` and a release dataset. Two subcommands:

  m1     gold self-scoring — every gold pipeline must earn EXC=1 against itself; any
         failure is a benchmark defect (unparseable gold, banned operator, empty result),
         and MUST be triaged before any leaderboard run.
  m2     null/shortcut probes × β sweep — confirms the frozen EXC surplus bound (β=2) is
         the smallest β that accepts the benign-surplus families and rejects every
         excess/null probe, across the dataset.

Heavy (executes golds against the MongoDB at TEND_MONGO_URI) but uses NO LLM; honors
TEND_USE_EXISTING_MONGO_DBS=1.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tend.config import Settings
from tend.evaluation.metric_validation import (
    gold_self_scores,
    select_min_beta,
    sweep_record,
)
from tend.evaluation.metrics import EXC_SURPLUS_BOUND
from tend.execution.ast_check import parse_pipeline, scan_disabled
from tend.execution.mongo import MongoExecutor
from tend.observability import setup_logging
from tend.release_layout import resolve_release_dataset_layout

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO / "release" / "tend-native-mongodb-v1"
_ORDER_OPS = {"$sort", "$limit", "$skip", "$setWindowFields"}


def _order_sensitive(mql: str) -> bool:
    try:
        _coll, pipeline = parse_pipeline(mql)
    except Exception:  # noqa: BLE001 - malformed gold handled by the M1 leg
        return False
    return any(
        isinstance(stage, dict) and any(op in _ORDER_OPS for op in stage) for stage in pipeline
    )


def _load_records(dataset_dir: Path, db: str | None, limit: int | None) -> list[dict[str, Any]]:
    layout = resolve_release_dataset_layout(dataset_dir)
    records = json.loads(layout.test_path.read_text(encoding="utf-8"))
    if db:
        records = [r for r in records if str(r.get("db_id") or "") == db]
    if limit is not None:
        records = records[:limit]
    return records


def _load_witnesses(dataset_dir: Path, executor: MongoExecutor, db_ids: set[str]) -> None:
    layout = resolve_release_dataset_layout(dataset_dir)
    for db_id in sorted(db_ids):
        data_path = layout.mongodb_data_dir / f"{db_id}.json"
        data = json.loads(data_path.read_text(encoding="utf-8"))
        executor.load_witness(db_id, data)


def _executor(dataset_dir: Path, records: list[dict[str, Any]]) -> MongoExecutor:
    settings = Settings.from_env(run_id="metric-validate", require_bird=False)
    log = setup_logging(settings.run_dir)
    executor = MongoExecutor(settings, log)
    _load_witnesses(dataset_dir, executor, {str(r.get("db_id") or "") for r in records})
    return executor


def _gold_result(executor: MongoExecutor, record: dict[str, Any]) -> list[dict[str, Any]] | None:
    mql = str(record.get("MQL") or "")
    if scan_disabled(mql):
        return None
    try:
        return executor.norm_exec(str(record.get("db_id") or ""), mql)
    except Exception:  # noqa: BLE001 - an unexecutable gold fails M1 honestly
        return None


def _run_m1(args: argparse.Namespace) -> int:
    records = _load_records(args.dataset_dir, args.db, args.limit)
    executor = _executor(args.dataset_dir, records)
    failures: list[dict[str, Any]] = []
    for r in records:
        rows = _gold_result(executor, r)
        ident = {"db_id": r.get("db_id"), "record_id": r.get("record_id")}
        if rows is None:
            failures.append({**ident, "reason": "gold_unexecutable_or_banned"})
            continue
        if not rows:
            failures.append({**ident, "reason": "gold_empty_result"})
            continue
        if not gold_self_scores(rows, order_sensitive=_order_sensitive(str(r.get("MQL")))):
            failures.append({**ident, "reason": "gold_does_not_self_score"})
    report = {
        "leg": "M1_gold_self_score",
        "beta": EXC_SURPLUS_BOUND,
        "records": len(records),
        "passed": len(records) - len(failures),
        "failures": failures,
    }
    _emit(report, args.out)
    print(f"M1: {report['passed']}/{report['records']} golds self-score at β={EXC_SURPLUS_BOUND}; "
          f"{len(failures)} defect(s)")
    return 0 if not failures else 1


def _run_m2(args: argparse.Namespace) -> int:
    records = _load_records(args.dataset_dir, args.db, args.limit)
    executor = _executor(args.dataset_dir, records)
    sweep: list[Any] = []
    skipped = 0
    for r in records:
        rows = _gold_result(executor, r)
        if not rows:  # M1 already flags these; the β sweep needs a non-empty gold
            skipped += 1
            continue
        sweep.append(
            sweep_record(
                rows,
                order_sensitive=_order_sensitive(str(r.get("MQL"))),
                db_id=str(r.get("db_id") or ""),
                record_id=r.get("record_id"),
            )
        )
    decision = select_min_beta(sweep)
    report = {
        "leg": "M2_beta_sweep",
        "records_swept": len(sweep),
        "records_skipped_empty_gold": skipped,
        "decision": decision,
    }
    _emit(report, args.out)
    print(
        f"M2: chosen β={decision['chosen_beta']} (frozen={decision['frozen_bound']}, "
        f"match={decision['matches_frozen_bound']}); per-β correct={decision['per_beta_correct']}"
    )
    return 0 if decision["matches_frozen_bound"] else 1


def _emit(report: dict[str, Any], out: Path | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)


def main() -> int:
    ap = argparse.ArgumentParser(description="TEND metric-validation harness (M1/M2).")
    ap.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--db", default=None, help="optional db_id filter")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    sub = ap.add_subparsers(dest="leg", required=True)
    sub.add_parser("m1", help="gold self-scoring (defect gate)")
    sub.add_parser("m2", help="null/shortcut probe β sweep (bound freeze)")
    args = ap.parse_args()
    if args.leg == "m1":
        return _run_m1(args)
    if args.leg == "m2":
        return _run_m2(args)
    ap.error("unknown leg")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
