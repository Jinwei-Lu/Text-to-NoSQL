"""Execution-grounded repair gradient: prefix bisection + output contracts.

When a gate-clean pipeline executes to an empty result, "0 rows" is a gradient-free
signal. Prefix bisection executes ``pipeline[:k] + [$count]`` to locate the stage
where the row count first collapses to zero (or errors) and describes it with the
ACTUAL distinct values at the filtered paths — dense, data-grounded feedback the
model can act on. The synthetic ``_id`` contract catches release-world internal
document keys (``"db::42"``) leaking into result rows.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ...errors import ExecutionError
from ...execution.mongo import _normalize_doc
from .gates import dyn_prefixed
from .world import WorldAccess

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .induction import GroundingIndex

_STAGE_PREVIEW_CHARS = 140
_DISTINCT_PREVIEW_CHARS = 220
_SYNTHETIC_ID_SCAN_ROWS = 5


def run_pipeline(
    world: WorldAccess, coll: str, pipe: list[dict[str, Any]], *, timeout_ms: int = 20_000
) -> list[dict[str, Any]]:
    """Execute and normalize (the same normalization evaluation uses)."""
    return [_normalize_doc(d) for d in world.aggregate(coll, pipe, max_time_ms=timeout_ms)]


def stage_count(
    world: WorldAccess, coll: str, prefix: list[dict[str, Any]], *, timeout_ms: int = 15_000
) -> int | str:
    try:
        r = world.aggregate(coll, prefix + [{"$count": "n"}], max_time_ms=timeout_ms)
        return int(r[0]["n"]) if r else 0
    except ExecutionError as exc:
        detail = str(exc.context.get("error") or exc.message)
        return f"ERROR: {detail[:100]}"


def match_paths_values(stage: dict[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    m = stage.get("$match")
    if isinstance(m, dict):
        for k, v in m.items():
            if k.startswith("$"):
                continue
            if isinstance(v, dict):
                for op in ("$eq", "$in"):
                    if op in v:
                        out.append((k, v[op]))
            else:
                out.append((k, v))
    return out


def distinct_sample(
    world: WorldAccess, coll: str, path: str, k: int = 8, *, timeout_ms: int = 8_000
) -> list[Any] | None:
    try:
        r = world.aggregate(
            coll, [{"$group": {"_id": f"${path}"}}, {"$limit": k}], max_time_ms=timeout_ms
        )
        return [x["_id"] for x in r]
    except ExecutionError:
        return None


def bisect_empty(
    world: WorldAccess,
    index: "GroundingIndex",
    coll: str,
    pipe: list[dict[str, Any]],
    *,
    stage_timeout_ms: int = 15_000,
    distinct_k: int = 8,
) -> str | None:
    """Locate the stage where the row count first drops to 0 / errors; describe it
    with actual data values at the filtered paths."""
    prev: int | None = None
    for k in range(1, len(pipe) + 1):
        n = stage_count(world, coll, pipe[:k], timeout_ms=stage_timeout_ms)
        if isinstance(n, str):
            return f"stage {k} ({json.dumps(pipe[k - 1])[:_STAGE_PREVIEW_CHARS]}) raises {n}"
        if n == 0:
            stage = pipe[k - 1]
            msg = (
                f"stage {k} ({json.dumps(stage)[:_STAGE_PREVIEW_CHARS]}) reduces "
                f"{prev if prev is not None else 'all'} rows -> 0."
            )
            dyn = index.dynamic_maps.get(coll, {})
            for path, _val in match_paths_values(stage)[:2]:
                if dyn_prefixed(index, coll, path):
                    dp = next(
                        d
                        for d in dyn
                        if path == d or path.startswith(d + ".") or d.startswith(path)
                    )
                    msg += (
                        f" `{path}` is under dynamic-key map `{dp}` "
                        f"(example keys {list(dyn[dp])[:4]})."
                    )
                else:
                    vals = distinct_sample(world, coll, path, distinct_k)
                    if vals is not None:
                        msg += (
                            f" Actual values at `{path}` (sample): "
                            f"{json.dumps(vals, default=str)[:_DISTINCT_PREVIEW_CHARS]}."
                        )
            return msg
        prev = n
    return None


def synthetic_id_violation(rows: list[dict[str, Any]]) -> str | None:
    """Result rows leak the release-world synthetic document key ('db::42')."""
    if any(
        isinstance(d, dict) and isinstance(d.get("_id"), str) and "::" in d["_id"]
        for d in rows[:_SYNTHETIC_ID_SCAN_ROWS]
    ):
        return (
            "result rows carry the internal document `_id` "
            "(synthetic key like 'db::42'); the question does not "
            "ask for it — exclude it with `_id: 0` in the final "
            "$project."
        )
    return None
