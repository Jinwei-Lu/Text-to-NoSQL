"""Direct-compile template registry for 12 primary patterns."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

TEMPLATES_DIR = Path(__file__).resolve().parent

PRIMARY_PATTERNS = (
    "window_facet_filter",
    "simple_filter",
    "lookup_join",
    "polymorphic_dispatch",
    "dynamic_key_aggregation",
    "attribute_bag_unfold",
    "schema_version_fallback",
    "graph_traversal",
    "bucket_summary",
    "extended_reference_join",
    "nested_unwind",
    "set_window",
)


def load_pattern_meta(pattern: str) -> dict[str, Any]:
    path = TEMPLATES_DIR / f"{pattern}.yaml"
    if not path.exists():
        raise KeyError(f"Missing template metadata for pattern: {pattern}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def compile_query_plan(
    query_plan: dict[str, Any],
    schema: dict[str, Any] | None = None,
    *,
    strategy: str = "direct",
) -> str:
    pattern = query_plan["primary_pattern"]
    if pattern not in PRIMARY_PATTERNS:
        raise ValueError(f"Unsupported primary_pattern: {pattern}")

    compiler = _COMPILERS[pattern]
    return compiler(query_plan, schema or {}, strategy=strategy)


def _schema_root_collection(schema: dict[str, Any]) -> str | None:
    skip = {"collections", "collection_names", "world_signature", "db_id", "generated_at"}
    for key, val in schema.items():
        if key.startswith("_") or key in skip:
            continue
        if isinstance(val, dict):
            return key
    return None


def _numeric_field_from_schema(schema: dict[str, Any], collection: str) -> str | None:
    coll = schema.get(collection, {})
    if not isinstance(coll, dict):
        return None
    numeric_types = {"INT", "REAL", "FLOAT", "NUMBER", "INTEGER"}
    for key, val in coll.items():
        if key.startswith("_"):
            continue
        if isinstance(val, str) and val.upper() in numeric_types:
            return key
        if isinstance(val, dict) and str(val.get("type", "")).upper() in numeric_types:
            return key
    return None


def _is_orchestra_embed_plan(query_plan: dict[str, Any]) -> bool:
    fields = query_plan.get("target_fields") or []
    return any("orchestra" in f or ".performance." in f for f in fields)


def _root_collection(schema: dict[str, Any], query_plan: dict[str, Any]) -> str:
    if "root_collection" in query_plan:
        return query_plan["root_collection"]
    root = _schema_root_collection(schema)
    if root:
        return root
    collections = schema.get("collections") or schema.get("collection_names") or []
    if isinstance(collections, dict):
        return next(iter(collections))
    if collections:
        return str(collections[0])
    target_fields = query_plan.get("target_fields") or []
    if target_fields:
        parts = target_fields[0].split(".")
        if len(parts) >= 2:
            return parts[0]
    return "documents"


def _format_pipeline(collection: str, stages: list[dict[str, Any]]) -> str:
    import json

    body = json.dumps(stages, ensure_ascii=False, indent=2)
    return f"db.{collection}.aggregate({body})"


def _compile_window_facet_filter(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    if not _is_orchestra_embed_plan(query_plan):
        return _compile_set_window(query_plan, schema, strategy=strategy)
    collection = _root_collection(schema, query_plan)
    fields = query_plan.get("target_fields") or []
    name_field = next((f.split(".")[-1] for f in fields if f.endswith(".Name")), "Name")
    perf_id_field = next(
        (f for f in fields if f.endswith(".Performance_ID")),
        "orchestra.performance.Performance_ID",
    )
    attendance_field = next(
        (f for f in fields if f.endswith(".Attendance")),
        "orchestra.performance.Attendance",
    )
    embed_path = perf_id_field.rsplit(".", 1)[0]
    embed_root = embed_path.split(".")[0]
    null_strategy = query_plan.get("null_missing_strategy", "none")
    attendance_expr: Any = f"${attendance_field}"
    if null_strategy == "ifNull":
        attendance_expr = {"$ifNull": [f"${attendance_field}", 0]}

    window_docs = [-2, 0]
    if strategy == "algebraic_rewrite":
        window_docs = [-2, 0]

    stages: list[dict[str, Any]] = [
        {"$unwind": {"path": f"${embed_root}", "preserveNullAndEmptyArrays": False}},
        {
            "$unwind": {
                "path": f"${embed_path}",
                "preserveNullAndEmptyArrays": False,
            }
        },
        {
            "$setWindowFields": {
                "partitionBy": "$_id",
                "sortBy": {perf_id_field: 1},
                "output": {
                    "moving_avg_attendance": {
                        "$avg": attendance_expr,
                        "window": {"documents": window_docs},
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id",
                name_field: {
                    "$first": {
                        "$ifNull": [f"${name_field}", "(unknown)"]
                        if null_strategy == "ifNull"
                        else f"${name_field}"
                    }
                },
                "last_window_avg": {"$last": "$moving_avg_attendance"},
            }
        },
    ]

    per_conductor_branch = [{"$project": {"_id": 0, name_field: 1, "last_window_avg": 1}}]
    global_median_branch = [
        {"$sort": {"last_window_avg": 1}},
        {"$group": {"_id": None, "vals": {"$push": "$last_window_avg"}}},
        {
            "$project": {
                "_id": 0,
                "median": {
                    "$arrayElemAt": [
                        "$vals",
                        {"$floor": {"$divide": [{"$size": "$vals"}, 2]}},
                    ]
                },
            }
        },
    ]

    facet_branches: dict[str, list[dict[str, Any]]]
    if strategy == "algebraic_rewrite":
        facet_branches = {
            "global_median": global_median_branch,
            "per_conductor": per_conductor_branch,
        }
    else:
        facet_branches = {
            "per_conductor": per_conductor_branch,
            "global_median": global_median_branch,
        }

    stages.extend(
        [
            {"$facet": facet_branches},
            {
                "$project": {
                    "kept": {
                        "$filter": {
                            "input": "$per_conductor",
                            "as": "c",
                            "cond": {
                                "$gt": [
                                    "$$c.last_window_avg",
                                    {"$arrayElemAt": ["$global_median.median", 0]},
                                ]
                            },
                        }
                    }
                }
            },
            {"$unwind": "$kept"},
            {
                "$project": {
                    "_id": 0,
                    name_field: "$kept.Name" if name_field == "Name" else f"$kept.{name_field}",
                    "last_window_avg": "$kept.last_window_avg",
                }
            },
        ]
    )
    if name_field != "Name":
        stages[-1]["$project"][name_field] = f"$kept.{name_field}"
    return _format_pipeline(collection, stages)


def _compile_simple_filter(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    field = (query_plan.get("target_fields") or ["field"])[0].split(".")[-1]
    return _format_pipeline(
        collection,
        [{"$match": {field: {"$exists": True}}}, {"$project": {"_id": 0, field: 1}}],
    )


def _compile_lookup_join(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    return _format_pipeline(
        collection,
        [
            {
                "$lookup": {
                    "from": "related",
                    "localField": "_id",
                    "foreignField": "parent_id",
                    "as": "related",
                }
            },
            {"$project": {"_id": 0, "related": 1}},
        ],
    )


def _compile_polymorphic_dispatch(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    return _format_pipeline(
        collection,
        [
            {
                "$addFields": {
                    "resolved": {
                        "$switch": {
                            "branches": [
                                {
                                    "case": {"$eq": [{"$type": "$payload"}, "object"]},
                                    "then": "$payload",
                                }
                            ],
                            "default": "$payload",
                        }
                    }
                }
            },
            {"$project": {"_id": 0, "resolved": 1}},
        ],
    )


def _compile_dynamic_key_aggregation(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    return _format_pipeline(
        collection,
        [
            {"$project": {"pairs": {"$objectToArray": "$metrics"}}},
            {"$unwind": "$pairs"},
            {"$group": {"_id": "$pairs.k", "total": {"$sum": "$pairs.v"}}},
            {"$project": {"_id": 0, "key": "$_id", "total": 1}},
        ],
    )


def _compile_attribute_bag_unfold(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    return _format_pipeline(
        collection,
        [
            {
                "$addFields": {
                    "bag_map": {
                        "$arrayToObject": {
                            "$map": {
                                "input": "$attributes",
                                "as": "a",
                                "in": {"k": "$$a.name", "v": "$$a.value"},
                            }
                        }
                    }
                }
            },
            {"$project": {"_id": 0, "bag_map": 1}},
        ],
    )


def _compile_schema_version_fallback(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    return _format_pipeline(
        collection,
        [
            {
                "$addFields": {
                    "value": {
                        "$ifNull": [
                            "$payload.v2",
                            {"$ifNull": ["$payload.v1", "$payload.legacy"]},
                        ]
                    }
                }
            },
            {"$project": {"_id": 0, "value": 1}},
        ],
    )


def _compile_graph_traversal(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    return _format_pipeline(
        collection,
        [
            {
                "$graphLookup": {
                    "from": collection,
                    "startWith": "$_id",
                    "connectFromField": "_id",
                    "connectToField": "parent_id",
                    "as": "descendants",
                }
            },
            {"$project": {"_id": 0, "descendants": 1}},
        ],
    )


def _compile_bucket_summary(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    field = (query_plan.get("target_fields") or ["score"])[0].split(".")[-1]
    return _format_pipeline(
        collection,
        [
            {
                "$bucket": {
                    "groupBy": f"${field}",
                    "boundaries": [0, 10, 20, 100],
                    "default": "other",
                    "output": {"count": {"$sum": 1}},
                }
            }
        ],
    )


def _compile_extended_reference_join(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    return _format_pipeline(
        collection,
        [
            {
                "$lookup": {
                    "from": "related",
                    "localField": "ref_id",
                    "foreignField": "_id",
                    "as": "related",
                }
            },
            {"$unwind": "$related"},
            {"$project": {"_id": 0, "related": 1}},
        ],
    )


def _compile_nested_unwind(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    return _format_pipeline(
        collection,
        [
            {"$unwind": "$items"},
            {"$unwind": "$items.tags"},
            {"$project": {"_id": 0, "tag": "$items.tags"}},
        ],
    )


def _compile_set_window(
    query_plan: dict[str, Any],
    schema: dict[str, Any],
    *,
    strategy: str = "direct",
) -> str:
    _ = strategy
    collection = _root_collection(schema, query_plan)
    fields = query_plan.get("target_fields") or []
    field = fields[0].split(".")[-1] if fields else None
    if not field or field == "field":
        field = _numeric_field_from_schema(schema, collection) or "field"
    null_strategy = query_plan.get("null_missing_strategy", "none")
    value_expr: Any = f"${field}"
    if null_strategy == "ifNull":
        value_expr = {"$ifNull": [f"${field}", 0]}
    return _format_pipeline(
        collection,
        [
            {
                "$setWindowFields": {
                    "partitionBy": "$_id",
                    "sortBy": {"_id": 1},
                    "output": {
                        "rolling_avg": {
                            "$avg": value_expr,
                            "window": {"documents": [-2, 0]},
                        }
                    },
                }
            },
            {"$project": {"_id": 0, "rolling_avg": 1}},
        ],
    )


_COMPILERS = {
    "window_facet_filter": _compile_window_facet_filter,
    "simple_filter": _compile_simple_filter,
    "lookup_join": _compile_lookup_join,
    "polymorphic_dispatch": _compile_polymorphic_dispatch,
    "dynamic_key_aggregation": _compile_dynamic_key_aggregation,
    "attribute_bag_unfold": _compile_attribute_bag_unfold,
    "schema_version_fallback": _compile_schema_version_fallback,
    "graph_traversal": _compile_graph_traversal,
    "bucket_summary": _compile_bucket_summary,
    "extended_reference_join": _compile_extended_reference_join,
    "nested_unwind": _compile_nested_unwind,
    "set_window": _compile_set_window,
}
