from __future__ import annotations

from collections import Counter

from tend.construction.recipe import NativeFeature, NativeFeatureManifest
from tend.construction.phase_b import (
    _compatible_dynamic_key_metric_pairs,
    build_native_record,
    dynamic_key_comparison,
    nested_event_filter,
    plan_native_slots,
    subtype_field_dispatch,
    tag_combination,
)


def _manifest() -> NativeFeatureManifest:
    return NativeFeatureManifest(
        db_id="financial",
        features=[
            NativeFeature(
                id="account_activity.activity_by_month",
                type="dynamic_key_object",
                collection="account_activity",
                field="activity_by_month",
                query_patterns=["dynamic_key_comparison"],
                required_constructs=["$objectToArray", "$filter"],
                provenance_refs=["trans.date", "trans.amount", "trans.type"],
            ),
            NativeFeature(
                id="financial_entities.entity_union",
                type="polymorphic_collection",
                collection="financial_entities",
                field="entity_type",
                query_patterns=["subtype_field_dispatch"],
                required_constructs=["$switch"],
                provenance_refs=["account.account_id", "loan.loan_id"],
            ),
            NativeFeature(
                id="financial_entities.risk_tags",
                type="derived_tag_array",
                collection="financial_entities",
                field="risk_tags",
                query_patterns=["tag_combination"],
                required_constructs=["$setIntersection", "$size"],
                provenance_refs=["loan.status", "account.balance"],
            ),
            NativeFeature(
                id="account_activity.events",
                type="nested_event_stream",
                collection="account_activity",
                field="events",
                query_patterns=["nested_event_filter"],
                required_constructs=["$filter"],
                provenance_refs=["trans.type", "trans.amount"],
            ),
            NativeFeature(
                id="account_activity.optional_overdraft",
                type="missing_vs_present",
                collection="account_activity",
                field="overdraft_limit",
                query_patterns=["missing_vs_present"],
                required_constructs=["$type"],
                provenance_refs=["account.account_id"],
            ),
        ],
    )


def test_plan_native_slots_balances_feature_types_from_manifest() -> None:
    slots = plan_native_slots([_manifest()], n_records=5, seed=11)

    assert len(slots) == 5
    assert Counter(slot.feature_type for slot in slots) == {
        "dynamic_key_object": 1,
        "polymorphic_collection": 1,
        "derived_tag_array": 1,
        "nested_event_stream": 1,
        "missing_vs_present": 1,
    }
    assert all(slot.db_id == "financial" for slot in slots)
    assert all(slot.feature_id for slot in slots)
    assert all(slot.required_native_constructs for slot in slots)


def test_plan_native_slots_respects_records_per_db_cap() -> None:
    other = NativeFeatureManifest(
        db_id="sales",
        features=[
            NativeFeature(
                id="orders.monthly_totals",
                type="dynamic_key_object",
                collection="orders",
                field="monthly_totals",
                query_patterns=["dynamic_key_comparison"],
                required_constructs=["$objectToArray", "$filter"],
            )
        ],
    )

    slots = plan_native_slots(
        [_manifest(), other],
        n_records=4,
        seed=3,
        records_per_db={"financial": 2, "sales": 1},
    )

    assert Counter(slot.db_id for slot in slots) == {"financial": 2, "sales": 1}


def test_plan_native_slots_covers_features_before_repeating_sparse_types() -> None:
    manifest = NativeFeatureManifest(
        db_id="card_games",
        features=[
            NativeFeature(
                id=f"card_print_dossiers.dynamic_{index}",
                type="dynamic_key_object",
                collection="card_print_dossiers",
                field=f"dynamic_{index}",
                query_patterns=["dynamic_key_comparison"],
                required_constructs=["$objectToArray"],
            )
            for index in range(4)
        ]
        + [
            NativeFeature(
                id="card_print_dossiers.digital_faces_presence",
                type="missing_vs_present",
                collection="card_print_dossiers",
                field="schema_state.digital_faces",
                query_patterns=["missing_vs_present"],
                required_constructs=["$ifNull"],
            )
        ],
    )

    slots = plan_native_slots([manifest], n_records=5, seed=0)

    assert len({slot.feature_id for slot in slots}) == 5


def test_plan_native_slots_balances_features_for_records_per_db_release_runs() -> None:
    manifest = NativeFeatureManifest(
        db_id="card_games",
        features=[
            NativeFeature(
                id=f"card_print_dossiers.dynamic_{index}",
                type="dynamic_key_object",
                collection="card_print_dossiers",
                field=f"dynamic_{index}",
                query_patterns=["dynamic_key_comparison"],
                required_constructs=["$objectToArray"],
            )
            for index in range(4)
        ]
        + [
            NativeFeature(
                id="card_print_dossiers.digital_faces_presence",
                type="missing_vs_present",
                collection="card_print_dossiers",
                field="schema_state.digital_faces",
                query_patterns=["missing_vs_present"],
                required_constructs=["$ifNull"],
            )
        ],
    )

    slots = plan_native_slots([manifest], n_records=10, seed=0, records_per_db=10)

    assert Counter(slot.feature_id for slot in slots) == {
        "card_print_dossiers.dynamic_0": 2,
        "card_print_dossiers.dynamic_1": 2,
        "card_print_dossiers.dynamic_2": 2,
        "card_print_dossiers.dynamic_3": 2,
        "card_print_dossiers.digital_faces_presence": 2,
    }


def test_plan_native_slots_rotates_feature_query_patterns_for_repeated_features() -> None:
    manifest = NativeFeatureManifest(
        db_id="formula_1",
        features=[
            NativeFeature(
                id="race_weekends_v2.laps_by_number",
                type="dynamic_key_object",
                collection="race_weekends_v2",
                field="sessions.race.laps_by_number",
                query_patterns=[
                    "lap running order dynamic object",
                    "lap telemetry dynamic object",
                    "dynamic_key_comparison",
                ],
                required_constructs=["$objectToArray", "$unwind"],
            )
        ],
    )

    slots = plan_native_slots([manifest], n_records=3, seed=0)

    assert [slot.query_pattern for slot in slots] == [
        "lap running order dynamic object",
        "lap telemetry dynamic object",
        "dynamic_key_comparison",
    ]


def test_plan_native_slots_rotates_patterns_by_feature_usage_not_global_slot_index() -> None:
    manifest = NativeFeatureManifest(
        db_id="formula_1",
        features=[
            NativeFeature(
                id="race_weekends_v2.laps_by_number",
                type="dynamic_key_object",
                collection="race_weekends_v2",
                field="sessions.race.laps_by_number",
                query_patterns=["lap running order dynamic object", "lap telemetry dynamic object"],
                required_constructs=["$objectToArray"],
            ),
            NativeFeature(
                id="race_weekends_v2.qualifying_windows",
                type="nested_event_stream",
                collection="race_weekends_v2",
                field="sessions.qualifying.elimination_windows",
                query_patterns=["qualifying elimination window"],
                required_constructs=["$filter"],
            ),
        ],
    )

    slots = plan_native_slots([manifest], n_records=4, seed=0)
    dynamic_slots = [
        slot for slot in slots if slot.feature_id == "race_weekends_v2.laps_by_number"
    ]

    assert [slot.query_pattern for slot in dynamic_slots[:2]] == [
        "lap running order dynamic object",
        "lap telemetry dynamic object",
    ]


def test_build_native_record_generates_snapshot_semantic_variants_for_repeated_feature() -> None:
    manifest = NativeFeatureManifest(
        db_id="financial",
        features=[
            NativeFeature(
                id="account_ledgers.monthly_activity_matrix",
                type="dynamic_key_object",
                collection="account_ledgers",
                field="cashflow.activity_by_month",
                query_patterns=["monthly_account_cashflow_matrix"],
                required_constructs=["$objectToArray", "$unwind"],
                provenance_refs=["trans.date", "trans.amount"],
            )
        ],
    )
    snapshot = {
        "account_ledgers": [
            {
                "_id": 1,
                "cashflow": {
                    "activity_by_month": {
                        "2024-01": {"amount_total": 120, "transaction_count": 3},
                        "2024-02": {"amount_total": 80, "transaction_count": 2},
                    }
                },
            },
            {
                "_id": 2,
                "cashflow": {
                    "activity_by_month": {
                        "2024-02": {"amount_total": 220, "transaction_count": 5},
                        "2024-03": {"amount_total": 40, "transaction_count": 1},
                    }
                },
            },
        ]
    }
    slots = plan_native_slots([manifest], n_records=6, seed=0, records_per_db=6)

    records = [
        build_native_record(slot, manifest, snapshot=snapshot)
        for slot in slots
    ]

    assert len({record["MQL"] for record in records}) >= 5
    assert all(record["native_verification"]["ok"] for record in records)
    assert any("2024-02" in record["MQL"] for record in records)
    assert any("amount_total" in record["MQL"] for record in records)
    assert all(
        record["native_metadata"]["compiler"] == "semantic_snapshot_variant"
        for record in records
    )


def test_nested_event_object_field_prefers_shape_aware_blueprint() -> None:
    manifest = NativeFeatureManifest(
        db_id="formula_1",
        features=[
            NativeFeature(
                id="race_weekends_v2.qualifying_windows",
                type="nested_event_stream",
                collection="race_weekends_v2",
                field="sessions.qualifying.elimination_windows",
                query_patterns=["qualifying elimination window"],
                required_constructs=["$filter", "$ifNull", "$size"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "qualifying elimination window",
                            "intent": "filter Q1 entries from an object-backed qualifying window",
                            "pipeline": [
                                {
                                    "$project": {
                                        "q1_entries": {
                                            "$ifNull": [
                                                "$sessions.qualifying.elimination_windows.q1_slowest_five.entries",
                                                [],
                                            ]
                                        }
                                    }
                                },
                                {
                                    "$addFields": {
                                        "native_filtered_events": {
                                            "$filter": {
                                                "input": "$q1_entries",
                                                "as": "entry",
                                                "cond": {"$ne": ["$$entry.driver.ref", None]},
                                            }
                                        }
                                    }
                                },
                            ],
                            "mongo_native_constructs": ["$filter", "$ifNull", "$size"],
                        }
                    ]
                },
            )
        ],
    )
    snapshot = {
        "race_weekends_v2": [
            {
                "_id": "race:1",
                "sessions": {
                    "qualifying": {
                        "elimination_windows": {
                            "q1_slowest_five": {
                                "entries": [{"driver": {"ref": "senna"}}],
                            }
                        }
                    }
                },
            }
        ]
    }
    slot = plan_native_slots([manifest], n_records=1, seed=0, records_per_db=1)[0]

    record = build_native_record(slot, manifest, snapshot=snapshot)

    assert record["native_metadata"]["compiler"] == "pipeline_blueprint"
    assert "q1_slowest_five.entries" in record["MQL"]


def test_nested_event_type_time_variants_use_observed_type_time_pairs() -> None:
    manifest = NativeFeatureManifest(
        db_id="thrombosis_prediction",
        features=[
            NativeFeature(
                id="patient_clinical_profiles.thrombosis_diagnosis_events",
                type="nested_event_stream",
                collection="patient_clinical_profiles",
                field="timeline.events",
                query_patterns=["thrombosis_event_evidence_filter"],
                required_constructs=["$filter", "$ifNull", "$size"],
            )
        ],
    )
    snapshot = {
        "patient_clinical_profiles": [
            {"_id": 1, "timeline": {"events": [{"event_type": "A", "event_time": "2020-01-01"}]}},
            {"_id": 2, "timeline": {"events": [{"event_type": "A", "event_time": "2019-01-01"}]}},
            {"_id": 3, "timeline": {"events": [{"event_type": "B", "event_time": "2010-01-01"}]}},
        ]
    }
    slot = plan_native_slots([manifest], n_records=3, seed=0, records_per_db=3)[2]

    record = build_native_record(slot, manifest, snapshot=snapshot)

    assert '"$$event.event_type","B"' in record["MQL"]
    assert '"$$event.event_time","2010-01-01"' in record["MQL"]
    assert '"$$event.event_time","2020-01-01"' not in record["MQL"]
    assert record["native_verification"]["ok"] is True


def test_dynamic_key_metric_variants_require_observed_key_metric_pairs() -> None:
    manifest = NativeFeatureManifest(
        db_id="codebase_community",
        features=[
            NativeFeature(
                id="user_reputation_profiles.reputation_activity_lattice",
                type="dynamic_key_object",
                collection="user_reputation_profiles",
                field="activity.by_year",
                query_patterns=["community_user_reputation_activity_lattice"],
                required_constructs=["$objectToArray", "$unwind"],
            )
        ],
    )
    feature = manifest.features[0]
    snapshot = {
        "user_reputation_profiles": [
            {
                "_id": 1,
                "activity": {
                    "by_year": {
                        "2014": {
                            "question_accepted": {"score_total": 20},
                            "comment": {"score_total": 7},
                        }
                    }
                },
            },
            {
                "_id": 2,
                "activity": {
                    "by_year": {
                        "2014": {
                            "question_accepted": {"score_total": 12},
                            "comment": {"score_total": 3},
                        }
                    }
                },
            },
            {
                "_id": 3,
                "activity": {
                    "by_year": {
                        "2009": {
                            "rare_answer": {"score_total": 1},
                        }
                    }
                },
            },
        ]
    }

    pairs = _compatible_dynamic_key_metric_pairs(
        feature,
        snapshot,
        keys=["2014", "2009"],
        metrics=[
            "question_accepted.score_total",
            "comment.score_total",
            "rare_answer.score_total",
        ],
    )
    assert ("2009", "question_accepted.score_total") not in {
        (key, metric) for key, metric, _ in pairs
    }
    assert ("2009", "rare_answer.score_total") in {
        (key, metric) for key, metric, _ in pairs
    }

    record = build_native_record(
        plan_native_slots([manifest], n_records=1, seed=0, records_per_db=1)[0],
        manifest,
        snapshot=snapshot,
    )

    assert '"native_dynamic_entries.k":"2009"' not in record["MQL"] or (
        "rare_answer.score_total" in record["MQL"]
    )
    assert record["native_verification"]["ok"] is True


def test_build_native_record_uses_feature_pipeline_blueprint_for_semantic_pattern() -> None:
    manifest = NativeFeatureManifest(
        db_id="formula_1",
        features=[
            NativeFeature(
                id="race_weekends_v2.laps_by_number",
                type="dynamic_key_object",
                collection="race_weekends_v2",
                field="sessions.race.laps_by_number",
                query_patterns=["lap running order dynamic object"],
                required_constructs=["$objectToArray", "$unwind", "$group"],
                provenance_refs=["lapTimes", "results"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "lap running order dynamic object",
                            "intent": "count lap leaders from the race weekend lap index",
                            "pipeline": [
                                {
                                    "$project": {
                                        "laps": {
                                            "$objectToArray": "$sessions.race.laps_by_number"
                                        }
                                    }
                                },
                                {"$unwind": "$laps"},
                                {"$unwind": "$laps.v.running_order"},
                                {"$match": {"laps.v.running_order.position": 1}},
                                {
                                    "$group": {
                                        "_id": "$laps.v.running_order.driver.ref",
                                        "led_laps": {"$sum": 1},
                                    }
                                },
                                {"$sort": {"led_laps": -1, "_id": 1}},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind", "$group"],
                        }
                    ]
                },
            )
        ],
    )
    slot = plan_native_slots([manifest], n_records=1, seed=0)[0]

    record = build_native_record(slot, manifest, world_signature="sha256:" + "1" * 64)

    assert record["native_query_pattern"] == "lap running order dynamic object"
    assert "$objectToArray" in record["MQL"]
    assert "$sessions.race.laps_by_number" in record["MQL"]
    assert "$group" in record["MQL"]
    assert record["native_metadata"]["compiler"] == "pipeline_blueprint"
    assert record["native_verification"]["ok"] is True


def test_missing_vs_present_targets_explicit_presence_state_values() -> None:
    manifest = NativeFeatureManifest(
        db_id="toxicology",
        features=[
            NativeFeature(
                id="molecule_graphs.supplemental_assay_presence_state",
                type="missing_vs_present",
                collection="molecule_graphs",
                field="assay.supplemental_panel.presence_state",
                query_patterns=["missing_vs_present"],
                required_constructs=["$ifNull"],
            )
        ],
    )
    slot = plan_native_slots([manifest], n_records=1, seed=0)[0]

    record = build_native_record(slot, manifest)

    assert "$ifNull" in record["MQL"]
    assert '"missing"' in record["MQL"]
    assert "$type" not in record["MQL"]
    assert record["native_verification"]["ok"] is True


def test_compilers_emit_native_mql_constructs_and_verification_payloads() -> None:
    manifest = _manifest()
    slots = {slot.feature_type: slot for slot in plan_native_slots([manifest], 5, seed=0)}

    dynamic = dynamic_key_comparison(slots["dynamic_key_object"], manifest)
    dispatch = subtype_field_dispatch(slots["polymorphic_collection"], manifest)
    tags = tag_combination(slots["derived_tag_array"], manifest)
    events = nested_event_filter(slots["nested_event_stream"], manifest)

    assert "$objectToArray" in dynamic["MQL"]
    assert "$activity_by_month" in dynamic["MQL"]
    assert "$switch" in dispatch["MQL"]
    assert "$entity_type" in dispatch["MQL"]
    assert "$setIntersection" in tags["MQL"]
    assert "$size" in tags["MQL"]
    assert "$filter" in events["MQL"]
    assert "$events" in events["MQL"]
    assert all(out["native_verification"]["ok"] for out in [dynamic, dispatch, tags, events])
    assert all(out["provenance_refs"] for out in [dynamic, dispatch, tags, events])
