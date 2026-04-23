from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

from tend_core import CanonicalFormSet, StructuredIntent, append_jsonl, load_jsonl, si_hash


DEFAULT_PERSONA_BANK = [
    {
        "persona_id": "analyst",
        "framing_style": "aggregate / compare / trend",
        "schema_awareness_baseline": "medium-high",
    },
    {
        "persona_id": "ops",
        "framing_style": "filter / monitor / count",
        "schema_awareness_baseline": "medium",
    },
    {
        "persona_id": "auditor",
        "framing_style": "verify / compare / inspect",
        "schema_awareness_baseline": "high",
    },
    {
        "persona_id": "researcher",
        "framing_style": "distribution / typology / pattern-mine",
        "schema_awareness_baseline": "medium-high",
    },
    {
        "persona_id": "end-user",
        "framing_style": "lookup / simple-filter / count",
        "schema_awareness_baseline": "low",
    },
]

DEFAULT_INTENT_TEMPLATE_LATTICE = [
    # --- temporal_trend ---
    {
        "phenomenon_class": "temporal_trend",
        "allowed_personas": ["analyst", "researcher", "auditor"],
        "pattern_families": [
            "time_window_aggregate",
            "anomaly_vs_baseline",
            "window_function_with_facet_filter",
            "window_function",
        ],
    },
    # --- null_cluster ---
    {
        "phenomenon_class": "null_cluster",
        "allowed_personas": ["ops", "analyst", "auditor"],
        "pattern_families": [
            "simple_filter",
            "filter_then_aggregate",
            "facet_split",
            "null_vs_missing_disambig",
            "coalesce_with_default",
            "filter_then_count",
        ],
    },
    # --- outlier ---
    {
        "phenomenon_class": "outlier",
        "allowed_personas": ["analyst", "ops", "auditor"],
        "pattern_families": [
            "top_k_by_aggregate",
            "anomaly_vs_baseline",
            "percentile_approximation",
            "filter_then_count",
        ],
    },
    # --- high_cardinality ---
    {
        "phenomenon_class": "high_cardinality",
        "allowed_personas": ["researcher", "analyst"],
        "pattern_families": [
            "group_then_aggregate",
            "facet_split",
            "dynamic_key_expansion",
            "project_only",
        ],
    },
    # --- cross_group_comparison ---
    {
        "phenomenon_class": "cross_group_comparison",
        "allowed_personas": ["analyst", "researcher"],
        "pattern_families": [
            "group_then_aggregate",
            "window_function_with_facet_filter",
            "lookup_join",
            "percentile_approximation",
        ],
    },
    # --- rare_event ---
    {
        "phenomenon_class": "rare_event",
        "allowed_personas": ["ops", "end-user", "auditor"],
        "pattern_families": [
            "existential_quantifier",
            "filter_then_aggregate",
            "universal_quantifier",
            "array_positional_select",
            "filter_then_count",
        ],
    },
    # --- pollution ---
    {
        "phenomenon_class": "pollution",
        "allowed_personas": ["researcher", "auditor"],
        "pattern_families": [
            "facet_split",
            "simple_filter",
            "null_vs_missing_disambig",
            "coalesce_with_default",
        ],
    },
    # --- type_drift ---
    {
        "phenomenon_class": "type_drift",
        "allowed_personas": ["researcher", "auditor"],
        "pattern_families": [
            "facet_split",
            "group_then_aggregate",
            "polymorphic_branch",
            "type_introspection",
        ],
    },
    # --- skewed_distribution ---
    {
        "phenomenon_class": "skewed_distribution",
        "allowed_personas": ["analyst", "researcher"],
        "pattern_families": [
            "percentile_approximation",
            "group_then_aggregate",
            "top_k_by_aggregate",
        ],
    },
    # --- periodic_pattern ---
    {
        "phenomenon_class": "periodic_pattern",
        "allowed_personas": ["analyst", "ops"],
        "pattern_families": [
            "time_window_aggregate",
            "window_function",
            "anomaly_vs_baseline",
        ],
    },
    # --- duplicate_cluster ---
    {
        "phenomenon_class": "duplicate_cluster",
        "allowed_personas": ["auditor", "ops"],
        "pattern_families": [
            "group_then_aggregate",
            "filter_then_count",
            "lookup_join",
        ],
    },
    # --- sparse_field ---
    {
        "phenomenon_class": "sparse_field",
        "allowed_personas": ["researcher", "auditor", "analyst"],
        "pattern_families": [
            "null_vs_missing_disambig",
            "coalesce_with_default",
            "type_introspection",
            "project_only",
        ],
    },
    # --- correlation ---
    {
        "phenomenon_class": "correlation",
        "allowed_personas": ["analyst", "researcher"],
        "pattern_families": [
            "group_then_aggregate",
            "percentile_approximation",
            "window_function",
        ],
    },
    # --- boundary_value ---
    {
        "phenomenon_class": "boundary_value",
        "allowed_personas": ["ops", "auditor", "end-user"],
        "pattern_families": [
            "simple_filter",
            "filter_then_count",
            "facet_split",
            "existential_quantifier",
        ],
    },
    # --- hierarchical_nesting ---
    {
        "phenomenon_class": "hierarchical_nesting",
        "allowed_personas": ["researcher", "analyst"],
        "pattern_families": [
            "dynamic_key_expansion",
            "array_reshape",
            "graph_recursive_deep",
            "array_positional_select",
        ],
    },
]


@dataclass(frozen=True)
class SeedTuple:
    phenomenon: dict[str, Any]
    persona: dict[str, Any]
    pattern_family: str


class SIRegistry:
    def __init__(self, registry_path: Path):
        self.registry_path = registry_path
        self._cache: set[tuple[str, str]] = {
            (row["db_id"], row["si_hash"]) for row in load_jsonl(registry_path)
        }

    def exists(self, db_id: str, si_digest: str) -> bool:
        return (db_id, si_digest) in self._cache

    def register(self, db_id: str, record_id: int, si_payload: dict[str, Any]) -> str:
        digest = si_hash(si_payload)
        if not self.exists(db_id, digest):
            append_jsonl(
                self.registry_path,
                {"db_id": db_id, "record_id": record_id, "si_hash": digest},
            )
            self._cache.add((db_id, digest))
        return digest


class IntentSeeder:
    def __init__(
        self,
        persona_bank: list[dict[str, Any]] | None = None,
        lattice: list[dict[str, Any]] | None = None,
    ):
        self.persona_bank = persona_bank or DEFAULT_PERSONA_BANK
        self.lattice = lattice or DEFAULT_INTENT_TEMPLATE_LATTICE

    def choose_seed(
        self,
        registry_payload: dict[str, Any],
        db_id: str,
        seed: int,
        offset: int,
        schema_payload: dict[str, Any] | None = None,
    ) -> SeedTuple:
        phenomena = registry_payload.get("phenomena", [])
        if not phenomena:
            raise ValueError(f"No phenomena available for db {db_id}")

        rng = Random(seed + offset)
        phenomenon = phenomena[offset % len(phenomena)]
        lattice_row = next(
            (
                row
                for row in self.lattice
                if row["phenomenon_class"] == phenomenon["phenomenon_class"]
            ),
            None,
        )
        if lattice_row is None:
            raise ValueError(f"No lattice row for phenomenon class {phenomenon['phenomenon_class']}")

        allowed = [item for item in self.persona_bank if item["persona_id"] in lattice_row["allowed_personas"]]
        persona = allowed[rng.randrange(len(allowed))]
        supported_patterns = [
            pattern
            for pattern in lattice_row["pattern_families"]
            if _pattern_supported(pattern, phenomenon["witness_evidence"], schema_payload or {})
        ]
        if not supported_patterns:
            raise ValueError(
                f"No supported pattern family for phenomenon class {phenomenon['phenomenon_class']} in db {db_id}"
            )
        pattern_family = supported_patterns[offset % len(supported_patterns)]
        return SeedTuple(phenomenon=phenomenon, persona=persona, pattern_family=pattern_family)


def build_structured_intent(
    db_id: str,
    record_id: int,
    seed_tuple: SeedTuple,
    schema_payload: dict[str, Any],
) -> StructuredIntent:
    evidence = seed_tuple.phenomenon["witness_evidence"]
    collection = evidence["collection"]
    path = evidence["path"]
    path_leaf = path.split(".")[-1]
    fields = schema_payload[collection]["fields"]
    label_field = _pick_label_field(fields)
    time_field = _pick_time_field(fields)
    category_field = _pick_category_field(fields, exclude={label_field, path_leaf})
    array_field = _pick_array_field(fields)

    secondary_collection = _pick_secondary_collection(schema_payload, exclude=collection)
    join_field = label_field

    intent = {
        "phenomenon_id": seed_tuple.phenomenon["phenomenon_id"],
        "phenomenon_class": seed_tuple.phenomenon["phenomenon_class"],
        "persona_id": seed_tuple.persona["persona_id"],
        "pattern_family": seed_tuple.pattern_family,
        "collection": collection,
        "metric_field": path,
        "field_leaf": path_leaf,
        "label_field": label_field,
        "time_field": time_field,
        "category_field": category_field,
        "array_field": array_field,
        "secondary_collection": secondary_collection,
        "join_field": join_field,
    }
    output = {
        "keys": [key for key in [label_field, category_field, path_leaf] if key],
        "shape_policy": "preserve"
        if seed_tuple.pattern_family in {
            "simple_filter", "existential_quantifier", "project_only",
            "universal_quantifier", "array_positional_select",
        }
        else "reshape",
    }
    properties = {
        "threshold": 70,
        "top_k": 3,
        "sort_direction": -1,
        "window_size": 3,
        "baseline_multiplier": 1.25,
        "default_value": 0,
    }
    noise_policies = {"applied_layers": ["literal", "semantic", "pollution"]}
    nosql_nativeness = {
        "level": _nativeness_level(seed_tuple.pattern_family),
        "operator_family": seed_tuple.pattern_family,
    }
    meta = {
        "db_id": db_id,
        "record_id": record_id,
        "schema_fingerprint_hint": collection,
    }
    return StructuredIntent(
        meta=meta,
        intent=intent,
        output=output,
        properties=properties,
        noise_policies=noise_policies,
        nosql_nativeness=nosql_nativeness,
        canonical_form_set=CanonicalFormSet(
            must_contain=(),
            must_not_contain=(),
            must_contain_at_root=(),
            must_not_contain_at_root=(),
        ),
    )


def _pick_label_field(fields: dict[str, Any]) -> str:
    for candidate in ("name", "title", "product", "customer", "city", "segment", "station", "company"):
        if candidate in fields:
            return candidate
    for name, spec in fields.items():
        if spec["type"] == "TEXT":
            return name
    return next(iter(fields))


def _pick_time_field(fields: dict[str, Any]) -> str | None:
    for candidate in ("year", "month", "day", "period", "date", "timestamp", "quarter", "season"):
        if candidate in fields:
            return candidate
    for name, spec in fields.items():
        if spec.get("role") == "time":
            return name
    for name in fields:
        if any(kw in name.lower() for kw in ("year", "date", "month", "time")):
            return name
    return None


def _pick_category_field(fields: dict[str, Any], exclude: set[str]) -> str | None:
    for candidate in (
        "city", "segment", "warehouse", "zone", "condition", "department",
        "trial_phase", "region", "status", "category", "grade", "type",
        "severity", "priority", "branch",
    ):
        if candidate in fields and candidate not in exclude:
            return candidate
    for name, spec in fields.items():
        if name not in exclude and spec["type"] == "TEXT":
            return name
    return None


def _pick_array_field(fields: dict[str, Any]) -> str | None:
    for name, spec in fields.items():
        if spec["type"] == "ARRAY":
            return name
    return None


def _pick_secondary_collection(schema_payload: dict[str, Any], exclude: str) -> str | None:
    for name in schema_payload:
        if name != exclude:
            return name
    return None


def _nativeness_level(pattern_family: str) -> str:
    if pattern_family in {"simple_filter", "existential_quantifier", "project_only", "filter_then_count"}:
        return "L1"
    if pattern_family in {
        "top_k_by_aggregate", "time_window_aggregate", "filter_then_aggregate",
        "group_then_aggregate", "null_vs_missing_disambig", "coalesce_with_default",
        "type_introspection", "array_positional_select", "universal_quantifier",
    }:
        return "L2"
    if pattern_family in {
        "facet_split", "anomaly_vs_baseline", "array_reshape",
        "lookup_join", "percentile_approximation", "window_function",
    }:
        return "L3"
    return "L4"


def _pattern_supported(
    pattern_family: str,
    evidence: dict[str, Any],
    schema_payload: dict[str, Any],
) -> bool:
    collection = evidence["collection"]
    fields = schema_payload.get(collection, {}).get("fields", {})
    has_time = _pick_time_field(fields) is not None
    has_category = _pick_category_field(fields, exclude=set()) is not None
    has_array = _pick_array_field(fields) is not None
    has_secondary = any(name != collection for name in schema_payload)

    if pattern_family in {
        "time_window_aggregate", "anomaly_vs_baseline",
        "window_function_with_facet_filter", "window_function",
        "periodic_pattern",
    }:
        return has_time
    if pattern_family in {"group_then_aggregate", "facet_split", "percentile_approximation"}:
        return has_category
    if pattern_family in {
        "existential_quantifier", "universal_quantifier",
        "array_positional_select", "array_reshape",
    }:
        return has_array
    if pattern_family == "lookup_join":
        return has_secondary or True  # self-join fallback
    return True
