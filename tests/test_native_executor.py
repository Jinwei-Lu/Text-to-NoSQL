from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tend.errors import MigrationError
from tend.source import ColumnSchema, DbSchema, ForeignKey


class FixtureSource:
    def __init__(self, conn: sqlite3.Connection, schema: DbSchema) -> None:
        self._conn = conn
        self._schema = schema

    def schema(self, db_id: str) -> DbSchema:
        assert db_id == self._schema.db_id
        return self._schema

    def connection(self, db_id: str) -> sqlite3.Connection:
        assert db_id == self._schema.db_id
        return self._conn

    def row_count(self, db_id: str, table: str) -> int:
        assert db_id == self._schema.db_id
        return int(self._conn.execute(f"select count(*) from {table}").fetchone()[0])


def _fixture_source(tmp_path: Path) -> FixtureSource:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table account (
          account_id integer primary key,
          balance real,
          district_id integer
        );
        create table loan (
          loan_id integer primary key,
          account_id integer,
          amount real,
          status text
        );
        create table card (
          card_id integer primary key,
          account_id integer,
          credit_limit real,
          used_amount real
        );
        create table trans (
          trans_id integer primary key,
          account_id integer,
          date text,
          type text,
          amount real
        );
        """
    )
    conn.executemany(
        "insert into account values (?, ?, ?)",
        [(1, 1200.0, 10), (2, 400.0, 11)],
    )
    conn.executemany(
        "insert into loan values (?, ?, ?, ?)",
        [(100, 2, 5000.0, "active")],
    )
    conn.executemany(
        "insert into card values (?, ?, ?, ?)",
        [(200, 1, 3000.0, 250.0)],
    )
    conn.executemany(
        "insert into trans values (?, ?, ?, ?, ?)",
        [
            (1, 1, "2024-01-03", "credit", 100.0),
            (2, 1, "2024-01-08", "withdrawal", 30.0),
            (3, 1, "2024-02-02", "credit", 45.0),
            (4, 2, "2024-01-10", "withdrawal", 70.0),
        ],
    )
    conn.commit()

    columns = [
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
    ]
    schema = DbSchema(
        db_id="financial",
        domain="finance",
        tables=["account", "loan", "card", "trans"],
        columns=columns,
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
        sqlite_path=tmp_path / "financial.sqlite",
    )
    return FixtureSource(conn, schema)


def _recipe() -> dict:
    return {
        "db_id": "financial",
        "recipe_version": 1,
        "design_goal": "Build native financial documents.",
        "collections": {
            "financial_entities": {
                "purpose": "Polymorphic accounts, loans, and cards.",
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
                            "card": {
                                "source_table": "card",
                                "fields": {
                                    "entity_id": {
                                        "expr": "concat('card:', card.card_id)",
                                        "provenance": ["card.card_id"],
                                    },
                                    "available": {
                                        "expr": "card.credit_limit - card.used_amount",
                                        "provenance": ["card.credit_limit", "card.used_amount"],
                                    },
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
                            },
                            "low_balance": {
                                "condition": "account.balance < 500",
                                "provenance": ["account.balance"],
                            },
                        },
                    },
                ],
            },
            "account_activity": {
                "purpose": "Dynamic monthly activity and nested transaction events.",
                "source_tables": ["account", "trans"],
                "transforms": [
                    {
                        "id": "activity_by_month",
                        "type": "dynamic_key_object",
                        "parent_table": "account",
                        "child_table": "trans",
                        "join": {"left": "account.account_id", "right": "trans.account_id"},
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
                        "join": {"left": "account.account_id", "right": "trans.account_id"},
                        "event_type_field": "trans.type",
                        "event_time_field": "trans.date",
                        "event_payload": {"amount": "trans.amount"},
                    },
                ],
            },
        },
    }


def test_execute_native_recipe_materializes_polymorphic_documents(tmp_path):
    from tend.construction.executor import execute_native_recipe
    from tend.construction.recipe import load_native_recipe

    result = execute_native_recipe(_fixture_source(tmp_path), "financial", load_native_recipe(_recipe()))

    entities = result.data["financial_entities"]
    assert [doc["entity_type"] for doc in entities] == ["account", "account", "loan", "card"]
    assert entities[0] == {
        "_id": "account:1",
        "entity_id": "account:1",
        "entity_type": "account",
        "balance": 1200.0,
    }
    assert entities[2]["risk_tags"] == ["active_debt"]
    assert entities[3]["available"] == 2750.0


def test_execute_native_recipe_materializes_dynamic_key_object_and_events(tmp_path):
    from tend.construction.executor import execute_native_recipe
    from tend.construction.recipe import load_native_recipe

    result = execute_native_recipe(_fixture_source(tmp_path), "financial", load_native_recipe(_recipe()))

    first = result.data["account_activity"][0]
    assert first["_id"] == 1
    assert first["activity_by_month"] == {
        "2024-01": {"credit": 100.0, "withdrawal": 30.0},
        "2024-02": {"credit": 45.0, "withdrawal": 0},
    }
    assert first["events"] == [
        {"event_type": "credit", "event_time": "2024-01-03", "amount": 100.0},
        {"event_type": "withdrawal", "event_time": "2024-01-08", "amount": 30.0},
        {"event_type": "credit", "event_time": "2024-02-02", "amount": 45.0},
    ]


def test_execute_native_recipe_returns_manifest_provenance_and_signature(tmp_path):
    from tend.construction.executor import execute_native_recipe
    from tend.construction.recipe import load_native_recipe

    result = execute_native_recipe(_fixture_source(tmp_path), "financial", load_native_recipe(_recipe()))

    feature_ids = {feature.id for feature in result.manifest.features}
    assert feature_ids == {
        "financial_entities.entity_union",
        "financial_entities.risk_tags",
        "account_activity.activity_by_month",
        "account_activity.events",
    }
    dynamic_feature = next(
        feature for feature in result.manifest.features if feature.id.endswith("activity_by_month")
    )
    assert dynamic_feature.type == "dynamic_key_object"
    assert "$objectToArray" in dynamic_feature.required_constructs
    assert result.provenance["account_activity.activity_by_month"]["source_columns"] == [
        "account.account_id",
        "trans.account_id",
        "trans.amount",
        "trans.date",
        "trans.type",
    ]
    assert result.world_signature.startswith("sha256:")
    assert len(result.world_signature) == 71


def test_execute_native_recipe_fails_closed_on_unsupported_expression(tmp_path):
    from tend.construction.executor import execute_native_recipe
    from tend.construction.recipe import load_native_recipe

    recipe = _recipe()
    recipe["collections"]["financial_entities"]["transforms"][0]["variants"]["account"]["fields"][
        "weird"
    ] = {"expr": "regex(account.balance)", "provenance": ["account.balance"]}

    with pytest.raises(MigrationError, match="unsupported native expression"):
        execute_native_recipe(_fixture_source(tmp_path), "financial", load_native_recipe(recipe))
