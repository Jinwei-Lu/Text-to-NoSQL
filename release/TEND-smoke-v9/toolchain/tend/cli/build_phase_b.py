"""CLI wrapper for Phase B build."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tend.orchestrate.pipeline import build_phase_b


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Phase B record for one db_id.")
    parser.add_argument("--db", required=True, help="Spider db_id")
    parser.add_argument("--record", type=int, default=1001, help="Record id")
    parser.add_argument("--out", default="out/TEND", help="Output TEND root")
    args = parser.parse_args(argv)

    try:
        result = build_phase_b(args.db, Path(args.out), record_id=args.record)
        print(f"Phase B complete for {args.db}/{args.record}: {result.get('status', 'ok')}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"build_phase_b failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
