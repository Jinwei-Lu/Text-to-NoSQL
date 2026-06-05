from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tend.construction.audit import audit_database_structure, validate_structure_gate
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
def community_result() -> Any:
    from tend.construction.designs.codebase_community import materialize_native_dataworld

    return materialize_native_dataworld(_bird_source(), "codebase_community")


def test_codebase_community_direct_materializer_builds_semantic_collections(
    community_result: Any,
) -> None:
    result = community_result

    assert {
        "community_threads",
        "user_reputation_profiles",
        "tag_topic_ecosystems",
    }.issubset(result.data)
    assert result.schema["db_id"] == "codebase_community"
    assert result.world_signature.startswith("sha256:")

    thread = next(
        doc for doc in result.data["community_threads"]
        if doc["answers"]["items"] and doc["taxonomy"]["tags_by_name"]
    )
    assert thread["question"]["post_id"] == thread["identity"]["question_id"]
    assert thread["question"]["status_bucket"] in {
        "accepted_closed",
        "accepted_open",
        "unanswered_closed",
        "unanswered_open",
    }
    assert thread["taxonomy"]["tags_by_name"]
    assert thread["votes"]["by_type"]
    assert thread["comments"]["by_year"]
    assert thread["answers"]["items"][0]["votes_by_type"]
    assert thread["observability"]["accepted_answer_state"] in {
        "present",
        "missing",
        "empty",
        "null",
    }
    assert _max_depth(thread) >= 7


def test_codebase_community_direct_materializer_passes_native_structure_gate(
    community_result: Any,
) -> None:
    result = community_result
    audit = audit_database_structure("codebase_community", result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok is True, gate.errors
    assert audit.max_depth >= 7
    assert any(
        "tags_by_name.*.threads[]" in path
        for path in audit.dynamic_array_object_paths
    )
    assert any(
        "items[].votes_by_type.*" in path
        for path in audit.array_object_dynamic_paths
    )
    assert audit.presence_state_counts["present"] > 0
    assert (
        audit.presence_state_counts.get("missing", 0)
        + audit.presence_state_counts.get("null", 0)
        + audit.presence_state_counts.get("empty", 0)
        > 0
    )


def test_codebase_community_manifest_exposes_semantic_features_and_blueprints(
    community_result: Any,
) -> None:
    result = community_result
    features = {feature.id: feature for feature in result.manifest.features}

    expected = {
        "community_threads.tag_thread_matrix",
        "community_threads.answer_vote_buckets",
        "user_reputation_profiles.reputation_activity_lattice",
        "tag_topic_ecosystems.topic_status_year_buckets",
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
                slot_id=f"codebase_community:{feature.id}:{pattern}",
                db_id="codebase_community",
                feature_id=feature.id,
                feature_type=feature.type,
                query_pattern=pattern,
                target_shape_policy="preserve",
                target_difficulty="L4",
                required_native_constructs=list(feature.required_constructs),
                anti_sql_transfer_target="requires native community thread/tag traversal",
            )
            compiled = pipeline_blueprint(slot, result.manifest, snapshot=result.data)
            assert compiled["native_verification"]["ok"] is True
            assert all(stage.startswith("$") for stage in compiled["mongo_native_constructs"])
