from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from tend.construct.migrate import MigrationPlan, migrate
from tend.source import DbSchema


class _SqliteSource:
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
        row = self._conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0])


def _schema(tables: list[str], primary_keys: dict[str, list[str]]) -> DbSchema:
    return DbSchema(
        db_id="test_db",
        domain="test",
        tables=tables,
        columns=[],
        foreign_keys=[],
        primary_keys=primary_keys,
        sqlite_path=Path(":memory:"),
    )


def _record(events: list[dict[str, Any]]):
    def hook(event: str, **fields: Any) -> None:
        events.append({"event": event, **fields})

    return hook


def test_migrate_reads_primary_key_tables_in_pk_order_and_emits_events():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, label TEXT)")
        conn.executemany(
            "INSERT INTO item (id, label) VALUES (?, ?)",
            [(2, "b"), (1, "a"), (3, "c")],
        )
        source = _SqliteSource(conn, _schema(["item"], {"item": ["id"]}))
        events: list[dict[str, Any]] = []

        data = migrate(
            source,
            "test_db",
            MigrationPlan(db_id="test_db", roots=["item"]),
            event_hook=_record(events),
        )

        assert [d["_id"] for d in data["item"]] == [1, 2, 3]
        assert events == [
            {
                "event": "migration_table_start",
                "db_id": "test_db",
                "table": "item",
                "role": "root",
                "source_row_count": 3,
                "cap": None,
            },
            {
                "event": "migration_table_done",
                "db_id": "test_db",
                "table": "item",
                "role": "root",
                "source_row_count": 3,
                "materialized_row_count": 3,
                "cap": None,
                "capped": False,
            },
        ]
    finally:
        conn.close()


def test_migrate_sorts_pkless_rows_before_cap_and_materialization():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE event (name TEXT, score INTEGER)")
        conn.executemany(
            "INSERT INTO event (name, score) VALUES (?, ?)",
            [("b", 2), ("a", 3), ("a", 1)],
        )
        source = _SqliteSource(conn, _schema(["event"], {}))
        events: list[dict[str, Any]] = []

        data = migrate(
            source,
            "test_db",
            MigrationPlan(db_id="test_db", roots=["event"], sample_caps={"event": 2}),
            event_hook=_record(events),
        )

        assert [(d["name"], d["score"]) for d in data["event"]] == [("a", 1), ("a", 3)]
        assert events[-1] == {
            "event": "migration_table_done",
            "db_id": "test_db",
            "table": "event",
            "role": "root",
            "source_row_count": 3,
            "materialized_row_count": 2,
            "cap": 2,
            "capped": True,
        }
    finally:
        conn.close()
