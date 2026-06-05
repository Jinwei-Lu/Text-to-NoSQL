from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from tend.construction.audit import audit_database_structure, validate_structure_gate
from tend.source import ColumnSchema, DbSchema, ForeignKey

pytestmark = pytest.mark.integration


class ToxicologyFixtureSource:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._schema = DbSchema(
            db_id="toxicology",
            domain="native-fixture",
            tables=["molecule", "atom", "bond", "connected"],
            columns=[
                ColumnSchema("molecule", "molecule_id", "text"),
                ColumnSchema("molecule", "label", "text"),
                ColumnSchema("atom", "atom_id", "text"),
                ColumnSchema("atom", "molecule_id", "text"),
                ColumnSchema("atom", "element", "text"),
                ColumnSchema("bond", "bond_id", "text"),
                ColumnSchema("bond", "molecule_id", "text"),
                ColumnSchema("bond", "bond_type", "text"),
                ColumnSchema("connected", "atom_id", "text"),
                ColumnSchema("connected", "atom_id2", "text"),
                ColumnSchema("connected", "bond_id", "text"),
            ],
            foreign_keys=[
                ForeignKey("atom", "molecule_id", "molecule", "molecule_id"),
                ForeignKey("bond", "molecule_id", "molecule", "molecule_id"),
                ForeignKey("connected", "bond_id", "bond", "bond_id"),
                ForeignKey("connected", "atom_id", "atom", "atom_id"),
                ForeignKey("connected", "atom_id2", "atom", "atom_id"),
            ],
            primary_keys={
                "molecule": ["molecule_id"],
                "atom": ["atom_id"],
                "bond": ["bond_id"],
                "connected": ["atom_id", "atom_id2"],
            },
            sqlite_path=None,
        )

    def schema(self, db_id: str) -> DbSchema:
        assert db_id == "toxicology"
        return self._schema

    def connection(self, db_id: str) -> sqlite3.Connection:
        assert db_id == "toxicology"
        return self._conn


def _source() -> ToxicologyFixtureSource:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table molecule (
          molecule_id text primary key,
          label text
        );
        create table atom (
          atom_id text primary key,
          molecule_id text,
          element text
        );
        create table bond (
          bond_id text primary key,
          molecule_id text,
          bond_type text
        );
        create table connected (
          atom_id text,
          atom_id2 text,
          bond_id text,
          primary key (atom_id, atom_id2)
        );
        """
    )
    conn.executemany(
        "insert into molecule values (?, ?)",
        [
            ("TR100", "+"),
            ("TR200", "-"),
            ("TR300", None),
            ("TR400", ""),
        ],
    )
    conn.executemany(
        "insert into atom values (?, ?, ?)",
        [
            ("TR100_1", "TR100", "c"),
            ("TR100_2", "TR100", "o"),
            ("TR100_3", "TR100", "cl"),
            ("TR200_1", "TR200", "n"),
            ("TR200_2", "TR200", "c"),
            ("TR300_1", "TR300", None),
        ],
    )
    conn.executemany(
        "insert into bond values (?, ?, ?)",
        [
            ("TR100_1_2", "TR100", "-"),
            ("TR100_2_3", "TR100", "="),
            ("TR200_1_2", "TR200", "#"),
            ("TR300_1_9", "TR300", None),
        ],
    )
    conn.executemany(
        "insert into connected values (?, ?, ?)",
        [
            ("TR100_1", "TR100_2", "TR100_1_2"),
            ("TR100_2", "TR100_1", "TR100_1_2"),
            ("TR100_2", "TR100_3", "TR100_2_3"),
            ("TR100_3", "TR100_2", "TR100_2_3"),
            ("TR200_1", "TR200_2", "TR200_1_2"),
            ("TR200_2", "TR200_1", "TR200_1_2"),
        ],
    )
    conn.commit()
    return ToxicologyFixtureSource(conn)


def _max_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_max_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_max_depth(item) for item in value), default=0)
    return 0


def test_toxicology_native_materializer_builds_deep_semantic_molecule_graphs() -> None:
    from tend.construction.designs.toxicology import materialize_native_dataworld

    events: list[tuple[str, dict[str, Any]]] = []
    result = materialize_native_dataworld(
        _source(),
        "toxicology",
        event_hook=lambda event, **payload: events.append((event, payload)),
    )

    assert "molecule_graphs" in result.data
    assert result.schema["db_id"] == "toxicology"
    assert result.provenance["db_id"] == "toxicology"
    assert result.world_signature.startswith("sha256:")
    assert len(result.world_signature) == 71
    assert any(event == "toxicology_native_materialized" for event, _ in events)

    molecule = next(doc for doc in result.data["molecule_graphs"] if doc["_id"] == "TR100")
    assert molecule["identity"]["source_db"] == "toxicology"
    assert molecule["assay"]["label"]["presence_state"] == "present"
    assert molecule["chemistry"]["elements_by_symbol"]["c"]["atoms"][0]["atom_id"] == "TR100_1"
    assert molecule["assay"]["views"][0]["bonds_by_type"]["-"][0]["bond_id"] == "TR100_1_2"
    assert _max_depth(molecule) >= 4


def test_toxicology_native_materializer_passes_high_nesting_structure_gate() -> None:
    from tend.construction.designs.toxicology import materialize_native_dataworld

    result = materialize_native_dataworld(_source(), "toxicology")
    audit = audit_database_structure("toxicology", result.data)
    gate = validate_structure_gate(audit)

    assert gate.ok, gate.errors
    assert audit.max_depth >= 4
    assert audit.dynamic_array_object_paths
    assert audit.array_object_dynamic_paths
    assert audit.presence_state_counts["present"] >= 1
    assert audit.presence_state_counts["missing"] >= 1
    assert audit.presence_state_counts["null"] >= 1
    assert audit.presence_state_counts["empty"] >= 1
