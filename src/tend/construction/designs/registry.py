"""Explicit registry for database-specific native design modules."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

from tend.errors import MigrationError

NATIVE_DESIGN_MODULES: dict[str, str] = {
    "california_schools": "tend.construction.designs.california_schools",
    "card_games": "tend.construction.designs.card_games",
    "codebase_community": "tend.construction.designs.codebase_community",
    "debit_card_specializing": "tend.construction.designs.debit_card_specializing",
    "european_football_2": "tend.construction.designs.european_football_2",
    "financial": "tend.construction.designs.financial",
    "formula_1": "tend.construction.designs.formula_1",
    "student_club": "tend.construction.designs.student_club",
    "superhero": "tend.construction.designs.superhero",
    "thrombosis_prediction": "tend.construction.designs.thrombosis_prediction",
    "toxicology": "tend.construction.designs.toxicology",
}


def get_native_design(db_id: str) -> ModuleType:
    module_ref = NATIVE_DESIGN_MODULES.get(db_id)
    if module_ref is None:
        raise MigrationError(
            f"no native design module for db_id {db_id!r}",
            context={"db_id": db_id, "known": sorted(NATIVE_DESIGN_MODULES)},
        )
    return import_module(module_ref)


def materialize_native_dataworld_for_db(
    source: Any,
    db_id: str,
    *,
    event_hook: Any = None,
) -> Any:
    return get_native_design(db_id).materialize_native_dataworld(
        source, db_id, event_hook=event_hook
    )
