from __future__ import annotations

import json
import math
from typing import Any

from tend_core.checks import ast_check
from tend_core.models import CanonicalFormSet
from tend_core.mql import (
    canonical_text,
    contains_forbidden_operator,
    extract_field_paths,
    parse_ok,
    structure_signature,
)

FLOAT_ULP_TOLERANCE = 1e-9


def exact_match(predicted: str, gold: str) -> int:
    return int(canonical_text(predicted) == canonical_text(gold))


def query_structure_match(predicted: str, gold: str) -> int:
    if not parse_ok(predicted) or not parse_ok(gold):
        return 0
    pred_signature = structure_signature(predicted)
    gold_signature = structure_signature(gold)
    return int(pred_signature == gold_signature)


def query_field_coverage(predicted: str, gold: str) -> int:
    if not parse_ok(predicted) or not parse_ok(gold):
        return 0
    return int(extract_field_paths(predicted) == extract_field_paths(gold))


# ---------------------------------------------------------------------------
# NormExec – 4-layer normalization (§01 §4-5)
# ---------------------------------------------------------------------------

def normalize_scalar(value: Any) -> Any:
    """Layer 1: Scalar normalization with float tolerance."""
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


def normalize_composite(value: Any) -> Any:
    """Layer 2: Composite normalization – dicts sorted by key, lists by content."""
    if isinstance(value, dict):
        return {key: normalize_composite(normalize_scalar(value[key])) for key in sorted(value)}
    if isinstance(value, list):
        normalized_items = [normalize_composite(normalize_scalar(item)) for item in value]
        return sorted(normalized_items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return normalize_scalar(value)


def normalize_null_vs_missing(value: Any) -> Any:
    """Layer 3: Null-vs-missing normalization. Explicit nulls are preserved;
    missing keys are treated as absent (not compared)."""
    if isinstance(value, dict):
        return {
            key: normalize_null_vs_missing(v)
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [normalize_null_vs_missing(item) for item in value]
    return value


def normalize_shape(value: Any) -> Any:
    """Layer 4: _id and shape normalization.
    Strip auto-generated _id fields that are ObjectId-like strings."""
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


def normalize_result(value: Any) -> Any:
    """Full 4-layer NormExec pipeline."""
    value = normalize_shape(value)
    value = normalize_null_vs_missing(value)
    return normalize_composite(value)


def _floats_close(a: float, b: float) -> bool:
    return abs(a - b) <= FLOAT_ULP_TOLERANCE or abs(a - b) <= FLOAT_ULP_TOLERANCE * max(abs(a), abs(b))


def _values_equiv(a: Any, b: Any) -> bool:
    """Deep equivalence with float tolerance."""
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


def rec_equiv(predicted_result: Any, gold_result: Any) -> bool:
    """≡_rec: Recursive equivalence with float tolerance and 4-layer normalization."""
    norm_pred = normalize_result(predicted_result)
    norm_gold = normalize_result(gold_result)
    return _values_equiv(norm_pred, norm_gold)


def execution_field_match(predicted_result: list[dict[str, Any]], gold_result: list[dict[str, Any]]) -> int:
    if len(predicted_result) != len(gold_result):
        return 0
    for predicted_doc, gold_doc in zip(predicted_result, gold_result):
        if set(predicted_doc) != set(gold_doc):
            return 0
    return 1


def execution_value_match(predicted_result: list[dict[str, Any]], gold_result: list[dict[str, Any]]) -> int:
    return int(rec_equiv(predicted_result, gold_result))


def query_intent_match(predicted: str, canonical_form_set: CanonicalFormSet) -> int:
    return int(parse_ok(predicted) and ast_check(predicted, canonical_form_set) == "pass")


def execution_accuracy(
    predicted: str,
    canonical_form_set: CanonicalFormSet,
    predicted_result: Any,
    gold_result: Any,
) -> int:
    return int(
        ast_check(predicted, canonical_form_set) == "pass"
        and rec_equiv(predicted_result, gold_result)
    )


def parse_failure_fingerprint() -> tuple[int, int, int, int, int, int, int]:
    return (0, 0, 0, 0, 0, 0, 0)


def has_forbidden_operator(query: str) -> bool:
    return contains_forbidden_operator(query)
