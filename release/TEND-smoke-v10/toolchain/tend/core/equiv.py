from __future__ import annotations

import json
import math
from typing import Any


FLOAT_ULP_TOLERANCE = 1e-9


def normalize_scalar(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return "__NaN__"
        if math.isinf(value):
            return "__Inf__" if value > 0 else "__-Inf__"
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value.strip()
    return value


def normalize_composite(value: Any, *, order_sensitive: bool) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize_composite(normalize_scalar(value[key]), order_sensitive=order_sensitive)
            for key in sorted(value)
        }
    if isinstance(value, list):
        normalized_items = [
            normalize_composite(normalize_scalar(item), order_sensitive=order_sensitive) for item in value
        ]
        if order_sensitive:
            return normalized_items
        return sorted(normalized_items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return normalize_scalar(value)


def normalize_null_vs_missing(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalize_null_vs_missing(v) for key, v in value.items()}
    if isinstance(value, list):
        return [normalize_null_vs_missing(item) for item in value]
    return value


def normalize_shape(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, v in value.items():
            if key == "_id" and isinstance(v, str) and len(v) == 24:
                continue
            out[key] = normalize_shape(v)
        return out
    if isinstance(value, list):
        return [normalize_shape(item) for item in value]
    return value


def Norm(raw: Any, *, gold_mql: str = "", shape_policy: str = "preserve") -> Any:
    value = normalize_shape(raw)
    value = normalize_null_vs_missing(value)
    return normalize_composite(value, order_sensitive=shape_policy != "reshape")


def _floats_close(a: float, b: float) -> bool:
    return abs(a - b) <= FLOAT_ULP_TOLERANCE or abs(a - b) <= FLOAT_ULP_TOLERANCE * max(abs(a), abs(b))


def _values_equiv(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return _floats_close(a, b)
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_values_equiv(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_values_equiv(ai, bi) for ai, bi in zip(a, b))
    return a == b


def equiv_rec(a: Any, b: Any, *, order_sensitive: bool = False) -> bool:
    norm_a = Norm(a, shape_policy="preserve" if order_sensitive else "reshape")
    norm_b = Norm(b, shape_policy="preserve" if order_sensitive else "reshape")
    return _values_equiv(norm_a, norm_b)
