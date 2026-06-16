"""Explicit registry for database-specific native design modules."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

from tend.errors import MigrationError

from ..recipe import NativeMigrationRecipe


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


def build_native_recipe_for_db(source: Any, db_id: str) -> NativeMigrationRecipe:
    design = get_native_design(db_id)
    return _build_native_recipe_from_design(source, db_id, design)


def materialize_native_dataworld_for_db(
    source: Any,
    db_id: str,
    *,
    event_hook: Any = None,
) -> Any:
    design = get_native_design(db_id)
    materializer = getattr(design, "materialize_native_dataworld", None)
    if materializer is not None:
        return materializer(source, db_id, event_hook=event_hook)

    recipe = _build_native_recipe_from_design(source, db_id, design)
    from ..executor import execute_native_recipe

    result = execute_native_recipe(source, db_id, recipe, event_hook=event_hook)
    result.migration_recipe = recipe
    return result


def _build_native_recipe_from_design(
    source: Any,
    db_id: str,
    design: ModuleType,
) -> NativeMigrationRecipe:
    builder = getattr(design, "build_native_recipe", None)
    if builder is None:
        raise MigrationError(
            f"native design module {design.__name__} has no build_native_recipe",
            context={"db_id": db_id, "module": design.__name__},
        )
    return builder(source, db_id)
