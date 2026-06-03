from __future__ import annotations

from tend.construction.recipe import NativeFeature, NativeFeatureManifest
from tend.construction.verify import (
    classify_anti_sql_transfer,
    verify_native_record,
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
            ),
            NativeFeature(
                id="financial_entities.entity_union",
                type="polymorphic_collection",
                collection="financial_entities",
                field="entity_type",
                query_patterns=["subtype_field_dispatch"],
                required_constructs=["$switch"],
            ),
            NativeFeature(
                id="financial_entities.risk_tags",
                type="derived_tag_array",
                collection="financial_entities",
                field="risk_tags",
                query_patterns=["tag_combination"],
                required_constructs=["$setIntersection", "$size"],
            ),
            NativeFeature(
                id="account_activity.events",
                type="nested_event_stream",
                collection="account_activity",
                field="events",
                query_patterns=["nested_event_filter"],
                required_constructs=["$filter"],
            ),
            NativeFeature(
                id="account_activity.optional_overdraft",
                type="missing_vs_present",
                collection="account_activity",
                field="overdraft_limit",
                query_patterns=["missing_vs_present"],
                required_constructs=["$type"],
            ),
        ],
    )


def _record(feature_id: str, mql: str) -> dict:
    return {
        "record_id": 1001,
        "db_id": "financial",
        "native_metadata": {"feature_id": feature_id},
        "MQL": mql,
    }


def test_verify_native_record_accepts_required_native_dynamic_key_mql() -> None:
    record = _record(
        "account_activity.activity_by_month",
        (
            'db.account_activity.aggregate([{"$addFields":{"month_kv":{"$objectToArray":'
            '"$activity_by_month"}}},{"$addFields":{"large_months":{"$filter":{"input":'
            '"$month_kv","as":"month","cond":{"$gt":["$$month.v.credit",0]}}}}}])'
        ),
    )

    result = verify_native_record(record, _manifest())

    assert result.ok
    assert result.errors == []
    assert result.anti_sql_transfer.level == "strong"


def test_verify_native_record_rejects_dynamic_key_without_object_to_array() -> None:
    record = _record(
        "account_activity.activity_by_month",
        'db.account_activity.aggregate([{"$project":{"value":"$activity_by_month"}}])',
    )

    result = verify_native_record(record, _manifest())

    assert not result.ok
    assert any("$objectToArray" in error for error in result.errors)


def test_verify_native_record_rejects_unwind_group_nested_event_transfer() -> None:
    record = _record(
        "account_activity.events",
        (
            'db.account_activity.aggregate([{"$unwind":"$events"},{"$group":{"_id":"$_id",'
            '"total":{"$sum":"$events.amount"}}}])'
        ),
    )

    result = verify_native_record(record, _manifest())

    assert not result.ok
    assert any("shape-preserving $filter" in error for error in result.errors)


def test_verify_native_record_accepts_switch_dispatch_and_missing_field_checks() -> None:
    dispatch = _record(
        "financial_entities.entity_union",
        (
            'db.financial_entities.aggregate([{"$addFields":{"native_branch":{"$switch":'
            '{"branches":[{"case":{"$eq":["$entity_type","loan"]},"then":"loan"}],'
            '"default":"other"}}}}])'
        ),
    )
    missing = _record(
        "account_activity.optional_overdraft",
        (
            'db.account_activity.aggregate([{"$match":{"overdraft_limit":{"$exists":false}}},'
            '{"$addFields":{"missing_kind":{"$type":"$overdraft_limit"}}}])'
        ),
    )

    assert verify_native_record(dispatch, _manifest()).ok
    assert verify_native_record(missing, _manifest()).ok


def test_verify_native_record_rejects_missing_feature_without_missing_expression() -> None:
    record = _record(
        "account_activity.optional_overdraft",
        'db.account_activity.aggregate([{"$project":{"overdraft_limit":1}}])',
    )

    result = verify_native_record(record, _manifest())

    assert not result.ok
    assert any("missing-field" in error for error in result.errors)


def test_classify_anti_sql_transfer_levels() -> None:
    weak = classify_anti_sql_transfer(
        {"MQL": 'db.account_activity.aggregate([{"$unwind":"$events"},{"$group":{"_id":"$_id"}}])'}
    )
    medium = classify_anti_sql_transfer(
        {"MQL": 'db.account_activity.aggregate([{"$addFields":{"x":{"$filter":{"input":"$events","as":"e","cond":true}}}}])'}
    )
    strong = classify_anti_sql_transfer(
        {
            "MQL": (
                'db.account_activity.aggregate([{"$addFields":{"month_kv":{"$objectToArray":'
                '"$activity_by_month"}}},{"$addFields":{"large_months":{"$filter":{"input":'
                '"$month_kv","as":"month","cond":{"$gt":["$$month.v.credit",0]}}}}}])'
            ),
            "native_metadata": {"anti_sql_transfer_target": "strong"},
            "native_verification": {"ok": True},
        }
    )

    assert weak.level == "weak"
    assert medium.level == "medium"
    assert strong.level == "strong"
