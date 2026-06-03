"""MongoDB-native Phase A construction route.

The concrete registry/executor imports are intentionally local so tests and future CLI
routing can stub the native implementation without importing optional design modules at
module import time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import MigrationError


@dataclass
class NativeDbArtifacts:
    mongodb_schema: dict[str, Any]
    mongodb_data: dict[str, Any]
    rationale: dict[str, Any]
    world_signature: str
    migration_recipe: Any
    native_feature_manifest: Any
    provenance: dict[str, Any]
    domain_id: str
    sqlite_path: str
    table_count: int
    query_count: int
    conversion_code_ref: str
    query_bearing: bool = True
    db_id: str = ""
    validation: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def _build_native_recipe_for_db(source: Any, db_id: str) -> Any:
    from .designs.registry import build_native_recipe_for_db

    return build_native_recipe_for_db(source, db_id)


def _verify_native_recipe(recipe: Any, source_schema: Any) -> Any:
    from .recipe import verify_native_recipe

    return verify_native_recipe(recipe, source_schema)


def _execute_native_recipe(source: Any, db_id: str, recipe: Any, **kwargs: Any) -> Any:
    from .executor import execute_native_recipe

    return execute_native_recipe(source, db_id, recipe, **kwargs)


def _materialize_native_dataworld_for_db(source: Any, db_id: str, **kwargs: Any) -> Any:
    from .designs.registry import materialize_native_dataworld_for_db

    return materialize_native_dataworld_for_db(source, db_id, **kwargs)


async def run_native_phase_a(wf: Any, db_ids: list[str]) -> dict[str, NativeDbArtifacts]:
    """Build native Phase A assets for each selected database."""
    wf.phase("A.native")
    progress = getattr(getattr(wf, "ctx", None), "progress", None)
    if progress:
        for db_id in db_ids:
            progress.add_group(db_id, db_id, phase="A.native", total=3)

    results = await wf.parallel(
        [lambda db=db_id: _native_phase_a_one_db(wf, db) for db_id in db_ids],
        isolate=True,
    )
    return {art.db_id: art for art in results if isinstance(art, NativeDbArtifacts)}


async def _native_phase_a_one_db(wf: Any, db_id: str) -> NativeDbArtifacts:
    source = getattr(getattr(wf, "ctx", None), "source", None)
    if source is None:
        raise MigrationError("native Phase A requires a source", context={"db_id": db_id})

    source_schema = source.schema(db_id)
    result = _materialize_native_dataworld_for_db(
        source,
        db_id,
        event_hook=_event_hook(wf, db_id),
    )
    recipe = _result_migration_recipe(db_id, result)
    validation = getattr(result, "validation", None)
    workload = source.workload(db_id) if hasattr(source, "workload") else []
    conversion_code_ref = f"tend.construction.designs.{db_id}"
    provenance = _native_provenance_payload(
        db_id=db_id,
        provenance=getattr(result, "provenance", {}),
        conversion_code_ref=conversion_code_ref,
    )
    return NativeDbArtifacts(
        db_id=db_id,
        mongodb_schema=getattr(result, "schema", {}),
        mongodb_data=getattr(result, "data", {}),
        rationale=_native_rationale(recipe, validation, getattr(result, "manifest", None)),
        world_signature=str(getattr(result, "world_signature", "")),
        migration_recipe=recipe,
        native_feature_manifest=getattr(result, "manifest", {}),
        provenance=provenance,
        domain_id=str(getattr(source_schema, "domain", "unknown")),
        sqlite_path=str(getattr(source_schema, "sqlite_path", "")),
        table_count=_table_count(source_schema),
        query_count=len(workload),
        conversion_code_ref=conversion_code_ref,
        query_bearing=True,
        validation=validation,
    )


def _event_hook(wf: Any, db_id: str) -> Any:
    log = getattr(getattr(wf, "ctx", None), "log", None)
    if log is None or not hasattr(log, "info"):
        return lambda *_args, **_kwargs: None

    def emit(event: str, **fields: Any) -> None:
        log.info(event, **{"db_id": db_id, **fields})

    return emit


def _result_migration_recipe(db_id: str, result: Any) -> Any:
    recipe = getattr(result, "migration_recipe", None) or getattr(result, "recipe", None)
    if recipe is not None:
        return recipe
    manifest = getattr(result, "manifest", None)
    feature_count = len(getattr(manifest, "features", []) or [])
    return {
        "db_id": db_id,
        "recipe_version": "direct",
        "direct_conversion": True,
        "design_goal": "database-specific semantic MongoDB materialization",
        "native_feature_count": feature_count,
    }


def _native_rationale(recipe: Any, validation: Any, manifest: Any = None) -> dict[str, Any]:
    if isinstance(recipe, dict) and recipe.get("direct_conversion"):
        return {
            "construction_mode": "direct_materializer",
            "design_goal": str(recipe.get("design_goal") or ""),
            "recipe_version": recipe.get("recipe_version"),
            "native_feature_count": recipe.get(
                "native_feature_count",
                len(getattr(manifest, "features", []) or []),
            ),
            "validation_errors": list(getattr(validation, "errors", []) or []),
        }
    return {
        "construction_mode": "recipe_executor",
        "design_goal": str(getattr(recipe, "design_goal", "")),
        "recipe_version": getattr(recipe, "recipe_version", None),
        "native_feature_count": getattr(validation, "native_feature_count", 0),
        "validation_errors": list(getattr(validation, "errors", []) or []),
    }


def _native_provenance_payload(
    *,
    db_id: str,
    provenance: Any,
    conversion_code_ref: str,
) -> dict[str, Any]:
    if hasattr(provenance, "to_dict"):
        provenance = provenance.to_dict()
    if not isinstance(provenance, dict):
        provenance = {"value": provenance}
    if "entries" in provenance:
        payload = dict(provenance)
        payload.setdefault("db_id", db_id)
    else:
        payload = {"db_id": db_id, "entries": provenance}
    payload["conversion_code_ref"] = conversion_code_ref
    return payload


def _table_count(source_schema: Any) -> int:
    value = getattr(source_schema, "table_count", None)
    if isinstance(value, int):
        return value
    tables = getattr(source_schema, "tables", None)
    if tables is None:
        return 0
    return len(tables)
