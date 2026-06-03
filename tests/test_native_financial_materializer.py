from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tend.construction.audit import audit_database_structure, validate_structure_gate
from tend.construction.designs.financial import materialize_native_dataworld
from tend.source import BirdSource
from tend.construction.phase_b import NativeCoverageSlot, pipeline_blueprint


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
def financial_result() -> Any:
    return materialize_native_dataworld(_bird_source(), "financial")


def test_financial_direct_materializer_builds_deep_semantic_dataworld(financial_result: Any) -> None:
    result = financial_result

    assert {
        "account_ledgers",
        "party_relationship_graphs",
        "district_market_contexts",
        "counterparty_flow_profiles",
    }.issubset(result.data)
    assert result.schema["db_id"] == "financial"
    assert result.world_signature.startswith("sha256:")

    account = next(doc for doc in result.data["account_ledgers"] if doc["loan"]["contract"]["loan_id"] is not None)
    assert account["identity"]["account_id"] == account["ledger"]["account_id"]
    assert account["timeline"]["events_by_month"]
    assert account["cashflow"]["activity_by_month"]
    assert account["cashflow"]["activity_by_month"]["1993-09"]["entries"][0]["transaction_id"]
    assert account["loan"]["contract"]["status_bucket"] in {"running_good", "running_bad", "completed_good", "completed_bad"}
    assert account["party_graph"]["dispositions"]
    assert account["district_context"]["district_id"] is not None
    assert _max_depth(account) >= 7


def test_financial_direct_materializer_passes_native_structure_gate(financial_result: Any) -> None:
    result = financial_result
    audit = audit_database_structure("financial", result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok is True, gate.errors
    assert audit.max_depth >= 7
    assert any("activity_by_month.*.entries[]" in path for path in audit.dynamic_array_object_paths)
    assert any("monthly_flows[].operations_by_symbol.*" in path for path in audit.array_object_dynamic_paths)
    assert audit.presence_state_counts["present"] > 0
    assert (
        audit.presence_state_counts.get("missing", 0)
        + audit.presence_state_counts.get("null", 0)
        + audit.presence_state_counts.get("empty", 0)
        > 0
    )


def test_financial_direct_manifest_exposes_semantic_features_and_compilable_blueprints(
    financial_result: Any,
) -> None:
    result = financial_result
    features = {feature.id: feature for feature in result.manifest.features}

    expected = {
        "account_ledgers.monthly_activity_matrix",
        "account_ledgers.loan_repayment_schedule",
        "party_relationship_graphs.disposition_party_network",
        "district_market_contexts.account_market_segments",
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
            assert pattern not in {"dynamic_key_comparison", "nested_event_filter", "missing_vs_present"}
            slot = NativeCoverageSlot(
                slot_id=f"financial:{feature.id}:{pattern}",
                db_id="financial",
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
