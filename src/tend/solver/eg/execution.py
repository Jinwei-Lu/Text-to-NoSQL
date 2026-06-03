"""Deterministic rendering and execution checks for SMART-EG."""
from __future__ import annotations

import json
from typing import Any

from ...errors import ExecutionError
from ...execution.ast_check import parse_pipeline

DISALLOWED_OPERATORS = {
    "$accumulator",
    "$function",
    "$merge",
    "$out",
    "$rand",
    "$sample",
    "$where",
}


def render_mql(collection: str, pipeline: list[dict[str, Any]]) -> str:
    return f"db.{collection}.aggregate({json.dumps(pipeline, ensure_ascii=False, separators=(',', ':'))})"


def parse_or_render_mql(
    *,
    collection: str | None,
    pipeline: list[dict[str, Any]] | None,
    mql: str | None,
) -> tuple[str, list[dict[str, Any]], str]:
    if mql:
        parsed_collection, parsed_pipeline = parse_pipeline(mql)
        return parsed_collection, parsed_pipeline, mql
    if not collection or pipeline is None:
        raise ExecutionError("final MQL requires collection and pipeline")
    return collection, pipeline, render_mql(collection, pipeline)


def check_ast_filter(mql: str) -> dict[str, Any]:
    collection, pipeline = parse_pipeline(mql)
    hits = sorted(_walk_disallowed(pipeline))
    return {
        "ok": not hits,
        "collection": collection,
        "stage_count": len(pipeline),
        "disallowed_operators": hits,
    }


def run_final_sanity_execution(
    *,
    executor: Any,
    db_id: str,
    mql: str,
) -> dict[str, Any]:
    if executor is None:
        return {"ok": True, "skipped": True, "reason": "no_executor"}
    try:
        if hasattr(executor, "norm_exec"):
            rows = executor.norm_exec(db_id, mql)
        elif hasattr(executor, "aggregate_readonly_bounded"):
            summary = executor.aggregate_readonly_bounded(db_id, mql, limit=50)
            return {
                "ok": True,
                "skipped": False,
                "row_count": int(summary.get("count", 0)) if isinstance(summary, dict) else 0,
                "sample_preview": _safe_preview(summary.get("sample", [])) if isinstance(summary, dict) else [],
            }
        else:
            return {"ok": True, "skipped": True, "reason": "executor_has_no_norm_exec"}
    except Exception as exc:  # noqa: BLE001 - execution feedback is solver evidence
        return {
            "ok": False,
            "skipped": False,
            "error": str(exc)[:500],
            "error_type": type(exc).__name__,
        }
    return {
        "ok": True,
        "skipped": False,
        "row_count": len(rows or []),
        "sample_preview": _safe_preview(rows),
    }


def _walk_disallowed(value: Any) -> set[str]:
    hits: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in DISALLOWED_OPERATORS:
                hits.add(str(key))
            hits.update(_walk_disallowed(child))
    elif isinstance(value, list):
        for child in value:
            hits.update(_walk_disallowed(child))
    elif isinstance(value, str) and value in DISALLOWED_OPERATORS:
        hits.add(value)
    return hits


def _safe_preview(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    preview: list[dict[str, Any]] = []
    for row in rows[:2]:
        if isinstance(row, dict):
            preview.append({str(key): _clip_value(value) for key, value in list(row.items())[:8]})
    return preview


def _clip_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:80]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_clip_value(item) for item in value[:4]]
    if isinstance(value, dict):
        return {str(key): _clip_value(child) for key, child in list(value.items())[:6]}
    return str(value)[:80]
