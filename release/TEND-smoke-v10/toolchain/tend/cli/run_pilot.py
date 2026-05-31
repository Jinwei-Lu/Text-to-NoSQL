"""Pilot-B sweep: 6 fixtures, expanded records, real LLM gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tend.config import REPO_ROOT, assert_pilot_llm_live
from tend.core import logging as log_module
from tend.orchestrate.paths import DEFAULT_OUT_ROOT
from tend.orchestrate.publish import bootstrap_fixtures_snapshot, load_snapshot, publish_dataset
from tend.orchestrate.record_expand import expand_records


def run_pilot(
    out_root: Path,
    *,
    target_records: int = 210,
    test_ratio: float = 0.20,
    skip_llm_check: bool = False,
) -> dict:
    if not skip_llm_check and os.getenv("TEND_PILOT_ALLOW_STUB") != "1":
        assert_pilot_llm_live()

    run_dir = log_module.init_run_dir()
    log_module.configure_logging(quiet=os.getenv("TEND_QUIET") == "1")
    log_module.emit("pipeline.start", stage="pilot", target_records=target_records)

    snapshot_dir = REPO_ROOT / "fixtures-snapshot"
    if not (snapshot_dir / "records.json").exists():
        bootstrap_fixtures_snapshot(snapshot_dir)

    records, catalog = load_snapshot(snapshot_dir)
    expanded = expand_records(records, target_total=target_records)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "records.pilot.json").write_text(
        json.dumps(expanded, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    pilot_snapshot = snapshot_dir / "pilot-publish"
    pilot_snapshot.mkdir(parents=True, exist_ok=True)
    (pilot_snapshot / "records.json").write_text(
        json.dumps(expanded, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (pilot_snapshot / "spider_db_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for sub in ("mongodb_schema", "mongodb_data", "agent_design_rationale", "fixtures"):
        src = snapshot_dir / sub
        dst = pilot_snapshot / sub
        if src.exists() and not dst.exists():
            import shutil

            shutil.copytree(src, dst)

    out_root = Path(out_root)
    result = publish_dataset(pilot_snapshot, out_root, test_ratio=test_ratio)

    meta = {
        "stage": "pilot-b",
        "llm_stub": False,
        "use_fixtures": False,
        "panel_stub": True,
        "record_count": len(result["TEND"]),
        "db_ids": sorted({r["db_id"] for r in result["TEND"]}),
        "run_dir": str(run_dir),
    }
    (out_root / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log_module.emit("pipeline.done", stage="pilot", records=len(result["TEND"]))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Pilot-B publish sweep (Gate-P input).")
    parser.add_argument("--out", default=str(DEFAULT_OUT_ROOT / "pilot"))
    parser.add_argument("--target-records", type=int, default=210)
    parser.add_argument("--test-ratio", type=float, default=0.20)
    parser.add_argument(
        "--allow-stub",
        action="store_true",
        help="Skip real-LLM check (sets TEND_PILOT_ALLOW_STUB=1 for CI only)",
    )
    args = parser.parse_args(argv)

    if args.allow_stub:
        os.environ["TEND_PILOT_ALLOW_STUB"] = "1"

    try:
        result = run_pilot(
            Path(args.out),
            target_records=args.target_records,
            test_ratio=args.test_ratio,
            skip_llm_check=args.allow_stub,
        )
        print(
            f"Pilot-B publish OK: {len(result['TEND'])} records "
            f"(train={len(result['train'])}, test={len(result['test'])}) -> {args.out}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"run_pilot failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
