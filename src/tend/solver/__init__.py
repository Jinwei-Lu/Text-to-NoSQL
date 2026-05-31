"""SMART schema-less solver implementation."""
from __future__ import annotations

from .contracts import (
    CollectionShape,
    FieldLocus,
    LogicalSpec,
    PhysicalPlan,
    PlannedStage,
    ShapeModel,
    ShapeVariant,
    SolverDisclosure,
    SolverPrediction,
)
from .workflow import smart_solve_record

__all__ = [
    "CollectionShape",
    "FieldLocus",
    "LogicalSpec",
    "PhysicalPlan",
    "PlannedStage",
    "ShapeModel",
    "ShapeVariant",
    "SolverDisclosure",
    "SolverPrediction",
    "smart_solve_record",
]
