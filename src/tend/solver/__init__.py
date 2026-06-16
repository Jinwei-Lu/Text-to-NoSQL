"""Public solver inputs and the SAG (Schema-as-Data Grounding) runtime."""
from __future__ import annotations

from typing import Any

from .inputs import (
    NlqDbSolverInput,
    NlqTrack,
    build_nlq_db_solver_input,
    build_witness_digest,
    load_solver_release_inputs,
    select_solver_release_records,
)

_SAG_EXPORTS = {
    "GroundingIndex",
    "GroundingIndexCache",
    "SAGFailure",
    "SAGPolicy",
    "SAGPrediction",
    "sag_solve_nlq_db",
    "sag_solve_record",
}


def __getattr__(name: str) -> Any:
    if name in _SAG_EXPORTS:
        from . import sag

        return getattr(sag, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GroundingIndex",
    "GroundingIndexCache",
    "NlqDbSolverInput",
    "NlqTrack",
    "SAGFailure",
    "SAGPolicy",
    "SAGPrediction",
    "build_nlq_db_solver_input",
    "build_witness_digest",
    "load_solver_release_inputs",
    "sag_solve_nlq_db",
    "sag_solve_record",
    "select_solver_release_records",
]
