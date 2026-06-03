from __future__ import annotations

from pathlib import Path
from typing import Any

from tend.construction.audit import audit_database_structure, validate_structure_gate
from tend.construction.designs.superhero import materialize_native_dataworld
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


def test_superhero_direct_materializer_builds_semantic_dataworld() -> None:
    result = materialize_native_dataworld(_bird_source(), "superhero")

    assert {
        "hero_dossiers",
        "publisher_universes",
        "ability_catalog",
        "alignment_rosters",
    }.issubset(result.data)
    assert result.schema["db_id"] == "superhero"
    assert result.world_signature.startswith("sha256:")

    hero = next(doc for doc in result.data["hero_dossiers"] if doc["identity"]["hero_id"] == 1)
    assert hero["profile"]["hero_name"]["value"] == "3-D Man"
    assert hero["universe"]["publisher"]["name"]["value"] == "Marvel Comics"
    assert hero["ability_matrix"]["attributes_by_name"]["intelligence"]["observations"][0]["value"] == 80
    assert hero["appearance"]["color_refs_by_role"]["eye"]["states"][0]["colour"] == "Brown"
    assert hero["query_views"][0]["power_families_by_bucket"]
    assert _max_depth(hero) >= 7


def test_superhero_direct_materializer_passes_native_structure_gate() -> None:
    result = materialize_native_dataworld(_bird_source(), "superhero")
    audit = audit_database_structure("superhero", result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok is True, gate.errors
    assert audit.max_depth >= 7
    assert any(
        "attributes_by_name.*.observations[]" in path
        for path in audit.dynamic_array_object_paths
    )
    assert any(
        "query_views[].power_families_by_bucket.*" in path
        for path in audit.array_object_dynamic_paths
    )
    assert audit.presence_state_counts["present"] > 0
    assert (
        audit.presence_state_counts.get("missing", 0)
        + audit.presence_state_counts.get("null", 0)
        + audit.presence_state_counts.get("empty", 0)
        > 0
    )
    assert result.schema["structure_gate"]["ok"] is True


def test_superhero_direct_manifest_exposes_features_and_pipeline_blueprints() -> None:
    result = materialize_native_dataworld(_bird_source(), "superhero")
    features = {feature.id: feature for feature in result.manifest.features}

    assert {
        "hero_dossiers.attribute_matrix",
        "hero_dossiers.power_family_views",
        "publisher_universes.alignment_power_matrix",
        "ability_catalog.power_to_hero_index",
        "hero_dossiers.profile_presence_states",
    }.issubset(features)
    assert features["hero_dossiers.profile_presence_states"].field == "schema_state.attributes"
    assert len(features) >= 3

    default_patterns = {
        "dynamic_key_comparison",
        "nested_event_filter",
        "missing_vs_present",
    }
    non_default_features = [
        feature
        for feature in features.values()
        if any(pattern not in default_patterns for pattern in feature.query_patterns)
    ]
    assert non_default_features
    for feature in non_default_features:
        blueprints = feature.extra.get("pipeline_blueprints")
        assert blueprints
        assert {
            blueprint["query_pattern"]
            for blueprint in blueprints
        }.intersection(set(feature.query_patterns) - default_patterns)
