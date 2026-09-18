"""M3 counterfactual-witness flip-rate runner (metric validation).

For a system's headline-passing predictions, inject gold-invariant distractors into the
witness and re-score: a prediction that agreed with the gold on D but diverges on D' was a
single-witness coincidence. Reports per-system flip rate (diagnostic only; never the
headline). Reuses ``tend.evaluation.counterfactual`` (the tested core) over the live
``MongoExecutor``.

Usage:
  python scripts/run_counterfactual.py \
      --dataset-dir release/tend-native-mongodb-v1 \
      --predictions runs/<id>/ablation/ablation_predictions.jsonl \
      --system sag_full --db financial --out M3_flip_financial.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tend.config import Settings
from tend.evaluation.counterfactual import (
    FlipRateLedger,
    build_counterfactual_witness,
    counterfactual_flip,
)
from tend.execution.mongo import MongoExecutor
from tend.observability import setup_logging
from tend.release_layout import resolve_release_dataset_layout

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO / "release" / "tend-native-mongodb-v1"


def _system_id(row: dict[str, Any]) -> str:
    return str(row.get("ablation_id") or row.get("baseline_id") or row.get("solver_variant") or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="M3 counterfactual-witness flip rate.")
    ap.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--predictions", required=True, type=Path,
                    help="a *_predictions.jsonl from solve/ablation/baseline")
    ap.add_argument("--system", default=None, help="system_id filter (e.g. sag_full)")
    ap.add_argument("--db", default=None, help="db_id filter")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--per-collection", type=int, default=4,
                    help="candidate distractors per collection")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    layout = resolve_release_dataset_layout(args.dataset_dir)
    records = {
        (str(r.get("db_id")), r.get("record_id")): r
        for r in json.loads(layout.test_path.read_text(encoding="utf-8"))
    }

    preds = [
        json.loads(line)
        for line in args.predictions.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    preds = [p for p in preds if str(p.get("MQL") or "").strip()]
    if args.system:
        preds = [p for p in preds if _system_id(p) == args.system]
    if args.db:
        preds = [p for p in preds if str(p.get("db_id")) == args.db]
    if args.limit is not None:
        preds = preds[: args.limit]

    settings = Settings.from_env(run_id="m3-counterfactual", require_bird=False)
    log = setup_logging(settings.run_dir)
    executor = MongoExecutor(settings, log)

    # Witness per db (the on-disk release witness, the same D the headline scored on).
    witness_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def _witness(db_id: str) -> dict[str, list[dict[str, Any]]]:
        if db_id not in witness_cache:
            data_path = layout.mongodb_data_dir / f"{db_id}.json"
            witness_cache[db_id] = json.loads(data_path.read_text(encoding="utf-8"))
        return witness_cache[db_id]

    ledgers: dict[str, FlipRateLedger] = {}
    for p in preds:
        sid = _system_id(p)
        db_id = str(p.get("db_id"))
        record_id = p.get("record_id")
        gold = records.get((db_id, record_id))
        if gold is None:
            continue
        gold_mql = str(gold.get("MQL") or "")
        pred_mql = str(p.get("MQL") or "")
        cf = build_counterfactual_witness(
            executor, db_id, _witness(db_id), gold_mql, per_collection=args.per_collection
        )
        verdict = counterfactual_flip(executor, db_id, cf, pred_mql, gold_mql)
        ledgers.setdefault(sid, FlipRateLedger(system_id=sid)).add(db_id, record_id, verdict)

    report = {
        "leg": "M3_counterfactual_flip",
        "predictions_file": str(args.predictions),
        "db_filter": args.db,
        "per_collection": args.per_collection,
        "systems": [ledger.summary() for ledger in ledgers.values()],
        "per_record": {sid: ledger.per_record for sid, ledger in ledgers.items()},
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    for ledger in ledgers.values():
        s = ledger.summary()
        print(
            f"{s['system_id']}: flip_rate={s['flip_rate']} "
            f"({s['flipped']}/{s['applicable']} applicable; "
            f"{s['skipped_no_distractor']} no-distractor, {s['skipped_not_passed']} not-passed)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
