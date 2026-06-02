from __future__ import annotations

from collections import Counter

from tend.construct.native_recipe import NativeFeature, NativeFeatureManifest
from tend.workflow.native_phase_b import (
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
