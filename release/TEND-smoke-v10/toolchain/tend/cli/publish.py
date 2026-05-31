"""CLI: publish train/test/TEND.json from fixtures snapshot."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tend.core import logging as log_module
from tend.core import progress as progress_module
from tend.orchestrate.paths import DEFAULT_OUT_ROOT, resolve_input_root
from tend.orchestrate.publish import publish_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish TEND dataset from fixtures snapshot.")
    parser.add_argument(
        "--in",
        dest="input_root",
        default="fixtures-snapshot",
        help="Input snapshot directory (default: fixtures-snapshot)",
    )
    parser.add_argument(
        "--out",
        dest="out_root",
        default=str(DEFAULT_OUT_ROOT),
        help="Output TEND root (default: out/TEND)",
    )
    parser.add_argument("--test-ratio", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    run_dir = log_module.init_run_dir()
    log_module.configure_logging(quiet=os.getenv("TEND_QUIET") == "1")
    progress_module.init(run_dir=run_dir)
    progress_module.outer(1)
    task = progress_module.inner("publish", 1)

    try:
        result = publish_dataset(
            resolve_input_root(args.input_root),
            Path(args.out_root),
            test_ratio=args.test_ratio,
            seed=args.seed,
        )
        progress_module.advance(task)
        progress_module.advance(progress_module._outer_task)
        print(
            f"Published {len(result['TEND'])} records "
            f"(train={len(result['train'])}, test={len(result['test'])}) -> {args.out_root}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        log_module.emit("publish.fail", error=str(exc), level="ERROR")
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    finally:
        progress_module.close()


if __name__ == "__main__":
    raise SystemExit(main())
