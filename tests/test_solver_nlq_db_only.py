from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import tend.solver.workflow as solver_workflow
from tend.agents import AgentContext
from tend.solver.agents import SmartNosqlPlanner, SmartShapeProbe, SmartShapeReduce
from tend.solver.introspection import introspect_solver_database


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
                            "1": {"running_order": [{"driver": {"ref": "driver:1"}, "position": 1}]},
                            "2": {"running_order": [{"driver": {"ref": "driver:2"}, "position": 1}]},
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


class _NullLog:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def bind(self, **kwargs: Any) -> "_NullLog":
        return self


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


def test_smart_solve_nlq_db_only_derives_context_from_database(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _Result:
        db_id = "manual_formula"
        record_id = 77

        def to_json(self) -> dict[str, Any]:
            return {"db_id": self.db_id, "record_id": self.record_id, "MQL": "db.x.aggregate([])"}

    async def fake_smart_solve_record(
        wf: Any,
        record: dict[str, Any],
        schema: dict[str, Any],
        *,
        local_data: dict[str, list[dict[str, Any]]] | None = None,
        r_max: int,
        witness_k: int,
        options: Any = None,
        witness_preloaded: bool = False,
    ) -> _Result:
        captured.update(
            wf=wf,
            record=record,
            schema=schema,
            local_data=local_data,
            r_max=r_max,
            witness_k=witness_k,
            options=options,
            witness_preloaded=witness_preloaded,
        )
        return _Result()

    monkeypatch.setattr(solver_workflow, "smart_solve_record", fake_smart_solve_record)
    mongo = _SnapshotMongo(_manual_native_docs())
    wf = SimpleNamespace(ctx=SimpleNamespace(mongo=mongo))

    result = asyncio.run(
        solver_workflow.smart_solve_nlq_db(
            wf,
            db_id="manual_formula",
            nlq="List race weekends that have Finished result status buckets.",
            record_id=77,
            r_max=1,
            witness_k=2,
        )
    )

    assert result.db_id == "manual_formula"
    assert mongo.calls == [("manual_formula", 2)]
    assert captured["record"] == {
        "db_id": "manual_formula",
        "record_id": 77,
        "nl_queries": {
            "canonical": "List race weekends that have Finished result status buckets."
        },
    }
    assert "MQL" not in captured["record"]
    assert "shape_policy" not in captured["record"]
    assert captured["schema"]["collections"]["race_weekends_v2"]["schema_flex"] == "native_deep"
    assert captured["local_data"] == _manual_native_docs()
    assert captured["r_max"] == 1
    assert captured["witness_k"] == 2
    assert captured["witness_preloaded"] is True


def test_shape_reduce_preserves_dynamic_schema_less_metadata() -> None:
    mongo = _SnapshotMongo(_manual_native_docs())
    snapshot = introspect_solver_database(mongo, "manual_formula", sample_size=3)
    collection_schema = snapshot.schema["collections"]["race_weekends_v2"]
    ctx = AgentContext(
        settings=SimpleNamespace(),
        llm=SimpleNamespace(),
        log=_NullLog(),
        db_id="manual_formula",
        record_id=1,
    )

    fragment = asyncio.run(
        SmartShapeProbe().run(
            ctx,
            {"collection": "race_weekends_v2", "schema": collection_schema},
        )
    )
    reduced = asyncio.run(SmartShapeReduce().run(ctx, {"fragments": [fragment]}))

    collection = reduced["collections"]["race_weekends_v2"]
    assert collection["dynamic_key_paths"] == [
        "sessions.race.laps_by_number",
        "sessions.race.results_by_status",
        "sessions.race.results_by_status.*.entries[].pace_profile.laps_by_number",
    ]
    assert collection["dynamic_key_samples"]["sessions.race.results_by_status"] == [
        "Accident",
        "Finished",
    ]
    assert "sessions.race.results_by_status.*.entries[]" in (
        collection["dynamic_array_object_paths"]
    )
    assert "sessions.race.results_by_status.*.entries[].pace_profile.laps_by_number" in (
        collection["array_object_dynamic_paths"]
    )
    assert collection["presence_state_counts"] == {
        "empty": 1,
        "missing": 1,
        "present": 1,
    }


def test_planner_rejects_brittle_dotted_dynamic_key_paths() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": ["name", "format_key", "status"],
        },
        "shape_model": {
            "collections": {
                "card_print_dossiers": {
                    "dynamic_key_paths": ["legality.by_format"],
                }
            },
            "shape_flex_signature": ["native_deep"],
        },
        "witness_digest": {},
    }
    bad_plan = {
        "collection": "card_print_dossiers",
        "stages": [
            {
                "op": "$match",
                "note": "Badly treats a dynamic key as a fixed dotted path.",
                "stage": {
                    "$match": {
                        "legality.by_format.Modern": {"$exists": True},
                        "legality.by_format.Modern.status": "banned",
                    }
                },
            },
            {
                "op": "$project",
                "note": "Projects through the same brittle fixed dynamic key.",
                "stage": {
                    "$project": {
                        "_id": 0,
                        "name": "$print_identity.name",
                        "format_key": "Modern",
                        "status": "$legality.by_format.Modern.status",
                    }
                },
            },
        ],
        "variant_handling": [{"variant": "*", "handling": "uniform"}],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert any("dynamic-key path legality.by_format" in item for item in violations)


def test_planner_accepts_object_to_array_dynamic_key_paths() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": ["name", "format_key", "status"],
        },
        "shape_model": {
            "collections": {
                "card_print_dossiers": {
                    "dynamic_key_paths": ["legality.by_format"],
                }
            },
            "shape_flex_signature": ["native_deep"],
        },
        "witness_digest": {},
    }
    good_plan = {
        "collection": "card_print_dossiers",
        "stages": [
            {
                "op": "$project",
                "note": "Convert dynamic legality map into key/value pairs.",
                "stage": {
                    "$project": {
                        "_id": 0,
                        "name": "$print_identity.name",
                        "formats": {"$objectToArray": "$legality.by_format"},
                    }
                },
            },
            {
                "op": "$unwind",
                "note": "One row per observed legality format key.",
                "stage": {"$unwind": "$formats"},
            },
            {
                "op": "$match",
                "note": "Filter after dynamic key expansion.",
                "stage": {"$match": {"formats.k": "Modern", "formats.v.status": "banned"}},
            },
            {
                "op": "$project",
                "note": "Return requested fields.",
                "stage": {
                    "$project": {
                        "_id": 0,
                        "name": "$name",
                        "format_key": "$formats.k",
                        "status": "$formats.v.status",
                    }
                },
            },
        ],
        "variant_handling": [
            {
                "variant": "*",
                "handling": "$objectToArray expands legality.by_format dynamic keys",
            }
        ],
    }

    assert planner.check_contract(None, inputs, good_plan) == []


def test_planner_preserve_contract_treats_star_as_keep_original_document() -> None:
    planner = SmartNosqlPlanner()
    ctx = SimpleNamespace(extra={"solver_use_preserve_guard": True})
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["*"],
        },
        "shape_model": {},
        "witness_digest": {},
    }
    plan = {
        "collection": "district_market_contexts",
        "stages": [
            {
                "op": "$match",
                "note": "Filter by a schema-less dynamic key while leaving source documents intact.",
                "stage": {
                    "$match": {
                        "$expr": {
                            "$in": [
                                "POPLATEK_MESICNE",
                                {
                                    "$map": {
                                        "input": {"$objectToArray": "$accounts_by_frequency"},
                                        "as": "kv",
                                        "in": "$$kv.k",
                                    }
                                },
                            ]
                        }
                    }
                },
            },
            {
                "op": "$limit",
                "note": "Limit output without adding or removing fields.",
                "stage": {"$limit": 25},
            },
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(ctx, inputs, plan)

    assert "preserve target_fields missing from plan output: ['*']" not in violations


def test_planner_preserve_contract_allows_id_as_retained_identity_field() -> None:
    planner = SmartNosqlPlanner()
    ctx = SimpleNamespace(extra={"solver_use_preserve_guard": True})
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["_id", "native_context_bucket", "native_matching_dynamic_entries"],
        },
        "shape_model": {},
        "witness_digest": {},
    }
    plan = {
        "collection": "district_market_contexts",
        "stages": [
            {
                "op": "$addFields",
                "note": "Derive a context bucket while retaining document identity.",
                "stage": {"$addFields": {"native_context_bucket": {"$ifNull": ["$salary_band", "missing"]}}},
            },
            {
                "op": "$project",
                "note": "Return id, context, and matching dynamic key/value entries.",
                "stage": {
                    "$project": {
                        "_id": 1,
                        "native_context_bucket": 1,
                        "native_matching_dynamic_entries": {
                            "$filter": {
                                "input": {
                                    "$objectToArray": {"$ifNull": ["$accounts_by_frequency", {}]}
                                },
                                "as": "entry",
                                "cond": {"$in": ["$$entry.k", ["POPLATEK_MESICNE"]]},
                            }
                        },
                    }
                },
            },
            {
                "op": "$match",
                "note": "Drop documents without matching dynamic keys.",
                "stage": {
                    "$match": {
                        "$expr": {"$gt": [{"$size": "$native_matching_dynamic_entries"}, 0]}
                    }
                },
            },
            {
                "op": "$project",
                "note": "Keep only the native output shape.",
                "stage": {
                    "$project": {
                        "_id": 1,
                        "native_context_bucket": 1,
                        "native_matching_dynamic_entries": 1,
                    }
                },
            },
            {
                "op": "$sort",
                "note": "Stable result order.",
                "stage": {"$sort": {"_id": 1}},
            },
            {
                "op": "$limit",
                "note": "Return up to 25 documents.",
                "stage": {"$limit": 25},
            },
        ],
        "variant_handling": [
            {
                "field": "accounts_by_frequency",
                "handling": "Use objectToArray/filter to handle dynamic keys as data.",
            }
        ],
    }

    violations = planner.check_contract(ctx, inputs, plan)

    assert not any(
        violation.startswith("preserve target_fields missing")
        and "_id" in violation
        for violation in violations
    )
