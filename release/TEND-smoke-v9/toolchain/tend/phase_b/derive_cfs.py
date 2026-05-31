"""Mechanical canonical_form_set derivation from query_plan (04 §II-3)."""

from __future__ import annotations

from typing import Any

DISABLED = {"$sample", "$rand", "$out", "$merge", "$function"}

PATTERN_CORE_OPS: dict[str, set[str]] = {
    "window_facet_filter": {"$setWindowFields", "$facet"},
    "simple_filter": set(),
    "lookup_join": {"$lookup"},
    "polymorphic_dispatch": {"$switch", "$type"},
    "dynamic_key_aggregation": {"$objectToArray", "$unwind", "$group"},
    "attribute_bag_unfold": {"$arrayToObject", "$map"},
    "schema_version_fallback": {"$ifNull"},
    "graph_traversal": {"$graphLookup"},
    "bucket_summary": {"$bucket"},
    "extended_reference_join": {"$lookup", "$unwind"},
    "nested_unwind": {"$unwind"},
    "set_window": {"$setWindowFields"},
}

NULL_OP = {"ifNull": "$ifNull", "type": "$type", "cond": "$cond"}

ACCUMULATOR_OPS = {
    "mean": "$avg",
    "avg": "$avg",
    "sum": "$sum",
    "count": "$sum",
    "median": "$median",
    "min": "$min",
    "max": "$max",
}

PATTERN_FORBIDDEN_OPS: dict[str, set[str]] = {
    "simple_filter": {"$group", "$setWindowFields", "$facet", "$graphLookup"},
    "lookup_join": {"$facet", "$graphLookup"},
    "set_window": {"$facet"},
}

PATTERN_ROOT_REQUIRED: dict[str, set[str]] = {
    "window_facet_filter": {"$setWindowFields", "$facet"},
    "lookup_join": {"$lookup"},
    "graph_traversal": {"$graphLookup"},
    "bucket_summary": {"$bucket"},
    "set_window": {"$setWindowFields"},
    "polymorphic_dispatch": {"$addFields", "$project"},
    "dynamic_key_aggregation": {"$unwind"},
    "attribute_bag_unfold": {"$addFields"},
    "schema_version_fallback": {"$addFields", "$project"},
    "extended_reference_join": {"$lookup"},
    "nested_unwind": {"$unwind"},
    "simple_filter": {"$match", "$project"},
}

SHAPE_ROOT_FORBIDDEN: dict[str, set[str]] = {
    "preserve": {"$unwind", "$group"},
    "augment": {"$unwind", "$group"},
    "reduce": {"$project"},
}


def accumulator_ops(aggregation: str) -> set[str]:
    return {ACCUMULATOR_OPS[aggregation]} if aggregation in ACCUMULATOR_OPS else set()


def pattern_forbidden_ops(pattern: str) -> set[str]:
    return set(PATTERN_FORBIDDEN_OPS.get(pattern, set()))


def root_required_ops(pattern: str, shape_policy: str) -> set[str]:
    required = set(PATTERN_ROOT_REQUIRED.get(pattern, set()))
    if shape_policy == "reduce":
        required.add("$group")
    return required


def root_forbidden_ops(shape_policy: str) -> set[str]:
    return set(SHAPE_ROOT_FORBIDDEN.get(shape_policy, set()))


def derive_canonical_form_set(query_plan: dict[str, Any]) -> dict[str, Any]:
    """Derive the four-tuple canonical_form_set from a query_plan."""
    pattern = query_plan["primary_pattern"]
    shape = query_plan["shape_policy"]

    must_contain = set(PATTERN_CORE_OPS.get(pattern, set()))
    for agg in query_plan.get("aggregations", []):
        must_contain |= accumulator_ops(agg)

    strat = query_plan.get("null_missing_strategy", "none")
    if strat in NULL_OP:
        must_contain.add(NULL_OP[strat])

    must_not_contain = set(DISABLED) | pattern_forbidden_ops(pattern)
    must_contain_at_root = root_required_ops(pattern, shape)
    must_not_contain_at_root = root_forbidden_ops(shape)

    return {
        "must_contain": sorted(must_contain),
        "must_not_contain": sorted(must_not_contain),
        "must_contain_at_root": sorted(must_contain_at_root),
        "must_not_contain_at_root": sorted(must_not_contain_at_root),
    }
