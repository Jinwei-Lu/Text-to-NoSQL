"""Assert H1-H9 hard constraints on a published TEND tree."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tend.orchestrate.coverage import CoverageController
from tend.orchestrate.paths import coverage_report_path, tend_json, test_json, train_json
from tend.orchestrate.publish import check_h1_h9


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert H1-H9 on published TEND output.")
    parser.add_argument("out_root", help="Published TEND root (e.g. out/TEND)")
    args = parser.parse_args(argv)

    out_root = Path(args.out_root)
    train = json.loads(train_json(out_root).read_text(encoding="utf-8"))
    test = json.loads(test_json(out_root).read_text(encoding="utf-8"))
    tend = json.loads(tend_json(out_root).read_text(encoding="utf-8"))

    split_meta: dict = {}
    report_path = coverage_report_path(out_root)
    if report_path.exists():
        split_meta = json.loads(report_path.read_text(encoding="utf-8")).get("split", {})

    coverage = CoverageController.with_defaults(target_records=len(tend))
    for record in tend:
        coverage.accept(record)

    errors = check_h1_h9(train, test, tend, out_root, split_meta, coverage)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    print(f"H1-H9 OK ({len(train)} train / {len(test)} test / {len(tend)} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
