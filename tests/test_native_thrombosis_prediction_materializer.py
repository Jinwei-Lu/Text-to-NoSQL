from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tend.construction.audit import audit_database_structure, validate_structure_gate
from tend.construction.designs.thrombosis_prediction import materialize_native_dataworld
from tend.source import BirdSource
from tend.construction.phase_b import NativeCoverageSlot, pipeline_blueprint

pytestmark = pytest.mark.integration


def _bird_source() -> BirdSource:
    root = Path(__file__).resolve().parents[1] / "minidev" / "MINIDEV"
    return BirdSource(root)


def _max_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_max_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_max_depth(item) for item in value), default=0)
    return 0


@pytest.fixture(scope="module")
def thrombosis_result() -> Any:
    return materialize_native_dataworld(_bird_source(), "thrombosis_prediction")


def test_thrombosis_direct_materializer_builds_deep_patient_semantic_dataworld(
    thrombosis_result: Any,
) -> None:
    result = thrombosis_result

    assert {
        "patient_clinical_profiles",
        "diagnosis_risk_panels",
        "measurement_code_bags",
    }.issubset(result.data)
    assert result.schema["db_id"] == "thrombosis_prediction"
    assert result.world_signature.startswith("sha256:")

    profile = next(
        doc for doc in result.data["patient_clinical_profiles"]
        if doc["timeline"]["lab_panels_by_year"]
    )
    year_bucket = next(iter(profile["timeline"]["lab_panels_by_year"].values()))
    assert year_bucket["panels"]
    panel = year_bucket["panels"][0]
    assert panel["measurements_by_code"]
    assert panel["panel_state"]["presence_state"] == "present"
    assert "clinical_risk_tags" in profile["risk_profile"]
    assert _max_depth(profile) >= 7


def test_thrombosis_direct_materializer_passes_native_structure_gate(
    thrombosis_result: Any,
) -> None:
    result = thrombosis_result
    audit = audit_database_structure("thrombosis_prediction", result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok is True, gate.errors
    assert audit.max_depth >= 7
    assert any("lab_panels_by_year.*.panels[]" in path for path in audit.dynamic_array_object_paths)
    assert any("events[].evidence_by_code.*" in path for path in audit.array_object_dynamic_paths)
    assert audit.presence_state_counts["present"] > 0
    assert (
        audit.presence_state_counts.get("missing", 0)
        + audit.presence_state_counts.get("null", 0)
        + audit.presence_state_counts.get("empty", 0)
        > 0
    )


def test_thrombosis_direct_manifest_exposes_semantic_features_and_blueprints(
    thrombosis_result: Any,
) -> None:
    result = thrombosis_result
    features = {feature.id: feature for feature in result.manifest.features}

    expected = {
        "patient_clinical_profiles.lab_timeline_year_matrix",
        "patient_clinical_profiles.thrombosis_diagnosis_events",
        "diagnosis_risk_panels.risk_group_patient_matrix",
    }
    assert expected.issubset(features)
    assert len(features) >= 3

    blueprint_features = [
        feature
        for feature in features.values()
        if feature.extra.get("pipeline_blueprints")
    ]
    assert len(blueprint_features) >= 3

    for feature in blueprint_features:
        for blueprint in feature.extra["pipeline_blueprints"]:
            pattern = blueprint["query_pattern"]
            assert pattern in feature.query_patterns
            assert pattern not in {
                "dynamic_key_comparison",
                "nested_event_filter",
                "missing_vs_present",
            }
            slot = NativeCoverageSlot(
                slot_id=f"thrombosis_prediction:{feature.id}:{pattern}",
                db_id="thrombosis_prediction",
                feature_id=feature.id,
                feature_type=feature.type,
                query_pattern=pattern,
                target_shape_policy="preserve",
                target_difficulty="L4",
                required_native_constructs=list(feature.required_constructs),
                anti_sql_transfer_target="requires native dynamic MongoDB traversal",
            )
            compiled = pipeline_blueprint(slot, result.manifest, snapshot=result.data)
            assert compiled["native_verification"]["ok"] is True
            assert all(stage.startswith("$") for stage in compiled["mongo_native_constructs"])
