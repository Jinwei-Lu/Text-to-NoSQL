from __future__ import annotations

from pathlib import Path

from tend.construction.audit import audit_database_structure, validate_structure_gate
from tend.construction.designs.card_games import materialize_native_dataworld
from tend.source import BirdSource


def _bird_source() -> BirdSource:
    root = Path(__file__).resolve().parents[1] / "minidev" / "MINIDEV"
    return BirdSource(root)


def test_card_games_direct_materializer_builds_deep_card_print_dossiers() -> None:
    result = materialize_native_dataworld(_bird_source(), "card_games")

    assert "card_print_dossiers" in result.data
    assert "set_release_ecosystems" in result.data
    card = result.data["card_print_dossiers"][0]
    assert card["print_identity"]["uuid"]
    assert isinstance(card["legality"]["by_format"], dict)
    assert isinstance(card["localization"]["translations_by_language"], dict)
    assert isinstance(card["rulings"]["by_year"], dict)
    assert card["schema_state"]["legalities"] in {"present", "empty"}
    assert card["schema_state"]["digital_faces"] == "missing"

    audit = audit_database_structure("card_games", result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok is True, gate.errors
    assert audit.max_depth >= 7
    assert any("legality.by_format.*.events[]" in path for path in audit.dynamic_array_object_paths)
    assert any(
        "views[].status_by_format.*" in path
        for path in audit.array_object_dynamic_paths
    )
    assert audit.presence_state_counts["present"] > 0
    assert audit.presence_state_counts["missing"] > 0


def test_card_games_direct_manifest_exposes_semantic_query_blueprints() -> None:
    result = materialize_native_dataworld(_bird_source(), "card_games")

    features = {feature.id: feature for feature in result.manifest.features}
    assert "card_print_dossiers.legality_by_format" in features
    assert "card_print_dossiers.rulings_by_year" in features
    assert "set_release_ecosystems.cards_by_rarity" in features
    assert len(features) >= 4
    for feature in features.values():
        if feature.query_patterns and feature.query_patterns[0] not in {
            "dynamic_key_comparison",
            "missing_vs_present",
        }:
            assert feature.extra.get("pipeline_blueprints")
