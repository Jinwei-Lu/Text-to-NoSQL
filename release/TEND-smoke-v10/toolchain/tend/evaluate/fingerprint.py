"""7-bit evaluation fingerprint with exception branches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tend.core import AST_check
from tend.core.models import CanonicalFormSet
from tend.errors import BOT, BOT_EXEC

from tend.core.parse import Parse

from .metrics import (
    METRICS,
    exact_match,
    execution_accuracy,
    execution_field_match,
    execution_value_match,
    parse_failure_fingerprint,
    query_field_coverage,
    query_intent_match,
    query_structure_match,
)


@dataclass(frozen=True)
class FingerprintDiagnostics:
    parse_error: bool = False
    forbidden_op_hit: bool = False
    timeout_hit: bool = False
    oom_hit: bool = False
    ast_result: str = "pass"


def compute_fingerprint(
    q_p: str,
    q_g: str,
    r_p: Any,
    r_g: Any,
    canonical_form_set: dict | CanonicalFormSet,
    ast_result: str | None = None,
    *,
    forbidden_op_hit: bool = False,
    timeout_hit: bool = False,
    oom_hit: bool = False,
) -> tuple[int, int, int, int, int, int, int]:
    """Fixed-order 7-bit fingerprint: EM, QSM, QFC, EX, EFM, EVM, QIM."""
    if isinstance(Parse(q_p), BOT):
        return parse_failure_fingerprint()

    if forbidden_op_hit:
        ast_result = ast_result or "fail:forbidden_op"
        qim = 0
        em = exact_match(q_p, q_g)
        qsm = query_structure_match(q_p, q_g)
        qfc = query_field_coverage(q_p, q_g)
        ex = 0
        efm = 0
        evm = 0
        return (em, qsm, qfc, ex, efm, evm, qim)

    cfs = (
        canonical_form_set
        if isinstance(canonical_form_set, CanonicalFormSet)
        else CanonicalFormSet.from_dict(canonical_form_set)
    )
    ast_result = ast_result if ast_result is not None else AST_check(q_p, cfs)

    em = exact_match(q_p, q_g)
    qsm = query_structure_match(q_p, q_g)
    qfc = query_field_coverage(q_p, q_g)
    qim = int(ast_result == "pass")

    exec_failed = (
        isinstance(r_p, (BOT, BOT_EXEC))
        or isinstance(r_g, (BOT, BOT_EXEC))
        or timeout_hit
        or oom_hit
    )
    if exec_failed:
        ex = 0
        efm = 0
        evm = 0
    else:
        ex = execution_accuracy(q_p, cfs, r_p, r_g)
        efm = execution_field_match(r_p, r_g)
        evm = execution_value_match(r_p, r_g) if efm else 0

    if qim == 0:
        ex = 0

    return (em, qsm, qfc, ex, efm, evm, qim)


def fingerprint_to_dict(fp: tuple[int, ...]) -> dict[str, int]:
    return {name: fp[index] for index, name in enumerate(METRICS)}


def mean_fingerprint(fingerprints: list[tuple[int, ...]]) -> dict[str, float]:
    if not fingerprints:
        return {name: 0.0 for name in METRICS}
    size = len(fingerprints)
    return {
        name: round(sum(fp[index] for fp in fingerprints) / size, 6)
        for index, name in enumerate(METRICS)
    }
