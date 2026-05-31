"""Per-call LLM cost accounting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tend.config import RUN_DIR
from tend.core.sync import append_jsonl
from tend.errors import BudgetExceeded

_COST_PATH: Path | None = None
_TOTALS: dict[str, float] = {}
_BUDGETS: dict[str, float] = {
    "A_construct": 10_000.0,
    "B_rtv": 5_000.0,
    "C_nnc_attack": 5_000.0,
    "D_bridge": 2_000.0,
    "B_panel": 50_000.0,
    "S_solver": 0.0,
}


def init(run_dir: Path | None = None) -> Path:
    global _COST_PATH
    run_dir = run_dir or RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    _COST_PATH = run_dir / "cost.jsonl"
    _COST_PATH.touch(exist_ok=True)
    return run_dir


def record_call(
    *,
    pool: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: float,
    cost_usd: float,
    cache_hit: bool,
) -> None:
    if _COST_PATH is None:
        init()
    payload = {
        "pool": pool,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "cache_hit": cache_hit,
    }
    append_jsonl(_COST_PATH, payload)
    _TOTALS[pool] = _TOTALS.get(pool, 0.0) + cost_usd
    budget = _BUDGETS.get(pool)
    if budget is not None and _TOTALS[pool] > budget:
        raise BudgetExceeded(f"Pool {pool} exceeded budget {_TOTALS[pool]:.2f}>{budget}")


def write_global_report(out_path: Path) -> dict[str, Any]:
    report = {"pools": dict(_TOTALS), "budgets": dict(_BUDGETS)}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
