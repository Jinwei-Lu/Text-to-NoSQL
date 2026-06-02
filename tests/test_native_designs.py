from __future__ import annotations

from pathlib import Path

import pytest

from tend.errors import MigrationError
from tend.source import BirdSource
from tend.source.bird import BIRD_DBS


def _bird_source() -> BirdSource:
    root = Path(__file__).resolve().parents[1] / "minidev" / "MINIDEV"
    return BirdSource(root)


def test_native_design_registry_covers_each_bird_minidev_db():
    from tend.construct.native_designs.registry import NATIVE_DESIGN_MODULES

    assert set(NATIVE_DESIGN_MODULES) == set(BIRD_DBS)
    for db_id, module_ref in NATIVE_DESIGN_MODULES.items():
        assert module_ref == f"tend.construct.native_designs.{db_id}"


def test_native_designs_build_verified_database_specific_recipes():
    from tend.construct.native_designs.registry import build_native_recipe_for_db
    from tend.construct.native_recipe import NATIVE_TRANSFORMS, verify_native_recipe

    source = _bird_source()

    for db_id in BIRD_DBS:
        recipe = build_native_recipe_for_db(source, db_id)
        result = verify_native_recipe(recipe, source.schema(db_id))
        transforms = [
            transform
            for collection in recipe.collections.values()
            for transform in collection.transforms
        ]

        assert recipe.db_id == db_id
        assert recipe.recipe_version == 1
        assert result.ok, f"{db_id}: {result.errors}"
        assert any(transform.type == "dynamic_key_object" for transform in transforms)
        assert sum(transform.type in NATIVE_TRANSFORMS for transform in transforms) >= 2
        assert any(
            f"tend.construct.native_designs.{db_id}" in transform.raw.get("design_module", "")
            for transform in transforms
        )


def test_native_design_registry_fails_closed_for_unknown_db():
    from tend.construct.native_designs.registry import build_native_recipe_for_db

    with pytest.raises(MigrationError, match="no native design module"):
        build_native_recipe_for_db(_bird_source(), "not_a_bird_db")
