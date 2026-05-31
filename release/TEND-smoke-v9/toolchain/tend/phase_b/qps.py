"""QPS · Query Plan Sampler (Phase B upstream intent atom)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from tend.config import FIXTURES_ROOT, use_fixtures
from tend.core.llm_client import LLMClient
from tend.core.llm_response import parse_llm_json_response
from tend.prompts import loader as prompt_loader
from tend.schemas.validators import validate

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

_VALID_SCHEMA_FLEX_MODES = frozenset(
    {"none", "polymorphic", "attribute_bag", "schema_versioning", "dynamic_key"}
)

_FLEX_PATTERN_MODES: dict[str, str] = {
    "polymorphic_dispatch": "polymorphic",
    "attribute_bag_unfold": "attribute_bag",
    "schema_version_fallback": "schema_versioning",
    "dynamic_key_aggregation": "dynamic_key",
}


def _schema_flex_mode_for_pattern(pattern: str) -> str:
    return _FLEX_PATTERN_MODES.get(pattern, "none")


def _normalize_query_plan_flex_mode(qp: dict[str, Any], *, pattern_hint: str | None = None) -> None:
    """Coerce LLM/legacy flex aliases onto schema-valid enum values."""
    qp.pop("root_collection", None)
    mode = qp.get("schema_flex_mode")
    pattern = str(qp.get("primary_pattern") or pattern_hint or "")
    if mode in _VALID_SCHEMA_FLEX_MODES:
        return
    if mode in ("light", "flex", "schema_flex"):
        qp["schema_flex_mode"] = _schema_flex_mode_for_pattern(pattern)
        return
    mapped = _schema_flex_mode_for_pattern(pattern)
    qp["schema_flex_mode"] = mapped if mapped != "none" else "none"


ORCHESTRA_WINDOW_FACET_PLAN: dict[str, Any] = {
    "query_plan": {
        "primary_pattern": "window_facet_filter",
        "operator_graph": {
            "stages": [
                "$unwind",
                "$unwind",
                "$setWindowFields",
                "$group",
                "$facet",
                "$project",
                "$unwind",
                "$project",
            ],
            "dependencies": [
                "partitionBy conductor _id",
                "sortBy Performance_ID ascending",
                "parallel global median branch via $facet",
            ],
        },
        "shape_policy": "reshape",
        "null_missing_strategy": "ifNull",
        "target_difficulty": "L4",
        "schema_flex_mode": "none",
        "join_depth_target": 0,
        "aggregation_depth_target": "deep",
        "target_fields": [
            "conductor.Name",
            "orchestra.performance.Performance_ID",
            "orchestra.performance.Attendance",
        ],
        "semantic_properties": [
            {
                "id": "result_cardinality_gte_2",
                "expect": "filtered conductors >= 2 on witness D",
            },
            {
                "id": "ifNull_attendance",
                "expect": "missing Attendance coalesced to 0 before window avg",
            },
            {
                "id": "window_partition_per_conductor",
                "expect": "moving average scoped per conductor _id",
            },
            {
                "id": "global_median_tie_possible",
                "expect": "witness supports median boundary comparisons",
            },
        ],
    },
    "qps_trace": {
        "coverage_cell": "L4|structural_pipeline|schema_flex_none",
        "deficit_weight": 0.18,
        "supply_constrained": False,
        "pattern_rationale": (
            "scenario_summary emphasizes attendance trend vs peer median; "
            "embed path avoids $lookup"
        ),
    },
}


def _load_fixture_qps(db_id: str) -> dict[str, Any] | None:
    path = FIXTURES_ROOT / db_id / "qps.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def sample_query_plan(
    db_id: str,
    *,
    plan_pattern: str | None = None,
    quota_state: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    witness: dict[str, Any] | None = None,
    scenario_summary: str | None = None,
    use_fixture: bool | None = None,
) -> dict[str, Any]:
    """
    Sample a structured query_plan for Phase B synthesis.

    Coverage Controller interface: when ``quota_state`` is provided, the highest
    deficit feasible cell is preferred; otherwise deterministic defaults apply.
    """
    _ = schema, witness, scenario_summary  # reserved for future quota-aware sampling

    if use_fixture is None:
        use_fixture = use_fixtures()

    fixture = _load_fixture_qps(db_id) if use_fixture else None
    if fixture and (plan_pattern is None or fixture["query_plan"]["primary_pattern"] == plan_pattern):
        payload = fixture
    elif not use_fixture:
        client = LLMClient()
        prompt = prompt_loader.load("qps_query_plan_sampler")
        user = prompt_loader.render(
            prompt["user"],
            {
                "db_id": db_id,
                "record_id": "1001",
                "scenario_summary": scenario_summary or "",
                "schema_json": json.dumps(schema or {}, ensure_ascii=False),
                "snapshot_json": json.dumps(witness or {}, ensure_ascii=False)[:8000],
                "quota_state_json": json.dumps(quota_state or {}, ensure_ascii=False),
                "plan_pattern_hint": plan_pattern or "",
            },
        )
        response = client.call(
            "A_construct",
            f"{prompt['system']}\n\n{user}",
            seed=42,
            schema=prompt.get("output_schema"),
        )
        parsed = parse_llm_json_response(response)
        if parsed and "query_plan" in parsed:
            qp = parsed["query_plan"]
            if isinstance(qp, dict):
                qp.pop("qps_trace", None)
                qp.pop("supply_constrained", None)
                _normalize_query_plan_flex_mode(qp, pattern_hint=plan_pattern)
            if "qps_trace" not in parsed or not isinstance(parsed.get("qps_trace"), dict):
                parsed["qps_trace"] = {
                    "coverage_cell": "L1|feasible|schema_flex_none",
                    "deficit_weight": 0.1,
                    "supply_constrained": False,
                    "pattern_rationale": f"LLM-generated plan for {plan_pattern or 'auto'} on {db_id}",
                }
            try:
                validate(parsed, "query_plan")
                payload = parsed
            except ValueError:
                payload = None
        else:
            payload = None
        if payload is None:
            if db_id == "orchestra" and (plan_pattern in (None, "window_facet_filter")):
                payload = ORCHESTRA_WINDOW_FACET_PLAN
            else:
                pattern = plan_pattern or "simple_filter"
                payload = _default_plan(db_id, pattern, quota_state, schema=schema)
    elif db_id == "orchestra" and (plan_pattern in (None, "window_facet_filter")):
        payload = ORCHESTRA_WINDOW_FACET_PLAN
    else:
        pattern = plan_pattern or "simple_filter"
        if pattern not in PRIMARY_PATTERNS:
            raise ValueError(f"Unsupported primary_pattern: {pattern}")
        payload = _default_plan(db_id, pattern, quota_state, schema=schema)

    validate(payload, "query_plan")
    return payload


def _schema_root_from_schema(schema: dict[str, Any] | None) -> str | None:
    if not schema:
        return None
    skip = {"collections", "collection_names", "world_signature", "db_id", "generated_at"}
    for key, val in schema.items():
        if key.startswith("_") or key in skip:
            continue
        if isinstance(val, dict):
            return key
    return None


def _guess_numeric_field(schema: dict[str, Any] | None, collection: str) -> str | None:
    if not schema:
        return None
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


def _default_plan(
    db_id: str,
    pattern: str,
    quota_state: dict[str, Any] | None,
    *,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deficit = 0.1
    cell = f"L1|feasible|schema_flex_none"
    if quota_state:
        cells = quota_state.get("cells", [])
        if cells:
            top = max(cells, key=lambda c: c.get("deficit", 0))
            cell = top.get("cell_id", cell)
            deficit = float(top.get("deficit", deficit))

    window_l4_patterns = {"window_facet_filter", "set_window"}
    structural_l4_patterns = {"dynamic_key_aggregation"}
    flex_patterns = {"polymorphic_dispatch", "attribute_bag_unfold", "schema_version_fallback"}

    if pattern in flex_patterns:
        difficulty = "L4"
        stages = ["$match", "$unwind", "$group", "$project"]
        agg_depth = "medium"
        shape = "reshape"
        null_strat = "none"
        flex_mode = _schema_flex_mode_for_pattern(pattern)
        infeasibility = "structural_schema_flex"
    elif pattern in window_l4_patterns:
        difficulty = "L4"
        stages = ["$unwind", "$setWindowFields", "$facet", "$project"]
        agg_depth = "deep"
        shape = "reshape"
        null_strat = "ifNull"
        flex_mode = "none"
        infeasibility = "structural_pipeline"
    elif pattern in structural_l4_patterns:
        difficulty = "L4"
        stages = ["$project", "$objectToArray", "$unwind", "$group", "$project"]
        agg_depth = "deep"
        shape = "reshape"
        null_strat = "none"
        flex_mode = "none"
        infeasibility = "structural_pipeline"
    else:
        difficulty = "L1"
        stages = ["$match", "$project"]
        agg_depth = "shallow"
        shape = "preserve"
        null_strat = "none"
        flex_mode = "none"
        infeasibility = "feasible"

    root = _schema_root_from_schema(schema) or db_id
    numeric_field = _guess_numeric_field(schema, root) or "field"

    return {
        "query_plan": {
            "primary_pattern": pattern,
            "operator_graph": {"stages": stages},
            "shape_policy": shape,
            "null_missing_strategy": null_strat,
            "target_difficulty": difficulty,
            "schema_flex_mode": flex_mode,
            "join_depth_target": 0,
            "aggregation_depth_target": agg_depth,
            "target_fields": [f"{root}.{numeric_field}"],
            "semantic_properties": [
                {"id": "non_empty_result", "expect": "at least one document returned"}
            ],
        },
        "qps_trace": {
            "coverage_cell": cell,
            "deficit_weight": deficit,
            "supply_constrained": False,
            "pattern_rationale": f"fallback plan for {pattern} on {db_id}",
        },
    }
