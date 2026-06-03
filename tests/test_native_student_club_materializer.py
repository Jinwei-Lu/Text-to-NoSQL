from __future__ import annotations

from pathlib import Path
from typing import Any

from tend.construct.native_audit import audit_database_structure, validate_structure_gate
from tend.construct.native_designs.student_club import materialize_native_dataworld
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


def test_student_club_direct_materializer_builds_deep_event_documents() -> None:
    result = materialize_native_dataworld(_bird_source(), "student_club")

    assert "club_event_plans_v2" in result.data
    assert "club_member_accounts_v2" in result.data
    assert "student_club" in result.schema["db_id"]
    assert result.world_signature.startswith("sha256:")

    event = next(
        doc
        for doc in result.data["club_event_plans_v2"]
        if doc["attendance"]["attendees"]
        and doc["budget_by_category"]
        and any(attendee["finance_by_category"] for attendee in doc["attendance"]["attendees"])
    )
    assert event["event"]["name"] == "Registration"
    assert event["event"]["type"] == "Registration"
    assert event["attendance"]["attendees"]
    assert event["budget_by_category"]
    assert any(bucket["budgets"] for bucket in event["budget_by_category"].values())
    assert any(
        attendee["finance_by_category"]
        for attendee in event["attendance"]["attendees"]
    )
    assert event["schema_state"]["location"] == "present"
    assert event["schema_state"]["external_rsvp_feed"] == "missing"
    assert _max_depth(event) >= 7


def test_student_club_direct_materializer_passes_native_structure_gate() -> None:
    result = materialize_native_dataworld(_bird_source(), "student_club")
    audit = audit_database_structure("student_club", result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok, gate.errors
    assert audit.collection_counts["club_event_plans_v2"] == 42
    assert audit.collection_counts["club_member_accounts_v2"] == 33
    assert audit.max_depth >= 7
    assert any(
        "budget_by_category.*.budgets[]" in path
        for path in audit.dynamic_array_object_paths
    )
    assert any(
        "attendees[].finance_by_category.*" in path
        for path in audit.array_object_dynamic_paths
    )
    assert audit.presence_state_counts["present"] > 0
    assert (
        audit.presence_state_counts.get("missing", 0)
        + audit.presence_state_counts.get("null", 0)
        + audit.presence_state_counts.get("empty", 0)
        > 0
    )


def test_student_club_direct_manifest_names_semantic_query_features() -> None:
    result = materialize_native_dataworld(_bird_source(), "student_club")

    features = {feature.id: feature for feature in result.manifest.features}
    assert len(features) >= 3
    assert "club_event_plans_v2.budget_by_category" in features
    assert "club_event_plans_v2.attendee_finance_by_category" in features
    assert "club_member_accounts_v2.member_event_timeline" in features
    assert any(
        "student_club" in pattern or "club" in pattern
        for feature in features.values()
        for pattern in feature.query_patterns
    )
    assert all(
        feature.extra.get("pipeline_blueprints")
        for feature in features.values()
        if any("student_club" in pattern or "club" in pattern for pattern in feature.query_patterns)
    )
