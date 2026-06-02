"""Codex-native migration recipe contracts and validation.

The native migration path is recipe-first: an LLM proposes a structured recipe, then
deterministic code verifies and executes it. This module intentionally keeps the wire
shape plain dict/YAML-friendly so recipes can be logged, reviewed, and reproduced.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_TRANSFORMS: frozenset[str] = frozenset({
    "polymorphic_union",
    "optional_embed",
    "dynamic_key_object",
    "attribute_bag",
    "derived_tag_array",
    "versioned_document",
    "nested_event_stream",
    "shape_preserving_projection",
    "reference_collection",
})

NATIVE_TRANSFORMS: frozenset[str] = frozenset({
    "polymorphic_union",
    "dynamic_key_object",
    "attribute_bag",
    "derived_tag_array",
    "versioned_document",
    "nested_event_stream",
})


@dataclass
class NativeTransform:
    id: str
    type: str
    raw: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass
class NativeCollectionRecipe:
    name: str
    purpose: str
    source_tables: list[str]
    transforms: list[NativeTransform] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "source_tables": list(self.source_tables),
            "transforms": [transform.to_dict() for transform in self.transforms],
        }


@dataclass
class NativeMigrationRecipe:
    db_id: str
    recipe_version: int
    design_goal: str
    collections: dict[str, NativeCollectionRecipe]

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_id": self.db_id,
            "recipe_version": self.recipe_version,
            "design_goal": self.design_goal,
            "collections": {
                name: collection.to_dict()
                for name, collection in sorted(self.collections.items())
            },
        }


@dataclass
class NativeFeature:
    id: str
    type: str
    collection: str
    field: str = ""
    query_patterns: list[str] = dc_field(default_factory=list)
    required_constructs: list[str] = dc_field(default_factory=list)
    provenance_refs: list[str] = dc_field(default_factory=list)
    coverage: dict[str, Any] = dc_field(default_factory=dict)
    extra: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "type": self.type,
            "collection": self.collection,
            "field": self.field,
            "supported_query_patterns": list(self.query_patterns),
            "required_native_constructs": list(self.required_constructs),
            "provenance_refs": list(self.provenance_refs),
            "coverage": dict(self.coverage),
        }
        out.update(self.extra)
        return out


@dataclass
class NativeFeatureManifest:
    db_id: str
    features: list[NativeFeature] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_id": self.db_id,
            "features": [feature.to_dict() for feature in self.features],
        }


@dataclass
class NativeProvenance:
    db_id: str
    entries: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"db_id": self.db_id, "entries": dict(self.entries)}


@dataclass
class RecipeValidationResult:
    ok: bool
    errors: list[str] = dc_field(default_factory=list)
    native_feature_count: int = 0


def _load_mapping(value: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    path = Path(value)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        loaded = json.loads(text)
    else:
        loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"expected mapping in {path}")
    return loaded


def load_native_recipe(path_or_mapping: str | Path | dict[str, Any]) -> NativeMigrationRecipe:
    raw = _load_mapping(path_or_mapping)
    collections_raw = raw.get("collections") or {}
    if not isinstance(collections_raw, dict):
        collections_raw = {}
    collections: dict[str, NativeCollectionRecipe] = {}
    for name, collection_raw in collections_raw.items():
        collection = collection_raw if isinstance(collection_raw, dict) else {}
        transforms = []
        for transform_raw in collection.get("transforms") or []:
            if not isinstance(transform_raw, dict):
                continue
            transforms.append(
                NativeTransform(
                    id=str(transform_raw.get("id") or ""),
                    type=str(transform_raw.get("type") or ""),
                    raw=dict(transform_raw),
                )
            )
        collections[str(name)] = NativeCollectionRecipe(
            name=str(name),
            purpose=str(collection.get("purpose") or ""),
            source_tables=[str(table) for table in collection.get("source_tables") or []],
            transforms=transforms,
        )
    return NativeMigrationRecipe(
        db_id=str(raw.get("db_id") or ""),
        recipe_version=int(raw.get("recipe_version") or 0),
        design_goal=str(raw.get("design_goal") or ""),
        collections=collections,
    )


def dump_native_recipe(recipe: NativeMigrationRecipe) -> dict[str, Any]:
    return recipe.to_dict()


def load_native_feature_manifest(
    path_or_mapping: str | Path | dict[str, Any],
) -> NativeFeatureManifest:
    raw = _load_mapping(path_or_mapping)
    features: list[NativeFeature] = []
    for feature_raw in raw.get("features") or []:
        if not isinstance(feature_raw, dict):
            continue
        extra = {
            key: value
            for key, value in feature_raw.items()
            if key not in {
                "id",
                "type",
                "collection",
                "field",
                "supported_query_patterns",
                "query_patterns",
                "required_native_constructs",
                "required_constructs",
                "provenance_refs",
                "coverage",
            }
        }
        features.append(
            NativeFeature(
                id=str(feature_raw.get("id") or ""),
                type=str(feature_raw.get("type") or ""),
                collection=str(feature_raw.get("collection") or ""),
                field=str(feature_raw.get("field") or ""),
                query_patterns=[
                    str(value)
                    for value in (
                        feature_raw.get("supported_query_patterns")
                        or feature_raw.get("query_patterns")
                        or []
                    )
                ],
                required_constructs=[
                    str(value)
                    for value in (
                        feature_raw.get("required_native_constructs")
                        or feature_raw.get("required_constructs")
                        or []
                    )
                ],
                provenance_refs=[
                    str(value) for value in feature_raw.get("provenance_refs") or []
                ],
                coverage=dict(feature_raw.get("coverage") or {}),
                extra=extra,
            )
        )
    return NativeFeatureManifest(db_id=str(raw.get("db_id") or ""), features=features)


def dump_native_feature_manifest(manifest: NativeFeatureManifest) -> dict[str, Any]:
    return manifest.to_dict()


def verify_native_recipe(recipe: NativeMigrationRecipe, source_schema: Any) -> RecipeValidationResult:
    errors: list[str] = []
    source_tables = set(getattr(source_schema, "tables", []) or [])
    source_columns = {
        f"{col.table}.{col.name}"
        for col in getattr(source_schema, "columns", []) or []
    }
    native_count = 0
    has_dynamic_key = False

    if not recipe.db_id:
        errors.append("missing db_id")
    if recipe.db_id and getattr(source_schema, "db_id", recipe.db_id) != recipe.db_id:
        errors.append(
            f"db_id {recipe.db_id!r} does not match source schema "
            f"{getattr(source_schema, 'db_id', '')!r}"
        )
    if not recipe.collections:
        errors.append("recipe must define at least one collection")

    for collection_name, collection in sorted(recipe.collections.items()):
        if not collection_name:
            errors.append("collection missing name")
        if not collection.source_tables:
            errors.append(f"collection {collection_name} missing source_tables")
        for table in collection.source_tables:
            if table not in source_tables:
                errors.append(
                    f"collection {collection_name} references unknown source table {table}"
                )
        if not collection.transforms:
            errors.append(f"collection {collection_name} missing transforms")
        for transform in collection.transforms:
            _verify_transform(
                collection_name,
                transform,
                source_tables=source_tables,
                source_columns=source_columns,
                errors=errors,
            )
            if transform.type in NATIVE_TRANSFORMS:
                native_count += 1
            if transform.type == "dynamic_key_object":
                has_dynamic_key = True

    if native_count == 0:
        errors.append("recipe requires at least one MongoDB-native transform")
    if not has_dynamic_key:
        errors.append("recipe requires at least one dynamic_key_object")
    return RecipeValidationResult(ok=not errors, errors=errors, native_feature_count=native_count)


def _verify_transform(
    collection_name: str,
    transform: NativeTransform,
    *,
    source_tables: set[str],
    source_columns: set[str],
    errors: list[str],
) -> None:
    if not transform.id:
        errors.append(f"collection {collection_name} transform missing id")
    if transform.type not in SUPPORTED_TRANSFORMS:
        errors.append(
            f"collection {collection_name} transform {transform.id} unsupported transform type "
            f"{transform.type!r}"
        )
        return
    raw = transform.raw
    for key in ("source_table", "parent_table", "child_table", "event_source_table"):
        value = raw.get(key)
        if isinstance(value, str) and value and value not in source_tables:
            errors.append(
                f"collection {collection_name} transform {transform.id} references "
                f"unknown source table {value}"
            )
    _verify_join(collection_name, transform, source_columns=source_columns, errors=errors)
    if transform.type == "dynamic_key_object":
        _verify_dynamic_key(collection_name, transform, source_columns, errors)
    if transform.type == "polymorphic_union":
        _verify_polymorphic_union(collection_name, transform, source_columns, errors)
    if transform.type == "derived_tag_array":
        _verify_derived_tags(collection_name, transform, source_columns, errors)
    if transform.type == "nested_event_stream":
        _verify_nested_event_stream(collection_name, transform, source_columns, errors)


def _verify_join(
    collection_name: str,
    transform: NativeTransform,
    *,
    source_columns: set[str],
    errors: list[str],
) -> None:
    join = transform.raw.get("join")
    if not isinstance(join, dict):
        return
    for side in ("left", "right"):
        ref = join.get(side)
        if isinstance(ref, str) and ref not in source_columns:
            errors.append(
                f"collection {collection_name} transform {transform.id} join {side} "
                f"references unknown source column {ref}"
            )


def _verify_dynamic_key(
    collection_name: str,
    transform: NativeTransform,
    source_columns: set[str],
    errors: list[str],
) -> None:
    raw = transform.raw
    if not isinstance(raw.get("key"), dict):
        errors.append(
            f"collection {collection_name} transform {transform.id} dynamic_key_object missing key"
        )
    else:
        _verify_provenance(
            collection_name,
            transform,
            raw["key"].get("provenance"),
            source_columns,
            errors,
            label="key",
        )
    values = raw.get("values")
    if not isinstance(values, dict) or not values:
        errors.append(
            f"collection {collection_name} transform {transform.id} dynamic_key_object missing values"
        )
    else:
        for value_name, value_spec in values.items():
            spec = value_spec if isinstance(value_spec, dict) else {}
            _verify_provenance(
                collection_name,
                transform,
                spec.get("provenance"),
                source_columns,
                errors,
                label=f"value {value_name}",
            )


def _verify_polymorphic_union(
    collection_name: str,
    transform: NativeTransform,
    source_columns: set[str],
    errors: list[str],
) -> None:
    variants = transform.raw.get("variants")
    if not isinstance(variants, dict) or len(variants) < 2:
        errors.append(
            f"collection {collection_name} transform {transform.id} polymorphic_union "
            "requires at least two variants"
        )
        return
    for variant_name, variant_raw in variants.items():
        variant = variant_raw if isinstance(variant_raw, dict) else {}
        fields = variant.get("fields")
        if not isinstance(fields, dict) or not fields:
            errors.append(
                f"collection {collection_name} transform {transform.id} variant "
                f"{variant_name} missing fields"
            )
            continue
        for field_name, field_spec in fields.items():
            _verify_field_spec(
                collection_name,
                transform,
                field_spec,
                source_columns,
                errors,
                label=f"variant {variant_name} field {field_name}",
            )


def _verify_derived_tags(
    collection_name: str,
    transform: NativeTransform,
    source_columns: set[str],
    errors: list[str],
) -> None:
    tags = transform.raw.get("tags")
    if not isinstance(tags, dict) or not tags:
        errors.append(
            f"collection {collection_name} transform {transform.id} derived_tag_array missing tags"
        )
        return
    for tag, spec_raw in tags.items():
        spec = spec_raw if isinstance(spec_raw, dict) else {}
        if not spec.get("condition"):
            errors.append(
                f"collection {collection_name} transform {transform.id} tag {tag} "
                "missing condition"
            )
        _verify_provenance(
            collection_name,
            transform,
            spec.get("provenance"),
            source_columns,
            errors,
            label=f"tag {tag}",
        )


def _verify_nested_event_stream(
    collection_name: str,
    transform: NativeTransform,
    source_columns: set[str],
    errors: list[str],
) -> None:
    for key in ("event_type_field", "event_time_field"):
        ref = transform.raw.get(key)
        if not isinstance(ref, str) or ref not in source_columns:
            errors.append(
                f"collection {collection_name} transform {transform.id} {key} "
                f"references unknown source column {ref}"
            )
    payload = transform.raw.get("event_payload")
    if not isinstance(payload, dict) or not payload:
        errors.append(
            f"collection {collection_name} transform {transform.id} nested_event_stream "
            "missing event_payload"
        )
        return
    for name, ref in payload.items():
        if not isinstance(ref, str) or ref not in source_columns:
            errors.append(
                f"collection {collection_name} transform {transform.id} event_payload "
                f"{name} references unknown source column {ref}"
            )


def _verify_field_spec(
    collection_name: str,
    transform: NativeTransform,
    field_spec: Any,
    source_columns: set[str],
    errors: list[str],
    *,
    label: str,
) -> None:
    if isinstance(field_spec, str):
        if field_spec not in source_columns:
            errors.append(
                f"collection {collection_name} transform {transform.id} {label} "
                f"references unknown source column {field_spec}"
            )
        return
    if not isinstance(field_spec, dict):
        errors.append(
            f"collection {collection_name} transform {transform.id} {label} invalid field spec"
        )
        return
    source = field_spec.get("source")
    if source is not None:
        if not isinstance(source, str) or source not in source_columns:
            errors.append(
                f"collection {collection_name} transform {transform.id} {label} "
                f"references unknown source column {source}"
            )
        return
    if "expr" in field_spec:
        _verify_provenance(
            collection_name,
            transform,
            field_spec.get("provenance"),
            source_columns,
            errors,
            label=label,
        )
        return
    errors.append(
        f"collection {collection_name} transform {transform.id} {label} missing provenance"
    )


def _verify_provenance(
    collection_name: str,
    transform: NativeTransform,
    provenance: Any,
    source_columns: set[str],
    errors: list[str],
    *,
    label: str,
) -> None:
    if not isinstance(provenance, list) or not provenance:
        errors.append(
            f"collection {collection_name} transform {transform.id} {label} missing provenance"
        )
        return
    for ref in provenance:
        if not isinstance(ref, str) or ref not in source_columns:
            errors.append(
                f"collection {collection_name} transform {transform.id} {label} "
                f"references unknown source column {ref}"
            )
