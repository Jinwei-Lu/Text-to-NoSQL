"""Explicit registry for database-specific native design modules."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

from tend.errors import MigrationError

from ..native_recipe import NativeMigrationRecipe


NATIVE_DESIGN_MODULES: dict[str, str] = {
    "california_schools": "tend.construct.native_designs.california_schools",
    "card_games": "tend.construct.native_designs.card_games",
    "codebase_community": "tend.construct.native_designs.codebase_community",
    "debit_card_specializing": "tend.construct.native_designs.debit_card_specializing",
    "european_football_2": "tend.construct.native_designs.european_football_2",
    "financial": "tend.construct.native_designs.financial",
    "formula_1": "tend.construct.native_designs.formula_1",
    "student_club": "tend.construct.native_designs.student_club",
    "superhero": "tend.construct.native_designs.superhero",
    "thrombosis_prediction": "tend.construct.native_designs.thrombosis_prediction",
    "toxicology": "tend.construct.native_designs.toxicology",
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
    builder = getattr(design, "build_native_recipe", None)
    if builder is None:
        raise MigrationError(
            f"native design module {design.__name__} has no build_native_recipe",
            context={"db_id": db_id, "module": design.__name__},
        )
    return builder(source, db_id)
