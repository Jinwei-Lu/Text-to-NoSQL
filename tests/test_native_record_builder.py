from __future__ import annotations

from tend.construct.native_recipe import NativeFeature, NativeFeatureManifest
from tend.workflow.native_phase_b import build_native_record, plan_native_slots


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
                id="financial_entities.risk_tags",
                type="derived_tag_array",
                collection="financial_entities",
                field="risk_tags",
                query_patterns=["tag_combination"],
                required_constructs=["$setIntersection", "$size"],
                provenance_refs=["loan.status", "account.balance"],
            ),
        ],
    )


def test_build_native_record_compiles_gold_and_attaches_native_metadata() -> None:
    manifest = _manifest()
    slot = plan_native_slots([manifest], n_records=1, seed=0)[0]

    record = build_native_record(
        slot,
        manifest,
        record_id=9001,
        world_signature="sha256:" + "1" * 64,
    )

    assert record["record_id"] == 9001
    assert record["db_id"] == "financial"
    assert record["shape_policy"] == slot.target_shape_policy
    assert record["difficulty"] == slot.target_difficulty
    assert "$objectToArray" in record["MQL"]
    assert record["native_metadata"]["feature_id"] == slot.feature_id
    assert record["native_metadata"]["feature_type"] == "dynamic_key_object"
    assert record["native_metadata"]["anti_sql_transfer"]["level"] == "strong"
    assert record["native_verification"]["ok"] is True
    assert record["nl_queries"]["canonical"]
    assert record["nl_queries"]["colloquial"]


def test_build_native_record_accepts_stubbed_nl_and_verifies_tag_compiler() -> None:
    manifest = _manifest()
    tag_slot = next(
        slot
        for slot in plan_native_slots([manifest], n_records=2, seed=0)
        if slot.feature_type == "derived_tag_array"
    )

    record = build_native_record(
        tag_slot,
        manifest,
        record_id=9002,
        canonical_nl="Find financial entities whose risk tag set overlaps the target tag set.",
        colloquial_nl="Show entities with those risk tags.",
    )

    assert "$setIntersection" in record["MQL"]
    assert record["nl_queries"] == {
        "canonical": "Find financial entities whose risk tag set overlaps the target tag set.",
        "colloquial": "Show entities with those risk tags.",
    }
    assert record["native_metadata"]["query_pattern"] == "tag_combination"
    assert record["native_verification"]["ok"] is True
