"""Public solver inputs and SMART-EG runtime."""
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


def __getattr__(name: str) -> Any:
    if name in {"SmartEGFailure", "SmartEGPolicy", "SmartEGPrediction", "smart_solve_nlq_db_eg"}:
        from . import eg

        return getattr(eg, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "NlqDbSolverInput",
    "NlqTrack",
    "SmartEGFailure",
    "SmartEGPolicy",
    "SmartEGPrediction",
    "build_nlq_db_solver_input",
    "build_witness_digest",
    "load_solver_release_inputs",
    "select_solver_release_records",
    "smart_solve_nlq_db_eg",
]
