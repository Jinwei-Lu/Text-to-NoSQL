"""Verify LLM pool disjointness."""

from __future__ import annotations

import sys

from tend.config import load_pool_roster, pool_disjoint_strict
from tend.core import logging as log_module
from tend.evaluate.disjointness import collect_pool_assignments, verify_six_pool_disjoint


def main() -> int:
    log_module.configure_logging()
    roster = load_pool_roster()
    strict = pool_disjoint_strict()
    report = verify_six_pool_disjoint(roster, strict=strict)
    assignments = collect_pool_assignments(roster)
    if report.get("shared_model_mode"):
        log_module.emit(
            "pool.disjoint.skipped",
            shared_model_mode=True,
            unique_models=report["model_count"],
        )
        print(
            f"OK: shared-model mode — {report['model_count']} unique model(s), "
            f"disjoint check skipped (set TEND_POOL_DISJOINT=1 to enforce)"
        )
    else:
        log_module.emit("pool.disjoint.ok", model_count=report["model_count"])
        print(
            f"OK: {report['model_count']} models across 6 pools are pairwise disjoint"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
