"""Assert Gate-P (Pilot-B) acceptance criteria."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tend.config import default_llm_stub, use_fixtures
from tend.orchestrate.coverage import CoverageController, SIX_AXES
from tend.orchestrate.paths import tend_json, test_json, train_json
from tend.orchestrate.publish import check_c1_c9
from tend.orchestrate.publish import check_h1_h9
from tend.orchestrate.paths import coverage_report_path


def assert_gate_p(out_root: Path, *, require_live_llm: bool = True) -> list[str]:
    out_root = Path(out_root)
    errors: list[str] = []

    meta_path = out_root / "_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("stage") != "pilot-b":
            errors.append(f"expected _meta.stage=pilot-b, got {meta.get('stage')}")
        if require_live_llm:
            if meta.get("llm_stub") is True:
                errors.append("Pilot-B _meta.llm_stub must be false")
            if meta.get("use_fixtures") is True:
                errors.append("Pilot-B _meta.use_fixtures must be false")
    elif require_live_llm:
        if default_llm_stub() or use_fixtures():
            errors.append("Pilot-B requires live LLM (llm_enabled and use_fixtures=false)")

    train = json.loads(train_json(out_root).read_text(encoding="utf-8"))
    test = json.loads(test_json(out_root).read_text(encoding="utf-8"))
    tend = json.loads(tend_json(out_root).read_text(encoding="utf-8"))

    if len(tend) < 200:
        errors.append(f"Gate-P requires ≥200 records, got {len(tend)}")

    c_errors = check_c1_c9(tend, out_root)
    errors.extend(c_errors)

    split_meta: dict = {}
    report_path = coverage_report_path(out_root)
    if report_path.exists():
        split_meta = json.loads(report_path.read_text(encoding="utf-8")).get("split", {})

    coverage = CoverageController.with_defaults(target_records=len(tend))
    for record in tend:
        coverage.accept(record)

    errors.extend(check_h1_h9(train, test, tend, out_root, split_meta, coverage))

    if len(test) < 5:
        errors.append(f"Gate-P requires ≥5 test records, got {len(test)}")

    test_domains = {r.get("domain_id") for r in test}
    if len(test_domains) < 2:
        errors.append(f"Gate-P requires ≥2 test domains, got {sorted(test_domains)}")

    catalog_path = out_root / "spider_db_catalog.json"
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        flex_dbs = [d for d in catalog.get("databases", []) if d.get("flex_eligible")]
        if not flex_dbs:
            errors.append("Gate-P requires ≥1 flex_eligible db in catalog")
        selected_flex = [d for d in catalog.get("databases", []) if d.get("selected") and d.get("flex_eligible")]
        if not selected_flex:
            errors.append("Gate-P requires ≥1 selected flex_eligible db (H1/H4 supply)")

    nonzero_cells = 0
    for record in tend:
        for axis, key_fn in SIX_AXES.items():
            cell = (axis, key_fn(record))
            if coverage.count[cell] > 0:
                nonzero_cells += 1
    if nonzero_cells == 0:
        errors.append("Gate-P requires non-zero six-axis coverage matrix")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert Gate-P on a Pilot-B publish tree.")
    parser.add_argument("out_root", help="Published Pilot-B root (e.g. out/TEND/pilot)")
    parser.add_argument(
        "--allow-stub",
        action="store_true",
        help="Skip live-LLM meta checks (CI only)",
    )
    args = parser.parse_args(argv)

    errors = assert_gate_p(Path(args.out_root), require_live_llm=not args.allow_stub)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    print(f"Gate-P OK: {args.out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
