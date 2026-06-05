from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tend.construction.audit import audit_database_structure, validate_structure_gate
from tend.construction.designs.debit_card_specializing import materialize_native_dataworld
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
def debit_card_result() -> Any:
    return materialize_native_dataworld(_bird_source(), "debit_card_specializing")


def test_debit_card_direct_materializer_builds_semantic_fuel_world(
    debit_card_result: Any,
) -> None:
    result = debit_card_result

    assert {
        "fuel_customer_spend_profiles",
        "fuel_station_market_catalog",
        "fuel_product_payment_timeline",
    }.issubset(result.data)
    assert result.schema["db_id"] == "debit_card_specializing"
    assert result.world_signature.startswith("sha256:")

    profile = next(
        doc
        for doc in result.data["fuel_customer_spend_profiles"]
        if doc["transactions"]["events"]
        and doc["spend"]["consumption_by_month"]
        and any(
            event["merchant_context"]["station_id"] is not None
            for event in doc["transactions"]["events"]
        )
    )

    assert profile["identity"]["customer_id"] == profile["spend"]["customer_id"]
    assert profile["identity"]["currency"]["state"] == "present"
    assert profile["spend"]["consumption_by_month"]
    assert profile["transactions"]["basket_by_date"]
    assert profile["transactions"]["station_buckets_by_station_id"]
    assert profile["transactions"]["events"][0]["payment"]["amount_state"] == "present"
    assert profile["schema_state"]["external_loyalty_tier"] == "missing"
    assert _max_depth(profile) >= 7


def test_debit_card_direct_materializer_passes_native_structure_gate(
    debit_card_result: Any,
) -> None:
    audit = audit_database_structure("debit_card_specializing", debit_card_result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok is True, gate.errors
    assert audit.max_depth >= 7
    assert any(
        "consumption_by_month.*.periods[]" in path
        for path in audit.dynamic_array_object_paths
    )
    assert any(
        "events[].product_mix_by_category.*" in path
        for path in audit.array_object_dynamic_paths
    )
    assert audit.presence_state_counts["present"] > 0
    assert (
        audit.presence_state_counts.get("missing", 0)
        + audit.presence_state_counts.get("null", 0)
        + audit.presence_state_counts.get("empty", 0)
        > 0
    )


def test_debit_card_direct_manifest_exposes_semantic_features_and_blueprints(
    debit_card_result: Any,
) -> None:
    result = debit_card_result
    features = {feature.id: feature for feature in result.manifest.features}

    expected = {
        "fuel_customer_spend_profiles.consumption_by_month",
        "fuel_customer_spend_profiles.product_mix_by_category",
        "fuel_station_market_catalog.station_transactions_by_date",
        "fuel_product_payment_timeline.product_day_station_buckets",
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
            assert blueprint["pipeline"]
            assert any(
                construct in blueprint["mongo_native_constructs"]
                for construct in {"$objectToArray", "$filter", "$switch", "$ifNull"}
            )
            slot = NativeCoverageSlot(
                slot_id=f"debit_card_specializing:{feature.id}:{pattern}",
                db_id="debit_card_specializing",
                feature_id=feature.id,
                feature_type=feature.type,
                query_pattern=pattern,
                target_shape_policy="preserve",
                target_difficulty="L4",
                required_native_constructs=list(feature.required_constructs),
                anti_sql_transfer_target="requires debit-card MongoDB bucket traversal",
            )
            compiled = pipeline_blueprint(slot, result.manifest, snapshot=result.data)
            assert compiled["native_verification"]["ok"] is True
            assert all(stage.startswith("$") for stage in compiled["mongo_native_constructs"])
