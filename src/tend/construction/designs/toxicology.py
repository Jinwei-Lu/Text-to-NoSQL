from __future__ import annotations

from collections import defaultdict
from typing import Any

from tend.execution import world_signature as compute_world_signature

from ..executor import NativeExecutionResult
from ..recipe import NativeFeature, NativeFeatureManifest
from ..recipe import NativeMigrationRecipe
from .common import collection, expr, join, recipe, source as field_source, transform

DESIGN_VERSION = 1
MODULE_REF = __name__


def build_native_recipe(source: Any, db_id: str) -> NativeMigrationRecipe:
    source.schema(db_id)
    return recipe(
        db_id,
        version=DESIGN_VERSION,
        design_goal=(
            "Represent molecular graphs with element-keyed atom counts, bond-type "
            "buckets, and polymorphic atom/bond components."
        ),
        collections=[
            collection(
                "molecule_graphs",
                purpose="Molecule documents with dynamic atom and bond summaries.",
                source_tables=["molecule", "atom", "bond", "connected"],
                transforms=[
                    transform(
                        "atom_counts_by_element",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="molecule",
                        child_table="atom",
                        join=join("molecule.molecule_id", "atom.molecule_id"),
                        target_field="atom_counts_by_element",
                        key=expr("atom.element", "atom.element"),
                        values={
                            "atom_count": expr("count(atom.atom_id)", "atom.atom_id"),
                            "molecule_count": expr(
                                "count(distinct atom.molecule_id)",
                                "atom.molecule_id",
                            ),
                        },
                    ),
                    transform(
                        "molecule_label_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="molecule_tags",
                        tags={
                            "carcinogenic": {
                                "condition": "molecule.label == '+'",
                                "provenance": ["molecule.label"],
                            },
                            "non_carcinogenic": {
                                "condition": "molecule.label == '-'",
                                "provenance": ["molecule.label"],
                            },
                        },
                    ),
                    transform(
                        "bond_counts_by_type",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="molecule",
                        child_table="bond",
                        join=join("molecule.molecule_id", "bond.molecule_id"),
                        target_field="bond_counts_by_type",
                        key=expr("bond.bond_type", "bond.bond_type"),
                        values={
                            "bond_count": expr("count(bond.bond_id)", "bond.bond_id"),
                        },
                    ),
                ],
            ),
            collection(
                "molecular_components",
                purpose="Typed atom and bond components for graph-native queries.",
                source_tables=["atom", "bond", "connected"],
                transforms=[
                    transform(
                        "component_union",
                        "polymorphic_union",
                        module_ref=MODULE_REF,
                        discriminator="component_type",
                        variants={
                            "atom": {
                                "source_table": "atom",
                                "fields": {
                                    "component_id": expr(
                                        "concat('atom:', atom.atom_id)",
                                        "atom.atom_id",
                                    ),
                                    "molecule_id": field_source("atom.molecule_id"),
                                    "element": field_source("atom.element"),
                                },
                            },
                            "bond": {
                                "source_table": "bond",
                                "fields": {
                                    "component_id": expr(
                                        "concat('bond:', bond.bond_id)",
                                        "bond.bond_id",
                                    ),
                                    "molecule_id": field_source("bond.molecule_id"),
                                    "bond_type": field_source("bond.bond_type"),
                                },
                            },
                        },
                    )
                ],
            ),
        ],
    )


def materialize_native_dataworld(
    source: Any,
    db_id: str,
    event_hook: Any = None,
) -> NativeExecutionResult:
    source.schema(db_id)
    conn = source.connection(db_id)
    molecules = _read_table(conn, "molecule")
    atoms = _read_table(conn, "atom")
    bonds = _read_table(conn, "bond")
    connected = _read_table(conn, "connected")

    atoms_by_molecule = _group_by(atoms, "molecule_id")
    bonds_by_molecule = _group_by(bonds, "molecule_id")
    atom_by_id = {str(row.get("atom_id")): row for row in atoms if row.get("atom_id") is not None}
    connections_by_bond = _group_by(connected, "bond_id")
    connections_by_atom = _connections_by_atom(connected)

    molecule_docs = [
        _molecule_doc(
            molecule,
            atoms_by_molecule.get(molecule.get("molecule_id"), []),
            bonds_by_molecule.get(molecule.get("molecule_id"), []),
            atom_by_id,
            connections_by_bond,
            connections_by_atom,
            db_id,
        )
        for molecule in molecules
    ]
    component_docs = _component_docs(
        atoms,
        bonds,
        atom_by_id,
        connections_by_bond,
        connections_by_atom,
        db_id,
    )
    data = {
        "molecule_graphs": molecule_docs,
        "molecular_components": component_docs,
    }
    manifest = _manifest()
    schema = _schema(db_id, data)
    provenance = _provenance(db_id)
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "toxicology_native_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            molecule_count=len(molecule_docs),
            world_signature=signature,
        )
    return NativeExecutionResult(
        data=data,
        schema=schema,
        manifest=manifest,
        provenance=provenance,
        world_signature=signature,
    )


def _read_table(conn: Any, table: str) -> list[dict[str, Any]]:
    cursor = conn.execute(f"select * from {table}")
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return dict(grouped)


def _connections_by_atom(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        atom_id = row.get("atom_id")
        if atom_id is not None:
            grouped[str(atom_id)].append(row)
    return dict(grouped)


def _molecule_doc(
    molecule: dict[str, Any],
    atoms: list[dict[str, Any]],
    bonds: list[dict[str, Any]],
    atom_by_id: dict[str, dict[str, Any]],
    connections_by_bond: dict[Any, list[dict[str, Any]]],
    connections_by_atom: dict[str, list[dict[str, Any]]],
    db_id: str,
) -> dict[str, Any]:
    molecule_id = str(molecule.get("molecule_id"))
    label = molecule.get("label")
    elements_by_symbol = _elements_by_symbol(atoms, connections_by_atom, atom_by_id)
    bond_buckets = _bonds_by_type(bonds, connections_by_bond, atom_by_id)
    return {
        "_id": molecule_id,
        "identity": {
            "source_db": db_id,
            "molecule_id": molecule_id,
            "label": label,
            "label_presence_state": _presence_state(label),
        },
        "assay": {
            "label": {
                "value": label,
                "presence_state": _presence_state(label),
                "interpretation": _label_interpretation(label),
            },
            "supplemental_panel": {
                "presence_state": "missing",
                "reason": "source toxicology schema provides label assay but no external assay panel table",
            },
            "tags": _assay_tags(label),
            "views": [
                {
                    "assay_id": "carcinogenicity_label",
                    "label_presence_state": _presence_state(label),
                    "outcome_by_label": _label_bucket(label),
                    "bonds_by_type": bond_buckets,
                }
            ],
        },
        "chemistry": {
            "component_presence": {
                "atoms": _presence_state(atoms),
                "bonds": _presence_state(bonds),
                "connected_pairs": _presence_state(
                    [
                        connection
                        for bond in bonds
                        for connection in connections_by_bond.get(bond.get("bond_id"), [])
                    ]
                ),
                "label": _presence_state(label),
            },
            "composition": {
                "atom_total": len(atoms),
                "bond_total": len(bonds),
                "element_total": len(elements_by_symbol),
                "bond_type_total": len(bond_buckets),
            },
            "elements_by_symbol": elements_by_symbol,
            "adjacency_by_atom_id": _adjacency_by_atom_id(
                atoms,
                connections_by_atom,
                atom_by_id,
                connections_by_bond,
            ),
        },
        "provenance": {
            "source_tables": ["molecule", "atom", "bond", "connected"],
            "source_columns": [
                "molecule.molecule_id",
                "molecule.label",
                "atom.atom_id",
                "atom.molecule_id",
                "atom.element",
                "bond.bond_id",
                "bond.molecule_id",
                "bond.bond_type",
                "connected.atom_id",
                "connected.atom_id2",
                "connected.bond_id",
            ],
        },
    }


def _elements_by_symbol(
    atoms: list[dict[str, Any]],
    connections_by_atom: dict[str, list[dict[str, Any]]],
    atom_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for atom in atoms:
        grouped[_dynamic_key(atom.get("element"), "unknown_element")].append(atom)
    return {
        symbol: {
            "presence_state": _presence_state(group),
            "atom_count": len(group),
            "atoms": [
                {
                    "atom_id": atom.get("atom_id"),
                    "element": atom.get("element"),
                    "element_presence_state": _presence_state(atom.get("element")),
                    "neighbors": _neighbor_docs(
                        str(atom.get("atom_id")),
                        connections_by_atom,
                        atom_by_id,
                    ),
                }
                for atom in sorted(group, key=lambda row: str(row.get("atom_id")))
            ],
        }
        for symbol, group in sorted(grouped.items())
    }


def _bonds_by_type(
    bonds: list[dict[str, Any]],
    connections_by_bond: dict[Any, list[dict[str, Any]]],
    atom_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bond in bonds:
        grouped[_dynamic_key(bond.get("bond_type"), "unknown_bond")].append(bond)
    return {
        bond_type: [
            _bond_doc(bond, connections_by_bond.get(bond.get("bond_id"), []), atom_by_id)
            for bond in sorted(group, key=lambda row: str(row.get("bond_id")))
        ]
        for bond_type, group in sorted(grouped.items())
    }


def _bond_doc(
    bond: dict[str, Any],
    connections: list[dict[str, Any]],
    atom_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "bond_id": bond.get("bond_id"),
        "bond_type": bond.get("bond_type"),
        "bond_type_presence_state": _presence_state(bond.get("bond_type")),
        "endpoints": [
            {
                "atom_id": connection.get("atom_id"),
                "paired_atom_id": connection.get("atom_id2"),
                "atom": _atom_ref(atom_by_id.get(str(connection.get("atom_id")))),
                "paired_atom": _atom_ref(atom_by_id.get(str(connection.get("atom_id2")))),
            }
            for connection in sorted(
                connections,
                key=lambda row: (str(row.get("atom_id")), str(row.get("atom_id2"))),
            )
        ],
    }


def _adjacency_by_atom_id(
    atoms: list[dict[str, Any]],
    connections_by_atom: dict[str, list[dict[str, Any]]],
    atom_by_id: dict[str, dict[str, Any]],
    connections_by_bond: dict[Any, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for atom in sorted(atoms, key=lambda row: str(row.get("atom_id"))):
        atom_id = str(atom.get("atom_id"))
        adjacency[atom_id] = [
            {
                "bond_id": connection.get("bond_id"),
                "neighbor_atom_id": connection.get("atom_id2"),
                "neighbor": _atom_ref(atom_by_id.get(str(connection.get("atom_id2")))),
                "bond_context": {
                    "connection_count": len(connections_by_bond.get(connection.get("bond_id"), [])),
                    "presence_state": _presence_state(connection.get("bond_id")),
                },
            }
            for connection in sorted(
                connections_by_atom.get(atom_id, []),
                key=lambda row: (str(row.get("bond_id")), str(row.get("atom_id2"))),
            )
        ]
    return adjacency


def _neighbor_docs(
    atom_id: str,
    connections_by_atom: dict[str, list[dict[str, Any]]],
    atom_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "bond_id": connection.get("bond_id"),
            "atom_id": connection.get("atom_id2"),
            "atom": _atom_ref(atom_by_id.get(str(connection.get("atom_id2")))),
        }
        for connection in sorted(
            connections_by_atom.get(atom_id, []),
            key=lambda row: (str(row.get("bond_id")), str(row.get("atom_id2"))),
        )
    ]


def _atom_ref(atom: dict[str, Any] | None) -> dict[str, Any]:
    if atom is None:
        return {"presence_state": "missing"}
    return {
        "atom_id": atom.get("atom_id"),
        "element": atom.get("element"),
        "element_presence_state": _presence_state(atom.get("element")),
    }


def _component_docs(
    atoms: list[dict[str, Any]],
    bonds: list[dict[str, Any]],
    atom_by_id: dict[str, dict[str, Any]],
    connections_by_bond: dict[Any, list[dict[str, Any]]],
    connections_by_atom: dict[str, list[dict[str, Any]]],
    db_id: str,
) -> list[dict[str, Any]]:
    atom_docs = [
        {
            "_id": f"atom:{atom.get('atom_id')}",
            "component_type": "atom",
            "source_db": db_id,
            "molecule_id": atom.get("molecule_id"),
            "atom": {
                "atom_id": atom.get("atom_id"),
                "element": atom.get("element"),
                "presence_state": _presence_state(atom.get("element")),
                "neighbors": _neighbor_docs(str(atom.get("atom_id")), connections_by_atom, atom_by_id),
            },
        }
        for atom in sorted(atoms, key=lambda row: str(row.get("atom_id")))
    ]
    bond_docs = [
        {
            "_id": f"bond:{bond.get('bond_id')}",
            "component_type": "bond",
            "source_db": db_id,
            "molecule_id": bond.get("molecule_id"),
            "bond": _bond_doc(
                bond,
                connections_by_bond.get(bond.get("bond_id"), []),
                atom_by_id,
            ),
        }
        for bond in sorted(bonds, key=lambda row: str(row.get("bond_id")))
    ]
    return atom_docs + bond_docs


def _presence_state(value: Any) -> str:
    if value is None:
        return "null"
    if value == "" or value == [] or value == {}:
        return "empty"
    return "present"


def _dynamic_key(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value)
    return text if text else fallback


def _label_interpretation(label: Any) -> str:
    if label == "+":
        return "carcinogenic"
    if label == "-":
        return "non_carcinogenic"
    if label == "":
        return "empty"
    if label is None:
        return "unknown"
    return str(label)


def _assay_tags(label: Any) -> list[str]:
    interpretation = _label_interpretation(label)
    if interpretation in {"carcinogenic", "non_carcinogenic"}:
        return [interpretation]
    return []


def _label_bucket(label: Any) -> dict[str, dict[str, Any]]:
    key = _label_interpretation(label)
    return {
        key: {
            "presence_state": _presence_state(label),
            "raw_label": label,
        }
    }


def _schema(db_id: str, data: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "db_id": db_id,
        "collections": {
            name: {
                "document_count": len(docs),
                "native_shape": "toxicology_molecule_graph"
                if name == "molecule_graphs"
                else "toxicology_component_union",
            }
            for name, docs in sorted(data.items())
        },
    }


def _manifest() -> NativeFeatureManifest:
    return NativeFeatureManifest(
        db_id="toxicology",
        features=[
            NativeFeature(
                id="molecule_graphs.elements_by_symbol",
                type="dynamic_key_object",
                collection="molecule_graphs",
                field="chemistry.elements_by_symbol",
                query_patterns=["dynamic_key_comparison", "nested_array_filter"],
                required_constructs=["$objectToArray", "$filter"],
                provenance_refs=["atom.element", "atom.atom_id"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "nested_array_filter",
                            "intent": "group atoms by element after traversing element-keyed atom arrays",
                            "pipeline": [
                                {
                                    "$project": {
                                        "label": "$identity.label",
                                        "elements": {
                                            "$objectToArray": "$chemistry.elements_by_symbol"
                                        },
                                    }
                                },
                                {"$unwind": "$elements"},
                                {"$unwind": "$elements.v.atoms"},
                                {
                                    "$group": {
                                        "_id": "$elements.k",
                                        "atom_count": {"$sum": 1},
                                        "molecules": {"$addToSet": "$_id"},
                                    }
                                },
                                {
                                    "$project": {
                                        "atom_count": 1,
                                        "molecule_count": {"$size": "$molecules"},
                                    }
                                },
                                {"$sort": {"atom_count": -1, "_id": 1}},
                                {"$limit": 25},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$size"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="molecule_graphs.assay_bonds_by_type",
                type="dynamic_key_object",
                collection="molecule_graphs",
                field="assay.views.bonds_by_type",
                query_patterns=["array_object_dynamic_key_comparison"],
                required_constructs=["$objectToArray", "$map"],
                provenance_refs=["bond.bond_type", "bond.bond_id"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "array_object_dynamic_key_comparison",
                            "intent": "compare bond-type buckets inside assay views across toxicology labels",
                            "pipeline": [
                                {"$unwind": "$assay.views"},
                                {
                                    "$project": {
                                        "label": "$identity.label",
                                        "bonds": {
                                            "$objectToArray": "$assay.views.bonds_by_type"
                                        },
                                    }
                                },
                                {"$unwind": "$bonds"},
                                {"$unwind": "$bonds.v"},
                                {
                                    "$group": {
                                        "_id": {
                                            "label": "$label",
                                            "bond_type": "$bonds.k",
                                        },
                                        "bond_count": {"$sum": 1},
                                        "molecules": {"$addToSet": "$_id"},
                                    }
                                },
                                {
                                    "$project": {
                                        "bond_count": 1,
                                        "molecule_count": {"$size": "$molecules"},
                                    }
                                },
                                {
                                    "$sort": {
                                        "bond_count": -1,
                                        "_id.label": 1,
                                        "_id.bond_type": 1,
                                    }
                                },
                                {"$limit": 25},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$size"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="molecule_graphs.supplemental_assay_presence_state",
                type="missing_vs_present",
                collection="molecule_graphs",
                field="assay.supplemental_panel.presence_state",
                query_patterns=["missing_vs_present", "presence_state_bucket_counts"],
                required_constructs=["$ifNull"],
                provenance_refs=["molecule.label"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "presence_state_bucket_counts",
                            "intent": "count explicit assay supplemental-panel presence states",
                            "pipeline": [
                                {
                                    "$addFields": {
                                        "native_presence_state": {
                                            "$ifNull": [
                                                "$assay.supplemental_panel.presence_state",
                                                "missing",
                                            ]
                                        }
                                    }
                                },
                                {
                                    "$group": {
                                        "_id": "$native_presence_state",
                                        "molecule_count": {"$sum": 1},
                                        "examples": {"$addToSet": "$_id"},
                                    }
                                },
                                {
                                    "$project": {
                                        "_id": 0,
                                        "native_presence_state": "$_id",
                                        "molecule_count": 1,
                                        "example_count": {"$size": "$examples"},
                                    }
                                },
                                {"$sort": {"molecule_count": -1, "native_presence_state": 1}},
                            ],
                            "mongo_native_constructs": ["$ifNull", "$group", "$size"],
                        }
                    ]
                },
            ),
        ],
    )


def _provenance(db_id: str) -> dict[str, Any]:
    return {
        "db_id": db_id,
        "conversion_code_ref": "tend.construction.designs.toxicology.materialize_native_dataworld",
        "entries": {
            "molecule_graphs.elements_by_symbol": {
                "source_tables": ["atom", "connected"],
                "provenance_refs": ["atom.element", "atom.atom_id", "connected.atom_id"],
            },
            "molecule_graphs.assay_bonds_by_type": {
                "source_tables": ["bond", "connected", "atom"],
                "provenance_refs": [
                    "bond.bond_type",
                    "bond.bond_id",
                    "connected.bond_id",
                    "connected.atom_id",
                    "connected.atom_id2",
                ],
            },
            "molecule_graphs.supplemental_assay_presence_state": {
                "source_tables": ["molecule"],
                "provenance_refs": ["molecule.label"],
            },
        },
    }
