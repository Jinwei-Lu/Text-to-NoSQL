"""Import a TEND release into MongoDB and report schema-less shape heterogeneity."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tend.config import MONGO_URI
from tend.orchestrate.mongo_schemaless import import_release_to_mongo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import mongodb_data into MongoDB and measure document shape diversity."
    )
    parser.add_argument(
        "--release-root",
        default="out/TEND/smoke",
        help="Published release dir containing mongodb_data/",
    )
    parser.add_argument(
        "--mongo-uri",
        default=MONGO_URI,
        help="MongoDB URI (env TEND_MONGO_URI overrides tend.config default)",
    )
    parser.add_argument(
        "--database-prefix",
        default="tend_smoke",
        help="Each db_id loads into {prefix}_{db_id}",
    )
    parser.add_argument(
        "--no-drop",
        action="store_true",
        help="Do not drop existing target databases before import",
    )
    parser.add_argument(
        "--report-out",
        default=None,
        help="Optional JSON report path",
    )
    args = parser.parse_args(argv)

    release_root = Path(args.release_root)
    try:
        report = import_release_to_mongo(
            release_root,
            mongo_uri=args.mongo_uri,
            database_prefix=args.database_prefix,
            drop_existing=not args.no_drop,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}", file=sys.stderr)
        print(
            "Hint: start MongoDB (e.g. docker compose -f infra/docker-compose.yml up -d) "
            "and set TEND_MONGO_URI if auth is enabled.",
            file=sys.stderr,
        )
        return 1

    summary = report.summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.report_out:
        out_path = Path(args.report_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Report written -> {out_path}")

    ratio = summary.get("overall_heterogeneous_ratio", 0.0)
    multi = summary.get("collections_with_multiple_shapes", 0)
    print(
        f"\nSchema-less check: {multi} collection(s) with >1 document shape; "
        f"{ratio:.1%} of documents differ from the modal shape in their collection."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
