from __future__ import annotations

from pathlib import Path
from typing import Any

from tend.construct.native_audit import audit_database_structure, validate_structure_gate
from tend.construct.native_designs.california_schools import materialize_native_dataworld
from tend.source import BirdSource


def _bird_source() -> BirdSource:
    root = Path(__file__).resolve().parents[1] / "minidev" / "MINIDEV"
    return BirdSource(root)


def _max_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_max_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_max_depth(item) for item in value), default=0)
    return 0


def test_california_schools_direct_materializer_builds_deep_school_dataworld():
    result = materialize_native_dataworld(_bird_source(), "california_schools")

    assert "school_profiles" in result.data
    assert result.data["school_profiles"]
    assert any("school_profile" in doc for doc in result.data["school_profiles"])

    core_max_depth = max(_max_depth(doc) for doc in result.data["school_profiles"])
    assert core_max_depth >= 7

    audit = audit_database_structure("california_schools", result.data)
    assert audit.dynamic_array_object_paths
    assert audit.array_object_dynamic_paths
    assert audit.presence_state_counts.get("present", 0) > 0
    assert (
        audit.presence_state_counts.get("missing", 0)
        or audit.presence_state_counts.get("null", 0)
        or audit.presence_state_counts.get("empty", 0)
    )

    gate = validate_structure_gate(audit)
    assert gate.ok, gate.to_dict()

    assert len(result.manifest.features) >= 3
    query_patterns = {
        pattern
        for feature in result.manifest.features
        for pattern in feature.query_patterns
    }
    assert {
        "school_frpm_year_trend",
        "district_grade_span_equity_comparison",
        "county_sat_frpm_readiness_panel",
    }.issubset(query_patterns)

    blueprints = [
        blueprint
        for feature in result.manifest.features
        for blueprint in feature.extra.get("pipeline_blueprints", [])
    ]
    assert len(blueprints) >= 3
    assert {
        "school_frpm_year_trend",
        "district_grade_span_equity_comparison",
        "county_sat_frpm_readiness_panel",
    }.issubset({blueprint["query_pattern"] for blueprint in blueprints})
