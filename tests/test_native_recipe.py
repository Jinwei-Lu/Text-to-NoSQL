from __future__ import annotations

from pathlib import Path

import pytest

from tend.source import ColumnSchema, DbSchema, ForeignKey


def _source_schema() -> DbSchema:
    return DbSchema(
        db_id="financial",
        domain="finance",
        tables=["account", "loan", "card", "trans"],
        columns=[
            ColumnSchema("account", "account_id", "integer"),
            ColumnSchema("account", "balance", "real"),
            ColumnSchema("account", "district_id", "integer"),
            ColumnSchema("loan", "loan_id", "integer"),
            ColumnSchema("loan", "account_id", "integer"),
            ColumnSchema("loan", "amount", "real"),
            ColumnSchema("loan", "status", "text"),
            ColumnSchema("card", "card_id", "integer"),
            ColumnSchema("card", "account_id", "integer"),
            ColumnSchema("card", "credit_limit", "real"),
            ColumnSchema("card", "used_amount", "real"),
            ColumnSchema("trans", "trans_id", "integer"),
            ColumnSchema("trans", "account_id", "integer"),
            ColumnSchema("trans", "date", "text"),
            ColumnSchema("trans", "type", "text"),
            ColumnSchema("trans", "amount", "real"),
        ],
        foreign_keys=[
            ForeignKey("loan", "account_id", "account", "account_id"),
            ForeignKey("card", "account_id", "account", "account_id"),
            ForeignKey("trans", "account_id", "account", "account_id"),
        ],
        primary_keys={
            "account": ["account_id"],
            "loan": ["loan_id"],
            "card": ["card_id"],
            "trans": ["trans_id"],
        },
        sqlite_path=Path(":memory:"),
    )


def _valid_recipe_mapping() -> dict:
    return {
        "db_id": "financial",
        "recipe_version": 1,
        "design_goal": "Build native financial document structures.",
        "collections": {
            "financial_entities": {
                "purpose": "Polymorphic financial entities.",
                "source_tables": ["account", "loan", "card"],
                "transforms": [
                    {
                        "id": "entity_union",
                        "type": "polymorphic_union",
                        "discriminator": "entity_type",
                        "variants": {
                            "account": {
                                "source_table": "account",
                                "fields": {
                                    "entity_id": {
                                        "expr": "concat('account:', account.account_id)",
                                        "provenance": ["account.account_id"],
                                    },
                                    "balance": {"source": "account.balance"},
                                },
                            },
                            "loan": {
                                "source_table": "loan",
                                "fields": {
                                    "entity_id": {
                                        "expr": "concat('loan:', loan.loan_id)",
                                        "provenance": ["loan.loan_id"],
                                    },
                                    "principal": {"source": "loan.amount"},
                                    "status": {"source": "loan.status"},
                                },
                            },
                        },
                    },
                    {
                        "id": "risk_tags",
                        "type": "derived_tag_array",
                        "target_field": "risk_tags",
                        "tags": {
                            "active_debt": {
                                "condition": "loan.status == 'active'",
                                "provenance": ["loan.status"],
                            }
                        },
                    },
                ],
            },
            "account_activity": {
                "purpose": "Dynamic monthly activity and event stream.",
                "source_tables": ["account", "trans"],
                "transforms": [
                    {
                        "id": "activity_by_month",
                        "type": "dynamic_key_object",
                        "parent_table": "account",
                        "child_table": "trans",
                        "join": {
                            "left": "account.account_id",
                            "right": "trans.account_id",
                        },
                        "target_field": "activity_by_month",
                        "key": {
                            "expr": "month(trans.date)",
                            "provenance": ["trans.date"],
                        },
                        "values": {
                            "credit": {
                                "expr": "sum(trans.amount where trans.type == 'credit')",
                                "provenance": ["trans.amount", "trans.type"],
                            },
                            "withdrawal": {
                                "expr": "sum(trans.amount where trans.type == 'withdrawal')",
                                "provenance": ["trans.amount", "trans.type"],
                            },
                        },
                    },
                    {
                        "id": "events",
                        "type": "nested_event_stream",
                        "target_field": "events",
                        "parent_table": "account",
                        "event_source_table": "trans",
                        "join": {
                            "left": "account.account_id",
                            "right": "trans.account_id",
                        },
                        "event_type_field": "trans.type",
                        "event_time_field": "trans.date",
                        "event_payload": {"amount": "trans.amount"},
                    },
                ],
            },
        },
    }


def test_load_native_recipe_accepts_supported_native_transforms():
    from tend.construction.recipe import load_native_recipe, verify_native_recipe

    recipe = load_native_recipe(_valid_recipe_mapping())
    result = verify_native_recipe(recipe, _source_schema())

    assert recipe.db_id == "financial"
    assert sorted(recipe.collections) == ["account_activity", "financial_entities"]
    assert result.ok
    assert result.errors == []
    assert result.native_feature_count == 4


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda data: data["collections"]["financial_entities"]["transforms"][0].__setitem__(
                "type", "plain_embed"
            ),
            "unsupported transform type",
        ),
        (
            lambda data: data["collections"]["financial_entities"]["transforms"][0]
            ["variants"]["account"]["fields"]["entity_id"].pop("provenance"),
            "missing provenance",
        ),
        (
            lambda data: data["collections"].pop("account_activity"),
            "requires at least one dynamic_key_object",
        ),
        (
            lambda data: data["collections"]["account_activity"]["transforms"][0].__setitem__(
                "child_table", "missing_table"
            ),
            "unknown source table",
        ),
        (
            lambda data: data["collections"]["account_activity"]["transforms"][0].pop("key"),
            "dynamic_key_object missing key",
        ),
    ],
)
def test_verify_native_recipe_rejects_invalid_native_contracts(mutation, expected):
    from tend.construction.recipe import load_native_recipe, verify_native_recipe

    data = _valid_recipe_mapping()
    mutation(data)
    recipe = load_native_recipe(data)
    result = verify_native_recipe(recipe, _source_schema())

    assert not result.ok
    assert any(expected in error for error in result.errors)


def test_native_feature_manifest_round_trips_mapping():
    from tend.construction.recipe import (
        NativeFeature,
        NativeFeatureManifest,
        dump_native_feature_manifest,
        load_native_feature_manifest,
    )

    manifest = NativeFeatureManifest(
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
                coverage={"document_count": 2, "non_empty_count": 2},
            )
        ],
    )

    dumped = dump_native_feature_manifest(manifest)
    loaded = load_native_feature_manifest(dumped)

    assert loaded.db_id == "financial"
    assert loaded.features[0].id == "account_activity.activity_by_month"
    assert loaded.features[0].required_constructs == ["$objectToArray", "$filter"]
