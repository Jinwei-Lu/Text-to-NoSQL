from __future__ import annotations

from copy import deepcopy
from random import Random
from typing import Any

from tend_core import WitnessWorld


NOISE_TAXONOMY = {
    "literal": [
        "trim_whitespace",
        "case_variation",
        "unicode_homoglyph",
        "encoding_artifact",
        "number_format_variation",
        "string_escape_artifact",
    ],
    "structural": [
        "missing_field",
        "extra_field",
        "key_rename",
        "nested_flatten",
        "array_to_scalar",
        "scalar_to_array",
    ],
    "semantic": [
        "outlier_boost",
        "value_swap",
        "unit_mismatch",
        "scale_shift",
        "synonym_replacement",
        "category_merge",
    ],
    "historical": [
        "stale_record",
        "duplicate_insert",
        "partial_update",
        "tombstone_residue",
        "schema_migration_ghost",
        "version_conflict",
    ],
    "pollution": [
        "null_injection",
        "empty_string",
        "sentinel_value",
        "placeholder_text",
        "html_tag_leak",
        "control_char_injection",
    ],
    "type_polymorphism": [
        "int_as_string",
        "float_as_int",
        "bool_as_string",
        "date_as_string",
        "numeric_string",
        "mixed_array_types",
    ],
}

DEFAULT_NOISE_BUDGET = {
    "literal": 0.15,
    "structural": 0.10,
    "semantic": 0.12,
    "historical": 0.08,
    "pollution": 0.12,
    "type_polymorphism": 0.10,
}


def generate_witness_world(
    template: dict[str, Any],
    schema_collections: dict[str, Any],
    db_id: str,
    noise_seed: int,
    noise_budget: dict[str, float] | None = None,
) -> tuple[WitnessWorld, dict[str, Any]]:
    rng = Random(noise_seed)
    budget = noise_budget or DEFAULT_NOISE_BUDGET
    data: dict[str, list[dict[str, Any]]] = {}
    manifest: dict[str, Any] = {
        "noise_seed": noise_seed,
        "noise_budget": budget,
        "applied_layers": list(NOISE_TAXONOMY.keys()),
        "events": [],
        "layer_summary": {layer: 0 for layer in NOISE_TAXONOMY},
    }

    for entity in template["entities"]:
        collection_name = entity["collection"]
        count = int(entity.get("cardinality", 12))
        documents: list[dict[str, Any]] = []
        for index in range(count):
            document = {"_id": index + 1}
            for field in entity["fields"]:
                maybe_value = _generate_field_value(field, rng, index)
                if maybe_value is _MISSING:
                    continue
                document[field["name"]] = maybe_value
            documents.append(document)
        _inject_noise_all_layers(documents, entity["fields"], rng, manifest, budget)
        data[collection_name] = documents

    return WitnessWorld(db_id=db_id, data=data), manifest


_MISSING = object()


def _generate_field_value(field_spec: dict[str, Any], rng: Random, index: int) -> Any:
    if field_spec.get("sparse") and rng.random() < field_spec.get("sparse_rate", 0.25):
        return _MISSING

    field_type = field_spec["type"].upper()
    nullable = field_spec.get("nullable", False)
    null_rate = field_spec.get("null_rate", 0.1 if nullable else 0.0)
    if nullable and rng.random() < null_rate:
        return None

    if field_type == "INT":
        if field_spec.get("role") == "time":
            return 2018 + index
        low, high = field_spec.get("range", [1, 100])
        return rng.randint(low, high)
    if field_type == "REAL":
        low, high = field_spec.get("range", [1.0, 100.0])
        return round(rng.uniform(low, high), 2)
    if field_type == "TEXT":
        values = field_spec.get("values")
        if values:
            return values[index % len(values)]
        return f"{field_spec['name']}_{index + 1}"
    if field_type == "BOOL":
        return bool(rng.randint(0, 1))
    if field_type == "OBJECT":
        obj: dict[str, Any] = {}
        for child in field_spec.get("fields", []):
            child_value = _generate_field_value(child, rng, index)
            if child_value is not _MISSING:
                obj[child["name"]] = child_value
        return obj
    if field_type == "ARRAY":
        items_spec = field_spec.get("items", {"type": "TEXT", "name": field_spec["name"]})
        min_items = field_spec.get("min_items", 1)
        max_items = field_spec.get("max_items", 3)
        length = min_items + rng.randint(0, max(max_items - min_items, 0))
        values: list[Any] = []
        for item_index in range(length):
            item_value = _generate_field_value(
                {**deepcopy(items_spec), "name": items_spec.get("name", field_spec["name"])},
                rng,
                item_index,
            )
            if item_value is not _MISSING:
                values.append(item_value)
        return values
    raise ValueError(f"Unsupported field type: {field_type}")


def _inject_noise_all_layers(
    documents: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    rng: Random,
    manifest: dict[str, Any],
    budget: dict[str, float],
) -> None:
    if not documents:
        return

    text_fields = [f["name"] for f in fields if f["type"].upper() == "TEXT"]
    numeric_fields = [f["name"] for f in fields if f["type"].upper() in {"INT", "REAL"}]
    nullable_fields = [f["name"] for f in fields if f.get("nullable")]
    bool_fields = [f["name"] for f in fields if f["type"].upper() == "BOOL"]
    n = len(documents)

    # --- literal layer ---
    if text_fields and rng.random() < budget.get("literal", 0.15):
        field = text_fields[0]
        doc = documents[0]
        doc[field] = f" {doc.get(field, '')} "
        _log_event(manifest, "literal", "trim_whitespace", field)
    if text_fields and len(documents) > 2 and rng.random() < budget.get("literal", 0.15):
        field = text_fields[rng.randrange(len(text_fields))]
        doc = documents[2 % n]
        val = doc.get(field, "")
        if isinstance(val, str):
            doc[field] = val.upper() if rng.random() < 0.5 else val.lower()
            _log_event(manifest, "literal", "case_variation", field)
    if numeric_fields and len(documents) > 4 and rng.random() < budget.get("literal", 0.15):
        field = numeric_fields[0]
        doc = documents[4 % n]
        val = doc.get(field, 0)
        if isinstance(val, (int, float)):
            doc[field] = str(val)
            _log_event(manifest, "literal", "number_format_variation", field)

    # --- structural layer ---
    if len(documents) > 3 and rng.random() < budget.get("structural", 0.10):
        doc = documents[3 % n]
        if text_fields:
            field = text_fields[-1]
            doc.pop(field, None)
            _log_event(manifest, "structural", "missing_field", field)
    if len(documents) > 5 and rng.random() < budget.get("structural", 0.10):
        doc = documents[5 % n]
        doc["_extra_noise_field"] = rng.randint(1, 999)
        _log_event(manifest, "structural", "extra_field", "_extra_noise_field")

    # --- semantic layer ---
    if numeric_fields and len(documents) > 3 and rng.random() < budget.get("semantic", 0.12):
        field = numeric_fields[0]
        doc = documents[-1]
        doc[field] = doc.get(field, 0) * 5
        _log_event(manifest, "semantic", "outlier_boost", field)
    if numeric_fields and len(documents) > 6 and rng.random() < budget.get("semantic", 0.12):
        field = numeric_fields[-1]
        doc_a, doc_b = documents[1], documents[2]
        doc_a[field], doc_b[field] = doc_b.get(field, 0), doc_a.get(field, 0)
        _log_event(manifest, "semantic", "value_swap", field)

    # --- historical layer ---
    if len(documents) > 4 and rng.random() < budget.get("historical", 0.08):
        doc = documents[4 % n]
        dup = deepcopy(doc)
        dup["_id"] = n + 100
        documents.append(dup)
        _log_event(manifest, "historical", "duplicate_insert", f"_id={dup['_id']}")
    if len(documents) > 7 and rng.random() < budget.get("historical", 0.08):
        doc = documents[7 % n]
        for key in list(doc.keys()):
            if key != "_id" and rng.random() < 0.3:
                break
        _log_event(manifest, "historical", "partial_update", f"doc_id={doc['_id']}")

    # --- pollution layer ---
    if nullable_fields and len(documents) > 2 and rng.random() < budget.get("pollution", 0.12):
        field = nullable_fields[0]
        documents[1][field] = None
        _log_event(manifest, "pollution", "null_injection", field)
    if text_fields and len(documents) > 5 and rng.random() < budget.get("pollution", 0.12):
        field = text_fields[0]
        documents[5 % n][field] = ""
        _log_event(manifest, "pollution", "empty_string", field)
    if text_fields and len(documents) > 8 and rng.random() < budget.get("pollution", 0.12):
        field = text_fields[-1]
        documents[8 % n][field] = "N/A"
        _log_event(manifest, "pollution", "sentinel_value", field)

    # --- type_polymorphism layer ---
    if numeric_fields and len(documents) > 1 and rng.random() < budget.get("type_polymorphism", 0.10):
        field = numeric_fields[0]
        val = documents[1].get(field, 0)
        documents[1][field] = str(val)
        _log_event(manifest, "type_polymorphism", "int_as_string", field)
    if bool_fields and len(documents) > 3 and rng.random() < budget.get("type_polymorphism", 0.10):
        field = bool_fields[0]
        val = documents[3 % n].get(field, False)
        documents[3 % n][field] = "true" if val else "false"
        _log_event(manifest, "type_polymorphism", "bool_as_string", field)


def _log_event(manifest: dict[str, Any], layer: str, noise_type: str, field: str) -> None:
    manifest["events"].append({"layer": layer, "type": noise_type, "field": field})
    manifest["layer_summary"][layer] = manifest["layer_summary"].get(layer, 0) + 1
