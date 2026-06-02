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
