"""CLI: scan Spider DBs for schema-flex (H1–H4) eligibility."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tend.phase_a.flex_scan import scan_spider_flex_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List Spider databases that cannot pass H1–H4 schema-flex pre-audit."
    )
    parser.add_argument(
        "--qualifying-only",
        action="store_true",
        help="Only dbs meeting catalog min_tables/min_queries/non-empty (default policy)",
    )
    parser.add_argument("--min-tables", type=int, default=2)
    parser.add_argument("--min-queries", type=int, default=10)
    parser.add_argument(
        "--list-not-flex",
        action="store_true",
        help="Print one db_id per line for non-flex-eligible databases",
    )
    parser.add_argument(
        "--report-out",
        default=None,
        help="Write full JSON report to this path",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Omit per-db details from stdout JSON",
    )
    force_group = parser.add_mutually_exclusive_group()
    force_group.add_argument(
        "--force-document-flex",
        action="store_true",
        default=None,
        help="Apply H0 build-policy flex fallback (default: TEND_FORCE_DOCUMENT_FLEX env, else on)",
    )
    force_group.add_argument(
        "--no-force-document-flex",
        action="store_true",
        help="Disable H0 build-policy flex fallback",
    )
    args = parser.parse_args(argv)

    force_override: bool | None = None
    if args.force_document_flex:
        force_override = True
    elif args.no_force_document_flex:
        force_override = False

    report = scan_spider_flex_report(
        min_tables=args.min_tables,
        min_queries=args.min_queries,
        qualifying_only=args.qualifying_only,
        force_document_flex_override=force_override,
    )

    if args.list_not_flex:
        for db_id in report["not_flex_eligible_dbs"]:
            print(db_id)
        return 0

    out = dict(report)
    if args.compact:
        out.pop("details", None)

    print(json.dumps(out, ensure_ascii=False, indent=2))

    if args.report_out:
        path = Path(args.report_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nFull report -> {path}", file=sys.stderr)

    subset = report["qualifying_subset"] if args.qualifying_only else report["all_scanned"]
    label = "qualifying dbs" if args.qualifying_only else "all discovered dbs"
    yes = subset["flex_eligible"]
    no = subset["not_flex_eligible"]
    ratio = subset["flex_eligible_ratio"]
    print(
        f"\nSummary ({label}): {yes} flex-eligible, {no} NOT flex-eligible "
        f"({ratio:.1%} eligible); natural={report.get('natural_flex_count', 0)}, "
        f"forced_h0={report.get('forced_h0_count', 0)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
