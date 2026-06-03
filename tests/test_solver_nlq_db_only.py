from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import tend.solver.inputs as solver_inputs
from tend.solver.introspection import introspect_solver_database


def _flat_collection_docs() -> dict[str, list[dict[str, Any]]]:
    return {
        "orders": [
            {
                "_id": "order:1",
                "customer_name": "Alice",
                "total": 120,
                "address": {"city": "Brno", "zip": "60200"},
                "line_items": [
                    {"sku": "A1", "qty": 2, "price": 30},
                    {"sku": "A2", "qty": 1, "price": 60},
                ],
            },
            {
                "_id": "order:2",
                "customer_name": "Bob",
                "total": 80,
                "address": {"city": "Praha", "zip": "11000"},
                "line_items": [{"sku": "B2", "qty": 1, "price": 80}],
            },
        ]
    }


def _manual_native_docs() -> dict[str, list[dict[str, Any]]]:
    return {
        "race_weekends_v2": [
            {
                "_id": "race:1",
                "calendar": {"race_name": "Australian GP", "season_year": 2008},
                "sessions": {
                    "race": {
                        "results_by_status": {
                            "Finished": {
                                "count": 2,
                                "entries": [
                                    {
                                        "driver": {"ref": "driver:1"},
                                        "points": 10,
                                        "pace_profile": {
                                            "laps_by_number": {
                                                "1": {"position": 1, "milliseconds": 92123}
                                            }
                                        },
                                    },
                                    {"driver": {"ref": "driver:2"}, "points": 8},
                                ],
                            },
                            "Accident": {"count": 1, "entries": []},
                        },
                        "laps_by_number": {
                            "1": {
                                "running_order": [
                                    {"driver": {"ref": "driver:1"}, "position": 1}
                                ]
                            },
                            "2": {
                                "running_order": [
                                    {"driver": {"ref": "driver:2"}, "position": 1}
                                ]
                            },
                        },
                    }
                },
                "schema_state": {
                    "pit_stops": "empty",
                    "external_weather_feed": "missing",
                    "race_results": "present",
                },
            }
        ]
    }


class _SnapshotMongo:
    def __init__(self, docs: dict[str, list[dict[str, Any]]]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, int]] = []

    def snapshot_database(self, db_id: str, sample_size: int) -> dict[str, list[dict[str, Any]]]:
        self.calls.append((db_id, sample_size))
        return {collection: rows[:sample_size] for collection, rows in self.docs.items()}


def test_introspection_extracts_deep_native_shape_from_database_snapshot() -> None:
    mongo = _SnapshotMongo(_manual_native_docs())

    snapshot = introspect_solver_database(mongo, "manual_formula", sample_size=3)

    assert mongo.calls == [("manual_formula", 3)]
    collection = snapshot.schema["collections"]["race_weekends_v2"]
    assert collection["fields"]["sessions.race.results_by_status"] == "object"
    assert collection["fields"]["sessions.race.laps_by_number"] == "object"
    assert "sessions.race.results_by_status" in collection["dynamic_key_paths"]
    assert "sessions.race.laps_by_number" in collection["dynamic_key_paths"]
    assert "sessions.race.results_by_status.*.entries[]" in collection[
        "dynamic_array_object_paths"
    ]
    assert "sessions.race.results_by_status.*.entries[].pace_profile.laps_by_number" in (
        collection["array_object_dynamic_paths"]
    )
    assert collection["dynamic_key_samples"]["sessions.race.results_by_status"] == [
        "Accident",
        "Finished",
    ]
    assert collection["presence_state_counts"] == {
        "empty": 1,
        "missing": 1,
        "present": 1,
    }
    assert snapshot.local_data == _manual_native_docs()


def test_flat_collection_introspection_does_not_invent_schema_flex() -> None:
    mongo = _SnapshotMongo(_flat_collection_docs())

    snapshot = introspect_solver_database(mongo, "flat_shop", sample_size=5)
    collection = snapshot.schema["collections"]["orders"]

    assert collection["schema_flex"] == "none"
    assert collection["dynamic_key_paths"] == []
    assert collection["presence_state_counts"] == {}
    assert snapshot.local_data == _flat_collection_docs()


def test_nlq_db_solver_input_derives_context_from_database() -> None:
    mongo = _SnapshotMongo(_manual_native_docs())
    wf = SimpleNamespace(ctx=SimpleNamespace(mongo=mongo))

    runtime_input = asyncio.run(
        solver_inputs.build_nlq_db_solver_input(
            wf,
            db_id="manual_formula",
            nlq="List race weekends that have Finished result status buckets.",
            record_id=77,
            sample_size=2,
        )
    )

    assert mongo.calls == [("manual_formula", 2)]
    assert runtime_input.record == {
        "db_id": "manual_formula",
        "record_id": 77,
        "nl_queries": {
            "canonical": "List race weekends that have Finished result status buckets."
        },
    }
    assert "MQL" not in runtime_input.record
    assert "shape_policy" not in runtime_input.record
    assert runtime_input.schema["collections"]["race_weekends_v2"]["schema_flex"] == "native_deep"
    assert runtime_input.local_data == _manual_native_docs()


def test_solver_input_helpers_are_publicly_reexported() -> None:
    from tend.solver import build_nlq_db_solver_input, load_solver_release_inputs

    assert build_nlq_db_solver_input is solver_inputs.build_nlq_db_solver_input
    assert load_solver_release_inputs is solver_inputs.load_solver_release_inputs


def test_witness_digest_remains_available_for_non_eg_baselines() -> None:
    digest = solver_inputs.build_witness_digest(
        {"orders": [{"name": "Alice", "nested": {"city": "Brno"}}]},
        witness_k=1,
    )

    assert digest["orders"]["sample_count"] == 1
    assert digest["orders"]["string_values_in_sample"] == {
        "name": ["Alice"],
        "nested.city": ["Brno"],
    }
