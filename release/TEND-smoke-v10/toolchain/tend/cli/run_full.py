"""Legacy catalog+publish stub (fixture expand). Use run_build for production."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tend.config import REPO_ROOT, assert_pilot_llm_live
from tend.core import logging as log_module
from tend.orchestrate.paths import DEFAULT_OUT_ROOT, spider_db_catalog_json
from tend.orchestrate.publish import bootstrap_fixtures_snapshot, load_snapshot, publish_dataset
from tend.orchestrate.record_expand import expand_records
from tend.phase_a.catalog import select_spider_dbs


def run_full_catalog_publish_stub(
    out_root: Path,
    *,
    max_selected: int = 200,
    target_records: int = 200,
    test_ratio: float = 0.20,
    skip_llm_check: bool = False,
    workers: int | None = None,
) -> dict:
    """Fixture clone + publish only — NOT a real LLM build."""
    if not skip_llm_check and os.getenv("TEND_FULL_ALLOW_STUB") != "1":
        assert_pilot_llm_live()

    if workers is None:
        workers = max(1, int(os.getenv("TEND_LLM_WORKERS", "128")))
    os.environ.setdefault("TEND_LLM_WORKERS", str(workers))

    run_dir = log_module.init_run_dir()
    log_module.configure_logging(quiet=os.getenv("TEND_QUIET") == "1")
    log_module.emit("pipeline.start", stage="full-c-stub", max_selected=max_selected, workers=workers)

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

    snapshot_dir = REPO_ROOT / "fixtures-snapshot"
    if not (snapshot_dir / "records.json").exists():
        bootstrap_fixtures_snapshot(snapshot_dir)

    records, _fixture_catalog = load_snapshot(snapshot_dir)
    expanded = expand_records(records, target_total=max(target_records, 200))

    full_snapshot = snapshot_dir / "full-publish"
    full_snapshot.mkdir(parents=True, exist_ok=True)
    (full_snapshot / "records.json").write_text(
        json.dumps(expanded, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (full_snapshot / "spider_db_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for sub in ("mongodb_schema", "mongodb_data", "agent_design_rationale", "fixtures"):
        src = snapshot_dir / sub
        dst = full_snapshot / sub
        if src.exists() and not dst.exists():
            import shutil

            shutil.copytree(src, dst)

    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    spider_db_catalog_json(out_root).write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    result = publish_dataset(full_snapshot, out_root, test_ratio=test_ratio)

    meta = {
        "stage": "full-c-stub",
        "llm_stub": False,
        "use_fixtures": True,
        "panel_stub": True,
        "record_count": len(result["TEND"]),
        "selected_db_count": sum(1 for d in catalog["databases"] if d.get("selected")),
        "supply_relax_active": result.get("split_meta", {}).get("supply_relax_active", False),
        "run_dir": str(run_dir),
        "workers": workers,
        "note": "fixture expand only; use tend.cli.run_build for production LLM build",
    }
    (out_root / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log_module.emit("pipeline.done", stage="full-c-stub", records=len(result["TEND"]))
    return {"publish": result, "catalog": catalog, "meta": meta}


def run_full(
    out_root: Path,
    *,
    max_selected: int = 200,
    target_records: int = 200,
    test_ratio: float = 0.20,
    skip_llm_check: bool = False,
    workers: int | None = None,
    use_stub_expand: bool = False,
) -> dict:
    """Default: production LLM build via run_build. Pass use_stub_expand=True for legacy stub."""
    if use_stub_expand or os.getenv("TEND_FULL_STUB_EXPAND") == "1":
        return run_full_catalog_publish_stub(
            out_root,
            max_selected=max_selected,
            target_records=target_records,
            test_ratio=test_ratio,
            skip_llm_check=skip_llm_check,
            workers=workers,
        )
    from tend.cli.run_build import run_build

    return run_build(
        out_root,
        max_selected=max_selected,
        target_records=target_records,
        test_ratio=test_ratio,
        skip_llm_check=skip_llm_check,
        workers=workers,
        release_tag="full",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Full dataset entrypoint (default: production LLM build via run_build)."
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT_ROOT / "full"))
    parser.add_argument("--max-selected", type=int, default=200)
    parser.add_argument("--target-records", type=int, default=17000)
    parser.add_argument("--test-ratio", type=float, default=0.20)
    parser.add_argument(
        "--stub-expand",
        action="store_true",
        help="Legacy fixture expand + publish only (no LLM Phase A+B)",
    )
    parser.add_argument(
        "--allow-stub",
        action="store_true",
        help="Skip real-LLM check (CI only)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="LLM worker budget (default 128).",
    )
    args = parser.parse_args(argv)

    if args.allow_stub:
        os.environ["TEND_FULL_ALLOW_STUB"] = "1"

    try:
        result = run_full(
            Path(args.out),
            max_selected=args.max_selected,
            target_records=args.target_records,
            test_ratio=args.test_ratio,
            skip_llm_check=args.allow_stub,
            workers=args.workers,
            use_stub_expand=args.stub_expand,
        )
        meta = result.get("meta", {})
        count = meta.get("record_count") or len(result.get("publish", {}).get("TEND", []))
        print(f"run_full OK: {count} records -> {args.out}")
        return 0 if meta.get("publish_error") is None else 1
    except Exception as exc:  # noqa: BLE001
        print(f"run_full failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
