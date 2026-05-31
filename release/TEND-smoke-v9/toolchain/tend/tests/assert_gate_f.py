"""Assert Gate-F (Full-C) acceptance criteria."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tend.config import assert_gate_f_no_stubs
from tend.evaluate.disclosure import check_disclosure_artifacts, disclosure_complete
from tend.evaluate.leaderboard import validate_leaderboard_payload
from tend.orchestrate.coverage import CoverageController
from tend.orchestrate.paths import coverage_report_path, tend_json, test_json, train_json
from tend.orchestrate.publish import check_c1_c9
from tend.orchestrate.publish import check_h1_h9


def assert_gate_f(
    out_root: Path,
    eval_dir: Path | None = None,
    *,
    min_selected_dbs: int = 140,
    min_records: int = 200,
) -> list[str]:
    out_root = Path(out_root)
    errors: list[str] = []

    meta_path = out_root / "_meta.json"
    if not meta_path.exists():
        errors.append("missing out/TEND/full/_meta.json")
        return errors

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    try:
        assert_gate_f_no_stubs(panel_stub=bool(meta.get("panel_stub", True)), llm_stub=meta.get("llm_stub"))
    except RuntimeError as exc:
        errors.append(str(exc))

    if meta.get("stage") != "full-c":
        errors.append(f"expected _meta.stage=full-c, got {meta.get('stage')}")

    catalog_path = out_root / "spider_db_catalog.json"
    if not catalog_path.exists():
        errors.append("missing spider_db_catalog.json")
    else:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        selected = sum(1 for d in catalog.get("databases", []) if d.get("selected"))
        if selected < min_selected_dbs:
            errors.append(f"Gate-F requires ≥{min_selected_dbs} selected dbs, got {selected}")

    train = json.loads(train_json(out_root).read_text(encoding="utf-8"))
    test = json.loads(test_json(out_root).read_text(encoding="utf-8"))
    tend = json.loads(tend_json(out_root).read_text(encoding="utf-8"))

    if len(tend) < min_records:
        errors.append(f"Gate-F requires ≥{min_records} records in dev sweep, got {len(tend)}")

    errors.extend(check_c1_c9(tend, out_root))

    split_meta: dict = {}
    report_path = coverage_report_path(out_root)
    supply_relax = False
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        split_meta = report.get("split", {})
        supply_relax = bool(report.get("supply_relax_active", False))

    coverage = CoverageController.with_defaults(target_records=len(tend))
    for record in tend:
        coverage.accept(record)
    errors.extend(check_h1_h9(train, test, tend, out_root, split_meta, coverage))

    eval_dir = eval_dir or Path("out/eval/full")
    lb_path = eval_dir / "leaderboard.json"
    if lb_path.exists():
        payload = json.loads(lb_path.read_text(encoding="utf-8"))
        try:
            validate_leaderboard_payload(payload)
        except ValueError as exc:
            errors.append(f"leaderboard schema invalid: {exc}")
        checks = check_disclosure_artifacts(
            eval_dir,
            leaderboard=payload,
            panel_stub=bool(meta.get("panel_stub", True)),
        )
        if not disclosure_complete(checks, require_panel=True):
            missing = [c.key for c in checks if not c.present]
            errors.append(f"disclosure incomplete: {', '.join(missing)}")
    else:
        errors.append(f"missing evaluation artifacts under {eval_dir} (run evaluate + build_panel_pr --full)")

    if supply_relax:
        flex_path = Path("out/audit/_global/flex_supply_report.json")
        if not flex_path.exists():
            errors.append("supply_relax active but flex_supply_report.json missing")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert Gate-F on a Full-C publish tree.")
    parser.add_argument("out_root", help="Published Full-C root (e.g. out/TEND/full)")
    parser.add_argument("--eval-dir", default="out/eval/full")
    parser.add_argument("--min-selected-dbs", type=int, default=140)
    args = parser.parse_args(argv)

    errors = assert_gate_f(
        Path(args.out_root),
        Path(args.eval_dir),
        min_selected_dbs=args.min_selected_dbs,
    )
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    print(f"Gate-F OK: {args.out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
