from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tend.construction.audit import audit_database_structure, validate_structure_gate
from tend.construction.designs.formula_1 import materialize_native_dataworld
from tend.source import BirdSource

pytestmark = pytest.mark.integration


def _bird_source() -> BirdSource:
    root = Path(__file__).resolve().parents[1] / "minidev" / "MINIDEV"
    return BirdSource(root)


@pytest.fixture(scope="module")
def formula_1_result() -> Any:
    return materialize_native_dataworld(_bird_source(), "formula_1")


def test_formula_1_direct_materializer_builds_deep_race_weekends(formula_1_result: Any) -> None:
    result = formula_1_result

    assert "race_weekends_v2" in result.data
    race = next(doc for doc in result.data["race_weekends_v2"] if doc["_id"] == "race:19")
    assert race["calendar"]["season_year"] == 2008
    assert race["circuit"]["country"] == "Malaysia"
    assert race["sessions"]["qualifying"]["entries"]
    assert race["sessions"]["race"]["entries"]
    assert race["sessions"]["race"]["results_by_status"]["Finished"]["entries"]
    assert race["sessions"]["race"]["entries"][0]["pace_profile"]["laps_by_number"]
    assert race["schema_state"]["pit_stops"] in {"empty", "present"}
    assert race["schema_state"]["external_weather_feed"] == "missing"

    audit = audit_database_structure("formula_1", result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok is True, gate.errors
    assert audit.max_depth >= 7
    assert any("sessions.race.laps_by_number.*.running_order[]" in path for path in audit.dynamic_array_object_paths)
    assert any("pace_profile.laps_by_number.*" in path for path in audit.array_object_dynamic_paths)
    assert audit.presence_state_counts["present"] > 0
    assert audit.presence_state_counts["missing"] > 0
    assert audit.presence_state_counts["empty"] > 0


def test_formula_1_direct_manifest_names_semantic_native_features(formula_1_result: Any) -> None:
    result = formula_1_result

    features = {feature.id: feature for feature in result.manifest.features}
    assert "race_weekends_v2.results_by_status" in features
    assert "race_weekends_v2.laps_by_number" in features
    assert "race_weekends_v2.driver_pace_profiles" in features
    assert features["race_weekends_v2.results_by_status"].field == "sessions.race.results_by_status"
    assert "lap telemetry dynamic object" in features["race_weekends_v2.driver_pace_profiles"].query_patterns
