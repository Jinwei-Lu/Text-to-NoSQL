from __future__ import annotations

from random import Random
from typing import Any

from tend_core import SchemaSpec, schema_signature


F_TOPOLOGY_FEATURES = (
    "nested_3_deep",
    "sparse_embedded",
    "polymorphic_value",
    "dynamic_key",
    "cross_collection_ref",
    "array_of_objects",
    "mixed_type_field",
)


def compose_schema(
    template: dict[str, Any],
    db_id: str,
    topology_seed: int | None = None,
) -> SchemaSpec:
    collections: dict[str, Any] = {}
    for entity in template["entities"]:
        collections[entity["collection"]] = {
            "type": "OBJECT",
            "fields": {field["name"]: _field_to_schema(field) for field in entity["fields"]},
        }

    hints = template.get("f_topology_hints", {})
    if topology_seed is not None:
        _inject_topology_features(collections, hints, topology_seed)

    return SchemaSpec(db_id=db_id, domain_id=template["domain_id"], collections=collections)


def _inject_topology_features(
    collections: dict[str, Any],
    hints: dict[str, Any],
    seed: int,
) -> None:
    rng = Random(seed)
    first_collection = next(iter(collections.values()))
    fields = first_collection.get("fields", {})

    if hints.get("nested_objects", rng.random() < 0.3):
        fields["_nested_meta"] = {
            "type": "OBJECT",
            "nullable": False,
            "sparse": False,
            "fields": {
                "level1": {
                    "type": "OBJECT",
                    "nullable": False,
                    "sparse": False,
                    "fields": {
                        "level2": {
                            "type": "OBJECT",
                            "nullable": False,
                            "sparse": False,
                            "fields": {
                                "deep_value": {"type": "INT", "nullable": True, "sparse": False},
                            },
                        },
                    },
                },
            },
        }

    if hints.get("sparse_embedded", rng.random() < 0.4):
        fields["_sparse_notes"] = {
            "type": "TEXT",
            "nullable": True,
            "sparse": True,
        }

    if hints.get("polymorphic_value", rng.random() < 0.25):
        fields["_poly_field"] = {
            "type": "TEXT",
            "nullable": True,
            "sparse": False,
            "_polymorphic_hint": True,
        }

    if hints.get("dynamic_key", rng.random() < 0.2):
        fields["_dynamic_attrs"] = {
            "type": "OBJECT",
            "nullable": True,
            "sparse": True,
            "fields": {},
            "_dynamic_hint": True,
        }

    if hints.get("cross_collection_ref", False) and len(collections) > 1:
        other_name = [k for k in collections if k != next(iter(collections))][0]
        fields["_ref_id"] = {
            "type": "TEXT",
            "nullable": True,
            "sparse": False,
            "_ref_collection": other_name,
        }

    if hints.get("array_of_objects", rng.random() < 0.3):
        fields["_tags"] = {
            "type": "ARRAY",
            "nullable": False,
            "sparse": False,
            "items": {
                "type": "OBJECT",
                "nullable": False,
                "sparse": False,
                "fields": {
                    "key": {"type": "TEXT", "nullable": False, "sparse": False},
                    "value": {"type": "INT", "nullable": True, "sparse": False},
                },
            },
        }

    if hints.get("mixed_type_field", rng.random() < 0.2):
        fields["_mixed"] = {
            "type": "TEXT",
            "nullable": True,
            "sparse": False,
            "_mixed_type_hint": True,
        }


def compute_schema_complexity_profile(schema: SchemaSpec) -> dict[str, Any]:
    counters: dict[str, int] = {
        "collection_count": len(schema.collections),
        "field_count": 0,
        "object_count": 0,
        "array_count": 0,
        "max_depth": 1,
        "numeric_field_count": 0,
        "text_field_count": 0,
        "bool_field_count": 0,
        "nullable_field_count": 0,
        "sparse_field_count": 0,
    }
    for collection in schema.collections.values():
        _walk_schema_node(collection, counters, depth=1)

    active_topology: list[str] = []
    for collection in schema.collections.values():
        fields = collection.get("fields", {})
        if counters["max_depth"] >= 3:
            active_topology.append("nested_3_deep")
        if counters["sparse_field_count"] > 0:
            active_topology.append("sparse_embedded")
        if any(f.get("_polymorphic_hint") for f in fields.values()):
            active_topology.append("polymorphic_value")
        if any(f.get("_dynamic_hint") for f in fields.values()):
            active_topology.append("dynamic_key")
        if any(f.get("_ref_collection") for f in fields.values()):
            active_topology.append("cross_collection_ref")
        if any(
            f.get("type") == "ARRAY" and f.get("items", {}).get("type") == "OBJECT"
            for f in fields.values()
        ):
            active_topology.append("array_of_objects")
        if any(f.get("_mixed_type_hint") for f in fields.values()):
            active_topology.append("mixed_type_field")
        break  # only check first collection

    counters["f_topology_active"] = sorted(set(active_topology))  # type: ignore[assignment]
    return counters


def schema_audit_payload(schema: SchemaSpec) -> dict[str, Any]:
    publish_schema = schema.to_publish_dict()
    return {
        "schema_signature": schema_signature(publish_schema),
        "schema_complexity_profile": compute_schema_complexity_profile(schema),
    }


def _field_to_schema(field_spec: dict[str, Any]) -> dict[str, Any]:
    field_type = field_spec["type"].upper()
    node: dict[str, Any] = {
        "type": field_type,
        "nullable": bool(field_spec.get("nullable", False)),
        "sparse": bool(field_spec.get("sparse", False)),
    }
    if field_type == "OBJECT":
        node["fields"] = {
            child["name"]: _field_to_schema(child) for child in field_spec.get("fields", [])
        }
    elif field_type == "ARRAY":
        items = field_spec.get("items", {"type": "TEXT"})
        node["items"] = _field_to_schema(items)
    return node


def _walk_schema_node(node: dict[str, Any], counters: dict[str, int], depth: int) -> None:
    counters["max_depth"] = max(counters["max_depth"], depth)
    fields = node.get("fields", {})
    for child in fields.values():
        counters["field_count"] += 1
        field_type = child["type"]
        if child.get("nullable"):
            counters["nullable_field_count"] += 1
        if child.get("sparse"):
            counters["sparse_field_count"] += 1
        if field_type in {"INT", "REAL"}:
            counters["numeric_field_count"] += 1
        elif field_type == "TEXT":
            counters["text_field_count"] += 1
        elif field_type == "BOOL":
            counters["bool_field_count"] += 1
        elif field_type == "OBJECT":
            counters["object_count"] += 1
            _walk_schema_node(child, counters, depth + 1)
        elif field_type == "ARRAY":
            counters["array_count"] += 1
            item_node = child.get("items", {})
            if item_node.get("type") == "OBJECT":
                _walk_schema_node(item_node, counters, depth + 1)
