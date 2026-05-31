"""Production full dataset build: Phase A+B LLM pipeline + publish."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from tend.config import REPO_ROOT, assert_pilot_llm_live
from tend.core import logging as log_module
from tend.orchestrate.paths import DEFAULT_OUT_ROOT
from tend.phase_a.catalog import select_spider_dbs


def run_build(
    out_root: Path,
    *,
    max_selected: int = 200,
    target_records: int = 17000,
    test_ratio: float = 0.20,
    skip_llm_check: bool = False,
    workers: int | None = None,
    release_tag: str = "full",
    with_evaluate: bool = True,
) -> dict:
    """Run scaled Phase A+B build via run_smoke (no fixture expand_records)."""
    if not skip_llm_check and os.getenv("TEND_FULL_ALLOW_STUB") != "1":
        assert_pilot_llm_live()

    if workers is None:
        workers = max(1, int(os.getenv("TEND_LLM_WORKERS", "128")))
    os.environ.setdefault("TEND_LLM_WORKERS", str(workers))

    run_dir = log_module.init_run_dir()
    log_module.configure_logging(quiet=os.getenv("TEND_QUIET") == "1")
    log_module.emit(
        "pipeline.start",
        stage="full-build",
        max_selected=max_selected,
        target_records=target_records,
        workers=workers,
    )

    catalog_result = select_spider_dbs(auto_select_qualifying=True, max_selected=max_selected)
    catalog = catalog_result["catalog"]
    warnings_path = REPO_ROOT / "out" / "audit" / "_global" / "domain_map_warnings.json"
    warnings_path.parent.mkdir(parents=True, exist_ok=True)
    warnings_path.write_text(
        json.dumps(catalog_result["domain_map_warnings"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    flex_report_path = REPO_ROOT / "out" / "audit" / "_global" / "flex_supply_report.json"
    flex_report_path.write_text(
        json.dumps(
            {
                "selected_flex_ratio": catalog.get("selected_flex_ratio"),
                "flex_supply_warning": catalog.get("flex_supply_warning"),
                "selected_count": sum(1 for d in catalog["databases"] if d.get("selected")),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    db_ids = sorted({str(d["db_id"]) for d in catalog["databases"] if d.get("selected")})
    if not db_ids:
        raise RuntimeError("No qualifying databases selected from Spider catalog")

    records_per_db = max(1, math.ceil(target_records / len(db_ids)))

    from tend.cli.run_smoke import run_smoke

    smoke_result = run_smoke(
        out_root,
        records_per_db=records_per_db,
        db_ids=db_ids,
        test_ratio=test_ratio,
        skip_llm_check=skip_llm_check,
        extra_db_count=0,
        llm_stub=False,
        skip_publish=False,
        with_evaluate=with_evaluate,
        with_disclosure=False,
        release_tag=release_tag,
        workers=workers,
    )

    meta = {
        "stage": "full-build",
        "llm_stub": False,
        "use_fixtures": False,
        "panel_stub": True,
        "record_count": len(smoke_result.get("records", [])),
        "selected_db_count": len(db_ids),
        "records_per_db": records_per_db,
        "publish_error": smoke_result.get("publish_error"),
        "run_dir": str(run_dir),
        "workers": workers,
    }
    out_root = Path(out_root)
    (out_root / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    log_module.emit(
        "pipeline.done",
        stage="full-build",
        records=meta["record_count"],
        publish_error=meta["publish_error"],
    )
    return {
        "smoke": smoke_result,
        "catalog": catalog,
        "meta": meta,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Production full dataset build (Phase A+B LLM).")
    parser.add_argument("--out", default=str(DEFAULT_OUT_ROOT / "full"))
    parser.add_argument("--max-selected", type=int, default=200)
    parser.add_argument("--target-records", type=int, default=17000)
    parser.add_argument("--test-ratio", type=float, default=0.20)
    parser.add_argument("--release-tag", default="full")
    parser.add_argument(
        "--no-evaluate",
        dest="with_evaluate",
        action="store_false",
        default=True,
        help="Skip post-publish evaluate on test split.",
    )
    parser.add_argument(
        "--allow-stub",
        action="store_true",
        help="Skip real-LLM check (CI only)",
    )
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args(argv)

    if args.allow_stub:
        os.environ["TEND_FULL_ALLOW_STUB"] = "1"

    try:
        result = run_build(
            Path(args.out),
            max_selected=args.max_selected,
            target_records=args.target_records,
            test_ratio=args.test_ratio,
            skip_llm_check=args.allow_stub,
            workers=args.workers,
            release_tag=args.release_tag,
            with_evaluate=args.with_evaluate,
        )
        meta = result["meta"]
        print(
            f"Full build OK: {meta['record_count']} records, "
            f"{meta['selected_db_count']} dbs -> {args.out}"
        )
        return 0 if meta.get("publish_error") is None else 1
    except Exception as exc:  # noqa: BLE001
        print(f"run_build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
