from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tend.construction.audit import audit_database_structure, validate_structure_gate
from tend.construction.designs.european_football_2 import materialize_native_dataworld
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
def football_result() -> Any:
    return materialize_native_dataworld(_bird_source(), "european_football_2")


def test_european_football_direct_materializer_builds_semantic_collections(
    football_result: Any,
) -> None:
    result = football_result

    assert {
        "match_documents",
        "team_profiles",
        "player_profiles",
        "league_season_buckets",
    }.issubset(result.data)
    assert result.schema["db_id"] == "european_football_2"
    assert result.world_signature.startswith("sha256:")

    match = next(
        doc
        for doc in result.data["match_documents"]
        if doc["lineups"]["home"]["players"] and doc["lineups"]["away"]["players"]
    )
    assert match["identity"]["match_api_id"] is not None
    assert match["competition"]["season"]
    assert match["teams"]["home"]["team_api_id"] != match["teams"]["away"]["team_api_id"]
    assert match["scoreline"]["status_bucket"] in {
        "home_win",
        "away_win",
        "draw",
        "scheduled_or_missing_score",
    }
    assert match["lineups"]["home"]["formation_slots_by_role"]
    assert match["observability"]["goal_event_feed_state"] in {"present", "missing", "empty", "null"}
    assert _max_depth(match) >= 7


def test_european_football_direct_materializer_passes_native_structure_gate(
    football_result: Any,
) -> None:
    result = football_result
    audit = audit_database_structure("european_football_2", result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok is True, gate.errors
    assert audit.max_depth >= 7
    assert any(
        "matches_by_season.*.fixtures[]" in path
        for path in audit.dynamic_array_object_paths
    )
    assert any(
        "players[].rating_by_season.*" in path
        for path in audit.array_object_dynamic_paths
    )
    assert audit.presence_state_counts["present"] > 0
    assert (
        audit.presence_state_counts.get("missing", 0)
        + audit.presence_state_counts.get("null", 0)
        + audit.presence_state_counts.get("empty", 0)
        > 0
    )


def test_european_football_manifest_exposes_deep_query_features_and_blueprints(
    football_result: Any,
) -> None:
    result = football_result
    features = {feature.id: feature for feature in result.manifest.features}

    expected = {
        "team_profiles.matches_by_season",
        "match_documents.lineup_rating_context",
        "player_profiles.attribute_timeline",
        "league_season_buckets.team_table",
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
                slot_id=f"european_football_2:{feature.id}:{pattern}",
                db_id="european_football_2",
                feature_id=feature.id,
                feature_type=feature.type,
                query_pattern=pattern,
                target_shape_policy="preserve",
                target_difficulty="L4",
                required_native_constructs=list(feature.required_constructs),
                anti_sql_transfer_target="requires native football season/lineup traversal",
            )
            compiled = pipeline_blueprint(slot, result.manifest, snapshot=result.data)
            assert compiled["native_verification"]["ok"] is True
            assert all(stage.startswith("$") for stage in compiled["mongo_native_constructs"])
