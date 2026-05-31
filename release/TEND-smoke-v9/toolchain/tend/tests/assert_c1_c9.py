"""Assert C1-C9 record constraints on a published TEND tree."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tend.orchestrate.paths import tend_json
from tend.orchestrate.publish import check_c1_c9


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert C1-C9 on published TEND output.")
    parser.add_argument("out_root", help="Published TEND root (e.g. out/TEND)")
    args = parser.parse_args(argv)

    out_root = Path(args.out_root)
    records = json.loads(tend_json(out_root).read_text(encoding="utf-8"))
    errors = check_c1_c9(records, out_root)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    print(f"C1-C9 OK ({len(records)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
