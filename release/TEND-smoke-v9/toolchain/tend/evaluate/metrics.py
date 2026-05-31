"""Seven TEND evaluation metrics (EM/QSM/QFC/EX/EFM/EVM/QIM)."""

from __future__ import annotations

from typing import Any

from tend.core import AST_check, canonical_text, equiv_rec
from tend.core.mql import (
    extract_field_paths,
    parse_ok,
    structure_signature,
)
from tend.core.models import CanonicalFormSet
from tend.errors import BOT

from tend.core.parse import Parse

METRICS = ("EM", "QSM", "QFC", "EX", "EFM", "EVM", "QIM")
METRIC_KEYS = tuple(name.lower() for name in METRICS)


def exact_match(predicted: str, gold: str) -> int:
    return int(canonical_text(predicted) == canonical_text(gold))


def query_structure_match(predicted: str, gold: str) -> int:
    if not parse_ok(predicted) or not parse_ok(gold):
        return 0
    pred_sig = structure_signature(predicted)
    gold_sig = structure_signature(gold)
    return int(pred_sig == gold_sig)


def query_field_coverage(predicted: str, gold: str) -> int:
    if not parse_ok(predicted) or not parse_ok(gold):
        return 0
    return int(extract_field_paths(predicted) == extract_field_paths(gold))


def query_intent_match(predicted: str, canonical_form_set: dict | CanonicalFormSet) -> int:
    if isinstance(canonical_form_set, dict):
        canonical_form_set = CanonicalFormSet.from_dict(canonical_form_set)
    return int(parse_ok(predicted) and AST_check(predicted, canonical_form_set) == "pass")


def execution_field_match(
    predicted_result: list[dict[str, Any]],
    gold_result: list[dict[str, Any]],
) -> int:
    if len(predicted_result) != len(gold_result):
        return 0
    for predicted_doc, gold_doc in zip(predicted_result, gold_result):
        if set(predicted_doc) != set(gold_doc):
            return 0
    return 1


def execution_value_match(
    predicted_result: list[dict[str, Any]],
    gold_result: list[dict[str, Any]],
) -> int:
    if not execution_field_match(predicted_result, gold_result):
        return 0
    return int(equiv_rec(predicted_result, gold_result, order_sensitive=False))


def execution_accuracy(
    predicted: str,
    canonical_form_set: dict | CanonicalFormSet,
    predicted_result: Any,
    gold_result: Any,
) -> int:
    cfs = (
        canonical_form_set
        if isinstance(canonical_form_set, CanonicalFormSet)
        else CanonicalFormSet.from_dict(canonical_form_set)
    )
    return int(
        AST_check(predicted, cfs) == "pass"
        and equiv_rec(predicted_result, gold_result, order_sensitive=False)
    )


def parse_is_bot(q: str) -> bool:
    return isinstance(Parse(q), BOT)


def parse_failure_fingerprint() -> tuple[int, int, int, int, int, int, int]:
    return (0, 0, 0, 0, 0, 0, 0)
