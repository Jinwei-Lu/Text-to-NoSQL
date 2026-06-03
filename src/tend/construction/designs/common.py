"""Small recipe-building helpers for database-specific native designs."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..recipe import (
    NativeCollectionRecipe,
    NativeMigrationRecipe,
    NativeTransform,
)


def transform(
    transform_id: str,
    transform_type: str,
    *,
    module_ref: str,
    **raw: Any,
) -> NativeTransform:
    body = {
        "id": transform_id,
        "type": transform_type,
        "design_module": module_ref,
    }
    body.update(raw)
    return NativeTransform(id=transform_id, type=transform_type, raw=body)


def collection(
    name: str,
    *,
    purpose: str,
    source_tables: Iterable[str],
    transforms: Iterable[NativeTransform],
) -> NativeCollectionRecipe:
    return NativeCollectionRecipe(
        name=name,
        purpose=purpose,
        source_tables=list(source_tables),
        transforms=list(transforms),
    )


def recipe(
    db_id: str,
    *,
    version: int,
    design_goal: str,
    collections: Iterable[NativeCollectionRecipe],
) -> NativeMigrationRecipe:
    return NativeMigrationRecipe(
        db_id=db_id,
        recipe_version=version,
        design_goal=design_goal,
        collections={item.name: item for item in collections},
    )


def join(left: str, right: str) -> dict[str, str]:
    return {"left": left, "right": right}


def source(ref: str) -> dict[str, str]:
    return {"source": ref}


def expr(expression: str, *provenance: str) -> dict[str, Any]:
    return {"expr": expression, "provenance": list(provenance)}
