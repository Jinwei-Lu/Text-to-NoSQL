"""Deterministic canonicalization + signatures for witnesses and gold MQL.

``world_signature = sha256(canonical_json(mongodb_data))`` pins a record's gold to the
exact witness it was verified against. Canonicalization sorts keys, uses compact
separators, and rounds floats so byte-identical data yields byte-identical signatures.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_FLOAT_NDIGITS = 12


def _canon(obj: Any) -> Any:
    if isinstance(obj, float):
        # avoid -0.0 / precision drift defeating determinism
        r = round(obj, _FLOAT_NDIGITS)
        return 0.0 if r == 0 else r
    if isinstance(obj, dict):
        return {k: _canon(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Stable JSON string (sorted keys, compact, rounded floats)."""
    return json.dumps(_canon(obj), ensure_ascii=False, separators=(",", ":"))


def world_signature(mongodb_data: Any) -> str:
    digest = hashlib.sha256(canonical_json(mongodb_data).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def canonical_mql(mql: str) -> str:
    """Stable representation of a ``db.<collection>.aggregate([...])`` string.

    The parser accepts the same json5-flavored MQL syntax as the execution layer. If a
    malformed candidate reaches this helper, keep a whitespace-normalized raw fallback so
    diagnostics and duplicate checks still have a deterministic value.
    """
    from .ast_check import parse_pipeline

    try:
        collection, pipeline = parse_pipeline(mql)
        payload = {"collection": collection, "pipeline": pipeline}
    except Exception:  # noqa: BLE001 - signature generation must not hide the real failure
        payload = {"raw": " ".join(str(mql).split())}
    return canonical_json(payload)


def mql_signature(mql: str) -> str:
    """SHA-256 signature for canonicalized representative MQL."""
    digest = hashlib.sha256(canonical_mql(mql).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def mql_skeleton(mql: str) -> str:
    """Return an abstract query-shape representation for diversity checks.

    Unlike :func:`mql_signature`, this intentionally erases collection names, schema field
    names, literal constants, and output aliases. It keeps the aggregation stage order and
    operator tree, so a matrix of queries that only swaps ``amount`` for ``balance`` or
    ``$avg`` for ``$sum`` still collapses into a small number of skeleton families.
    """
    from .ast_check import parse_pipeline

    try:
        _collection, pipeline = parse_pipeline(mql)
        payload = {
            "root_ops": [_stage_root_op(stage) for stage in pipeline],
            "pipeline": [_abstract_mql_node(stage) for stage in pipeline],
        }
    except Exception:  # noqa: BLE001 - diagnostics should survive malformed MQL
        payload = {"raw_shape": " ".join(str(mql).split())[:240]}
    return canonical_json(payload)


def mql_skeleton_signature(mql: str) -> str:
    """SHA-256 signature of the field/literal-abstracted MQL skeleton."""
    digest = hashlib.sha256(mql_skeleton(mql).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def mql_skeleton_summary(mql: str) -> str:
    """Compact human-readable stage-order summary for logs and release metadata."""
    from .ast_check import parse_pipeline

    try:
        _collection, pipeline = parse_pipeline(mql)
    except Exception:  # noqa: BLE001 - keep log emission non-throwing
        return "malformed"
    return ">".join(_stage_root_op(stage) for stage in pipeline) or "empty"


def _stage_root_op(stage: Any) -> str:
    if not isinstance(stage, dict) or not stage:
        return "?"
    if len(stage) == 1:
        return str(next(iter(stage)))
    return "+".join(str(k) for k in sorted(stage))


def _abstract_mql_node(value: Any) -> Any:
    if isinstance(value, dict):
        return [
            [
                key if isinstance(key, str) and key.startswith("$") else "<field>",
                _abstract_mql_node(child),
            ]
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        ]
    if isinstance(value, list):
        return [_abstract_mql_node(item) for item in value]
    if isinstance(value, str):
        if value.startswith("$$"):
            return "$$var"
        if value.startswith("$"):
            return "$field"
        return "<literal>"
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, (int, float)):
        return "<number>"
    if value is None:
        return "<null>"
    return f"<{type(value).__name__}>"
